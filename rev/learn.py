"""Live one-shot learning: an engineer corrects the agent ONCE in chat, River updates the weights.

    python3 -m rev.learn --text "Our USB-A cutouts always get 0.5 mm per side." [--promote] [--heldout]

Flow (every step is emitted as an event for the UI):
  1. parse_correction(text) -> {"type": "usb_a", "tolerance": 0.5}
  2. verify: generate lesson tasks where that connector is added/moved; gold uses the corrected tolerance.
     EVERY example is gated by the deterministic checker (kernel.passes) -- only verified examples are learned.
     Mixed with replay examples from the company history (train.jsonl) so nothing else is forgotten.
  3. train: continue the current tuned LoRA (River create_model(checkpoint=...)) for a few steps.
  4. eval: tuned-before vs tuned-after on rev/data/usb_a_test.jsonl (held out, disjoint seed).
  5. save rev/data/checkpoint_v2.json; with promote=True also rev/data/checkpoint_current.json,
     which river_util.sample_tuned() honours over checkpoint.json.

Events: {"type":"learn","stage":"verify|train|eval","step","total_steps","loss","msg"}
        {"type":"learned","before":{"passed","total"},"after":{"passed","total"},"seconds",...}
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import random
import re
import threading
import time
from pathlib import Path

from rev import kernel, prompts, tasks
from rev import river_util as ru

DATA_DIR = ru.DATA_DIR
USB_A_TEST_PATH = DATA_DIR / "usb_a_test.jsonl"
CKPT_V2_PATH = DATA_DIR / "checkpoint_v2.json"
CURRENT_PATH = ru.CURRENT_CHECKPOINT_PATH if hasattr(ru, "CURRENT_CHECKPOINT_PATH") else DATA_DIR / "checkpoint_current.json"

N_LESSON = 48
N_REPLAY = 48
N_TEST = 12
LESSON_SEED = 7001
TEST_SEED = 7002
REPLAY_SEED = 7003

# training hyper-params (measured; see report in PR / README)
STEPS = int(os.environ.get("REV_LEARN_STEPS", "8"))
BATCH = int(os.environ.get("REV_LEARN_BATCH", "16"))   # half lesson, half replay per step
LR = float(os.environ.get("REV_LEARN_LR", "1e-4"))
LESSON_PER_BATCH = int(os.environ.get("REV_LEARN_LESSON_PER_BATCH", "0"))  # 0 -> batch // 2
DEFAULT_RANK = 32
MAX_TOKENS = 512

# ---------------------------------------------------------------- 1. parse the correction

_TYPE_PATTERNS = [
    ("usb_a", r"usb[\s_\-]*(?:type[\s_\-]*)?a\b|type[\s_\-]*a\s*usb|\busba\b"),
    ("usb_c", r"usb[\s_\-]*(?:type[\s_\-]*)?c\b|type[\s_\-]*c\b|\busbc\b"),
    ("hdmi", r"\bhdmi\b"),
    ("rj45", r"\brj[\s_\-]*45\b|\bethernet\b"),
    ("barrel", r"\bbarrel\b|\bdc[\s_\-]*jack\b|\bpower[\s_\-]*jack\b"),
    ("sd", r"\b(?:micro[\s_\-]*)?sd\b(?:[\s_\-]*card)?"),
]


def parse_correction(text):
    """'Our USB-A cutouts always get 0.5 mm per side.' -> {"type": "usb_a", "tolerance": 0.5}; None if not understood."""
    if not isinstance(text, str) or not text.strip():
        return None
    t = text.lower()
    ctype = None
    for name, pat in _TYPE_PATTERNS:
        if re.search(pat, t):
            ctype = name
            break
    if ctype is None or ctype not in kernel.CONNECTOR_SPECS:
        return None
    m = re.search(r"(\d+(?:\.\d+)?|\.\d+)\s*(?:mm|millimet(?:er|re)s?)\b", t)
    if not m:
        nums = re.findall(r"(?<![\w.])(\d*\.\d+|\d+)(?![\w.])", t.replace("rj45", "").replace("rj-45", ""))
        if len(nums) != 1:
            return None
        val = nums[0]
    else:
        val = m.group(1)
    try:
        tol = round(float(val), 3)
    except ValueError:
        return None
    if not (0 < tol <= 5):
        return None
    return {"type": ctype, "tolerance": tol}


# ---------------------------------------------------------------- 2. lesson data (checker-gated)

_EXTRAS = ["resize", "move_hole", "add_hole", "remove_hole", "taller_component", "remove_connector"]
CONTRAST_P = 0.7  # share of lessons that ALSO re-place another connector type's opening (keeps 0.5 specific to the type)


def _move_type(rng, b, touched, ctype):
    cands = [c for c in b["connectors"] if c["type"] == ctype and c["id"] not in touched]
    for c in cands:
        for _ in range(20):
            delta = tasks.g05(rng.uniform(3, 20)) * rng.choice([1, -1])
            nc = dict(c, pos=tasks.r2(c["pos"] + delta))
            if tasks.conn_ok(b, nc):
                c["pos"] = nc["pos"]
                touched.add(c["id"])
                if c["side"] in ("front", "back"):
                    d = "right" if delta > 0 else "left"
                else:
                    d = "back" if delta > 0 else "front"
                return f"{c['id']} ({c['type']}) moved {abs(delta):.1f} mm toward {d}"
    return None


def _apply_extra(rng, kind, b, touched):
    if kind == "resize":
        return tasks.ch_resize(rng, b)
    if kind == "move_connector":
        return tasks.ch_move_connector(rng, b, touched)
    if kind == "move_hole":
        return tasks.ch_move_hole(rng, b, touched)
    if kind == "add_hole":
        return tasks.ch_add_hole(rng, b, touched)
    if kind == "remove_hole":
        return tasks.ch_remove_hole(rng, b, touched)
    if kind == "taller_component":
        return tasks.ch_taller(rng, b)
    if kind == "remove_connector":
        return tasks.ch_remove_connector(rng, b, touched)
    if kind == "add_other":
        return tasks.ch_add_connector(rng, b, touched, False, set(), tasks.TYPES)
    return None


def _gold_with_tol(board_a, enc_a, board_b, ctype, tol):
    gold = tasks.gold_calls(board_a, enc_a, board_b)
    types = {c["id"]: c["type"] for c in board_b["connectors"]}
    for c in gold:
        if c["tool"] == "place_opening" and types.get(c["args"]["connector"]) == ctype:
            c["args"]["tolerance"] = tol
    return gold


def make_lesson_task(rng, tid, ctype, tol):
    """One engineering change that adds (or moves) a `ctype` connector, plus 0-2 ordinary changes.

    Returns (task, verified). The gold uses the corrected tolerance `tol`; verified = the deterministic
    checker accepts gold on board_b (and the stale enclosure fails)."""
    for _ in range(300):
        mode = rng.choice(["add", "add", "add", "move"])
        types_a = rng.sample(tasks.TYPES, rng.choice([1, 2, 2, 3]))
        if mode == "move":
            types_a.append(ctype)
        board_a = tasks.random_board(rng, types_a)
        if not board_a["connectors"]:
            continue
        if mode == "move" and not any(c["type"] == ctype for c in board_a["connectors"]):
            continue
        board_b = copy.deepcopy(board_a)
        board_b["rev"] = "B"
        extras = rng.sample(_EXTRAS, rng.choice([0, 1, 1]))
        if rng.random() < CONTRAST_P:
            extras.append(rng.choice(["move_connector", "add_other", "add_other"]))
        extras.sort(key=lambda x: 0 if x == "resize" else 1)
        protect = {c["id"] for c in board_b["connectors"] if c["type"] == ctype}  # the lesson connector itself
        tt, summaries, ok = set(protect), [], True
        for kind in extras:
            s = _apply_extra(rng, kind, board_b, tt)
            if not s:
                ok = False
                break
            summaries.append(s)
        if not ok:
            continue
        touched = tt - protect
        if mode == "add":
            s = tasks.ch_add_connector(rng, board_b, touched, False, set(), [ctype])
        else:
            s = _move_type(rng, board_b, touched, ctype)
        if not s:
            continue
        first = 1 if (extras and extras[0] == "resize") else 0  # history lists resizes first
        summaries.insert(rng.randrange(first, len(summaries) + 1), s)
        if not tasks.board_valid(board_b):
            continue
        enc_a = kernel.enclosure_for(board_a)
        gold = _gold_with_tol(board_a, enc_a, board_b, ctype, tol)
        enc_b, errs = kernel.apply_calls(board_b, enc_a, gold)
        verified = (not errs) and kernel.passes(board_b, enc_b) and not kernel.passes(board_b, enc_a)
        summary = "; ".join(summaries)
        summary = summary[0].upper() + summary[1:]
        return {"id": tid, "board_a": board_a, "enclosure_a": enc_a, "board_b": board_b,
                "change_summary": summary, "gold_calls": gold, "kinds": [mode + "_" + ctype] + extras,
                "lesson": {"type": ctype, "tolerance": tol}}, verified
    raise RuntimeError("could not generate lesson task")


def generate_lesson(ctype, tol, n, seed, exclude=None):
    """Returns (verified_tasks, n_generated)."""
    rng = random.Random(seed)
    seen = set(exclude or ())
    out, generated = [], 0
    while len(out) < n and generated < n * 4:
        t, ok = make_lesson_task(rng, f"lesson-{ctype}-{generated:03d}", ctype, tol)
        k = tasks._key(t)
        if k in seen:
            continue
        seen.add(k)
        generated += 1
        if ok:
            out.append(t)
    return out, generated


def usb_a_test(regen=False):
    """Held-out usb_a tasks (seed TEST_SEED, disjoint from lessons and history). Gold uses the HOUSE value."""
    if USB_A_TEST_PATH.exists() and not regen:
        return [json.loads(l) for l in USB_A_TEST_PATH.read_text().splitlines() if l.strip()]
    rows, _ = generate_lesson("usb_a", kernel.HOUSE["tolerance"]["usb_a"], N_TEST, TEST_SEED)
    for i, r in enumerate(rows):
        r["id"] = f"usb_a-test-{i:03d}"
    assert len(rows) == N_TEST
    USB_A_TEST_PATH.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
    return rows


def heldout_for(ctype, tol):
    if ctype == "usb_a":
        return usb_a_test()
    rows, _ = generate_lesson(ctype, tol, N_TEST, TEST_SEED)
    return rows


def replay(n, seed=REPLAY_SEED):
    """History examples that place openings for OTHER connector types (so their tolerances aren't forgotten)."""
    rows = [r for r in tasks.load("train") if any(c["tool"] == "place_opening" for c in r["gold_calls"])]
    return random.Random(seed).sample(rows, n)


# ---------------------------------------------------------------- 3. eval helpers

def score(task, text):
    calls = prompts.parse_calls(text)
    if not isinstance(calls, list):
        return {"pass": False, "parsed": False, "fails": ["parse"]}
    enc, errs = kernel.apply_calls(task["board_b"], task["enclosure_a"], calls)
    res = kernel.check(task["board_b"], enc)
    return {"pass": all(c["pass"] for c in res), "parsed": True, "errors": errs,
            "fails": [c["id"] for c in res if not c["pass"]],
            "refs": sorted({r for c in res if not c["pass"] for r in c["refs"]})}


def sample_batch(prompt_texts, ckpt=None, max_tokens=MAX_TOKENS):
    """Greedy-sample many prompts in one River call (ckpt=None -> base model)."""
    last = None
    for attempt in range(2):
        try:
            if ckpt is None:
                out = ru.get_client().sample(prompt_texts, base_model=ru.BASE_MODEL, max_tokens=max_tokens,
                                             temperature=0.0, stop=ru.STOP)
            else:
                out = ru.get_session().sample(prompt_texts, base_model=ru.BASE_MODEL, checkpoint=ckpt,
                                              max_tokens=max_tokens, temperature=0.0, stop=ru.STOP)
            return [ru._first_text(o) for o in out]
        except Exception as e:  # noqa: BLE001
            last = e
            ru.close_session()
    raise last


def evaluate(rows, ckpt=None, ctype=None):
    """Pass count on rows; with ctype also `type_ok` = tasks whose ctype openings are all correct."""
    texts = sample_batch([ru.render(prompts.messages(t)) for t in rows], ckpt)
    scores = [score(t, x) for t, x in zip(rows, texts)]
    out = {"passed": sum(s["pass"] for s in scores), "total": len(rows), "details": scores, "texts": texts}
    if ctype:
        ok = 0
        for t, s in zip(rows, scores):
            ids = {c["id"] for c in t["board_b"]["connectors"] if c["type"] == ctype}
            ok += bool(s["parsed"]) and not (ids & set(s.get("refs", [])))
        out["type_ok"] = ok
    return out


def _ckpt_record(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def current_checkpoint_path():
    if hasattr(ru, "active_checkpoint_path"):
        return Path(ru.active_checkpoint_path())
    return CURRENT_PATH if CURRENT_PATH.exists() else ru.CHECKPOINT_PATH


# ---------------------------------------------------------------- 4. learn

def _default_emit(ev):
    if ev.get("type") == "learn":
        loss = f" loss={ev['loss']:.4f}" if isinstance(ev.get("loss"), (int, float)) else ""
        print(f"{ev.get('t', 0):6.1f}s [{ev['stage']:6s}] {ev.get('step', '')}/{ev.get('total_steps', '')}{loss}  {ev.get('msg', '')}", flush=True)
    else:
        print(json.dumps({k: v for k, v in ev.items() if k != "examples"}), flush=True)


def learn(correction, emit=None, promote=False, steps=None, batch=None, lr=None, heldout=False,
          base_checkpoint_path=None, fresh=False, lesson_per_batch=None, out_path=None):
    """Teach one correction. `correction` is chat text or {"type","tolerance"}. Returns the 'learned' event."""
    import river_client as river

    emit = emit or _default_emit
    steps = steps or STEPS
    batch = batch or BATCH
    lr = lr or LR
    t0 = time.time()

    def ev(stage, msg, step=0, total=steps, loss=None, **extra):
        e = {"type": "learn", "stage": stage, "step": step, "total_steps": total, "loss": loss, "msg": msg,
             "t": round(time.time() - t0, 1)}
        e.update(extra)
        emit(e)

    lesson = parse_correction(correction) if isinstance(correction, str) else correction
    if not lesson:
        ev("verify", "Could not understand the correction (need a connector type and a tolerance in mm).")
        out = {"type": "learned", "ok": False, "error": "could not parse correction", "seconds": round(time.time() - t0, 1)}
        emit(out)
        return out
    ctype, tol = lesson["type"], float(lesson["tolerance"])
    ev("verify", f"Correction understood: {ctype} opening tolerance = {tol} mm per side")

    # --- verify: build lesson examples, gate each through the deterministic checker
    test_rows = heldout_for(ctype, tol)
    lesson_rows, generated = generate_lesson(ctype, tol, N_LESSON, LESSON_SEED, exclude={tasks._key(t) for t in test_rows})
    ev("verify", f"verified {len(lesson_rows)}/{generated} lesson examples with the checker "
       f"(only verified examples are learned)", step=len(lesson_rows), total=generated)
    if len(lesson_rows) < N_LESSON // 2:
        ev("verify", "Checker rejected the lesson: the correction contradicts the house spec. Nothing learned.")
        out = {"type": "learned", "ok": False, "error": "lesson failed verification",
               "verified": len(lesson_rows), "generated": generated, "seconds": round(time.time() - t0, 1)}
        emit(out)
        return out
    replay_rows = replay(N_REPLAY)
    ev("verify", f"+ {len(replay_rows)} replay examples from the company history (prevents forgetting)")

    # --- which weights do we continue from?
    base_path = Path(base_checkpoint_path) if base_checkpoint_path else current_checkpoint_path()
    base_rec = None if fresh else _ckpt_record(base_path)
    base_ckpt = None
    if base_rec:
        base_ckpt = river.Checkpoint(path=base_rec["path"], step=int(base_rec.get("step", 0)),
                                     checkpoint_type=base_rec.get("checkpoint_type", "inference"))
    rank = int((base_rec or {}).get("lora_rank") or (base_rec or {}).get("rank") or DEFAULT_RANK)

    # --- eval "before" concurrently with training (same held-out set)
    before_box = {}

    def _before():
        try:
            before_box["r"] = evaluate(test_rows, base_ckpt, ctype)
        except Exception as e:  # noqa: BLE001
            before_box["err"] = repr(e)

    th = threading.Thread(target=_before, daemon=True)
    th.start()

    # --- data
    lesson_data = [ru.make_datum(ru.render(prompts.messages(t)), prompts.completion(t)) for t in lesson_rows]
    replay_data = [ru.make_datum(ru.render(prompts.messages(t)), prompts.completion(t)) for t in replay_rows]
    rng = random.Random(11)
    half = lesson_per_batch or LESSON_PER_BATCH or batch // 2
    out_path = Path(out_path) if out_path else CKPT_V2_PATH

    client = ru.get_client()
    losses = []
    run_name = f"learn-{ctype}-{int(time.time())}"
    with client.session(app="rev", run=run_name) as session:
        if base_ckpt is not None:
            ev("train", f"Loading current tuned weights ({base_path.name}, step {base_ckpt.step}) to continue training")
            model = session.create_model(base_model=ru.BASE_MODEL, lora=river.LoraConfig(rank=rank), checkpoint=base_ckpt)
        else:
            ev("train", "No tuned checkpoint found: starting a fresh LoRA (history + lesson)")
            model = session.create_model(base_model=ru.BASE_MODEL, lora=river.LoraConfig(rank=rank))
        li, ri = list(range(len(lesson_data))), list(range(len(replay_data)))
        rng.shuffle(li); rng.shuffle(ri)
        for s in range(steps):
            b = [lesson_data[li[(s * half + j) % len(li)]] for j in range(half)]
            b += [replay_data[ri[(s * (batch - half) + j) % len(ri)]] for j in range(batch - half)]
            fb, _opt = model.train_step(b, lr=lr, loss_fn="cross_entropy", grad_clip_norm=1.0)
            m = fb.metrics or {}
            loss = m.get("loss_mean", m.get("loss"))
            loss = float(loss) if loss is not None else None
            losses.append(loss)
            ev("train", f"step {s + 1}/{steps}: {half} lesson + {batch - half} replay examples", step=s + 1, loss=loss)
        ev("train", "Saving new weights", step=steps)
        ckpt = model.save_weights(run_name, mode="inference")
    rec = ru.save_checkpoint_ref(ckpt, out_path, lora_rank=rank, lesson=lesson, parent=(base_rec or {}).get("path"),
                                 steps=steps, batch=batch, lr=lr, created=time.strftime("%Y-%m-%d %H:%M:%S"))

    # --- eval after
    ev("eval", f"Evaluating new weights on {len(test_rows)} held-out {ctype} changes (fresh session, nothing in the prompt)",
       step=steps)
    after = evaluate(test_rows, ckpt, ctype)
    th.join(timeout=300)
    before = before_box.get("r")
    if before is None:
        ev("eval", f"before-eval failed ({before_box.get('err')}); retrying", step=steps)
        before = evaluate(test_rows, base_ckpt, ctype)
    ev("eval", f"{ctype} held-out: before {before['passed']}/{before['total']} -> after {after['passed']}/{after['total']} "
       f"({ctype} openings right: {before.get('type_ok')} -> {after.get('type_ok')})", step=steps)
    _dump_debug(test_rows, before, after)

    out = {"type": "learned", "ok": True, "lesson": lesson,
           "before": {"passed": before["passed"], "total": before["total"], "type_ok": before.get("type_ok")},
           "after": {"passed": after["passed"], "total": after["total"], "type_ok": after.get("type_ok")},
           "verified": len(lesson_rows), "generated": generated, "replay": len(replay_rows),
           "steps": steps, "losses": losses, "checkpoint": rec["path"], "checkpoint_file": out_path.name, "parent": rec.get("parent"),
           "promoted": False}
    if heldout:
        hrows = tasks.load("test")
        ev("eval", f"Regression check: {len(hrows)} regular held-out changes", step=steps)
        h_after = evaluate(hrows, ckpt)
        out["heldout_after"] = {"passed": h_after["passed"], "total": h_after["total"]}
        ev("eval", f"regular held-out after learning: {h_after['passed']}/{h_after['total']}", step=steps)
    if promote:
        promote_checkpoint(out_path)
        out["promoted"] = True
        ev("eval", "Promoted: the agent's tuned model now uses the new weights", step=steps)
    out["seconds"] = round(time.time() - t0, 1)
    emit(out)
    return out


def _dump_debug(rows, before, after):
    try:
        (DATA_DIR / "learn_eval_last.json").write_text(json.dumps([
            {"id": t["id"], "summary": t["change_summary"], "gold": t["gold_calls"],
             "before": before["texts"][i], "before_score": before["details"][i],
             "after": after["texts"][i], "after_score": after["details"][i]} for i, t in enumerate(rows)], indent=1))
    except Exception:  # noqa: BLE001
        pass


def promote_checkpoint(src=CKPT_V2_PATH):
    rec = json.loads(Path(src).read_text())
    rec["promoted_from"] = Path(src).name
    rec["promoted_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    tmp = CURRENT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rec, indent=2))
    os.replace(tmp, CURRENT_PATH)
    return rec


def unpromote():
    """Back to the original tuned checkpoint (rev/data/checkpoint.json) -- for demo rehearsals."""
    try:
        CURRENT_PATH.unlink()
        return True
    except FileNotFoundError:
        return False


DEMO_REV_C_PATH = DATA_DIR / "demo_rev_c.json"


def demo_rev_c():
    """Stage scenario: current design = demo Rev B board + its correct enclosure; new board = Rev C which adds
    J5 (usb_a, never seen in the company history) on the front edge and moves J2 (hdmi) 6 mm right."""
    demo = tasks.load("demo")
    a = copy.deepcopy(demo["board_b"])
    enc_a = kernel.enclosure_for(a)
    b = copy.deepcopy(a)
    b["rev"] = "C"
    for c in b["connectors"]:
        if c["id"] == "J2":
            c["pos"] = tasks.r2(c["pos"] + 6.0)
    b["connectors"].append(tasks.make_connector("J5", "usb_a", "front", 60.0))
    assert tasks.board_valid(b), "Rev C board invalid"
    gold = tasks.gold_calls(a, enc_a, b)
    enc_b, errs = kernel.apply_calls(b, enc_a, gold)
    assert not errs and kernel.passes(b, enc_b) and not kernel.passes(b, enc_a)
    return {"board_a": a, "enclosure_a": enc_a, "board_b": b,
            "change_summary": "J2 (hdmi) moved 6.0 mm toward right; added J5 (usb_a) on front edge",
            "gold_calls": gold}


def write_demo_rev_c():
    d = demo_rev_c()
    DEMO_REV_C_PATH.write_text(json.dumps(d, indent=1))
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default="Our USB-A cutouts always get 0.5 mm per side.")
    ap.add_argument("--promote", action="store_true")
    ap.add_argument("--heldout", action="store_true", help="also eval the regular 30 held-out tasks after learning")
    ap.add_argument("--steps", type=int)
    ap.add_argument("--batch", type=int)
    ap.add_argument("--lr", type=float)
    ap.add_argument("--fresh", action="store_true", help="ignore existing checkpoint, train a fresh LoRA")
    ap.add_argument("--base", help="checkpoint json to continue from (default: current)")
    ap.add_argument("--unpromote", action="store_true")
    ap.add_argument("--lesson-per-batch", type=int)
    ap.add_argument("--out", help="checkpoint json to write (default rev/data/checkpoint_v2.json)")
    ap.add_argument("--data-only", action="store_true", help="just build + verify data, no River")
    a = ap.parse_args()
    if not DEMO_REV_C_PATH.exists():
        write_demo_rev_c()
    if a.unpromote:
        print("unpromoted" if unpromote() else "nothing to unpromote")
        return
    if a.data_only:
        lesson = parse_correction(a.text)
        print("parsed:", lesson)
        test_rows = heldout_for(lesson["type"], lesson["tolerance"])
        rows, gen = generate_lesson(lesson["type"], lesson["tolerance"], N_LESSON, LESSON_SEED,
                                    exclude={tasks._key(t) for t in test_rows})
        print(f"verified {len(rows)}/{gen}; test {len(test_rows)}")
        return
    events = []

    def emit(e):
        events.append(e)
        _default_emit(e)

    r = learn(a.text, emit=emit, promote=a.promote, steps=a.steps, batch=a.batch, lr=a.lr, heldout=a.heldout,
              base_checkpoint_path=a.base, fresh=a.fresh, lesson_per_batch=a.lesson_per_batch, out_path=a.out)
    if r.get("ok") and not a.out:  # recorded real run: POST /api/learn {"cached": true} replays it (stage fallback)
        (DATA_DIR / "learn_last.json").write_text(json.dumps({"text": a.text, "events": events}, indent=1))
    print(json.dumps({k: v for k, v in r.items()}, indent=1))


if __name__ == "__main__":
    main()
