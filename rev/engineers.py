"""Personal AI engineers: every engineer at 49th Labs gets their own AI engineer.

Each one starts from the company model (v1 = Qwen3.5-9B + Acme LoRA trained on 200 verified ECOs) and keeps
learning from THAT engineer's own work (CAD saves via the capture watcher, chat corrections) into:
  * a personal GBrain area   engineers/<id>/lessons/..., engineers/<id>/captures/...
  * a personal LoRA branch   rev/data/engineers/<id>/checkpoint.json   (never the company production checkpoint)

    python3 -m rev.engineers list
    python3 -m rev.engineers teach senior-me "USB-A cutouts get 0.5 mm per side."
    python3 -m rev.engineers refit --who field-me
    python3 -m rev.engineers test --model 9b --variant untrained|custom|engineer [--engineer senior-me] [--suite usb_a]
    python3 -m rev.engineers record senior-me
    python3 -m rev.engineers merge senior-me
    python3 -m rev.engineers reset [--engineer senior-me]

Engineers are identified by role handles only (no real names).
"""
from __future__ import annotations

import argparse
import copy
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# HF Hub rate-limits (429) parallel tokenizer loads; all our tokenizers are in the local HF cache.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from rev import kernel, prompts, tasks
from rev import river_util as ru

ROOT = ru.ROOT
DATA_DIR = ru.DATA_DIR
ROSTER_PATH = DATA_DIR / "engineers.json"
ENG_DIR = DATA_DIR / "engineers"
PRISTINE_DIR = ENG_DIR / "_pristine"
COMPANY_CKPT = DATA_DIR / "checkpoint.json"          # company v1 (never written here)
DEMO_REV_C = DATA_DIR / "demo_rev_c.json"
SUITES = {"heldout": DATA_DIR / "test.jsonl", "usb_a": DATA_DIR / "usb_a_test.jsonl"}
CHECK_IDS = ["cavity_fit", "cavity_height", "standoffs", "openings", "orphans"]
MAX_TOKENS = 768
PY = sys.executable or "/opt/anaconda3/bin/python3"

DEFAULT_ROSTER = [
    {"id": "senior-me", "role": "Senior mechanical engineer", "display": "Senior ME (you)"},
    {"id": "field-me", "role": "Field / ruggedization mechanical engineer", "display": "Field ME"},
    {"id": "junior-me", "role": "Junior mechanical engineer", "display": "Junior ME"},
]

_io = threading.RLock()


def _now():
    return _dt.datetime.now().isoformat(timespec="seconds")


def _rj(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return default


def _wj(p, obj):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    os.replace(tmp, p)


# ============================================================================ models

def _ckpt_obj(rec):
    import river_client as river
    if not rec:
        return None
    if isinstance(rec, (str, Path)):
        rec = _rj(rec)
        if not rec:
            return None
    return river.Checkpoint(path=rec["path"], step=int(rec.get("step", 0)),
                            checkpoint_type=rec.get("checkpoint_type", "inference"))


class Model:
    """One base model family: its own tokenizer/renderer, base sampling and LoRA-checkpoint sampling."""

    def __init__(self, key, base_model, custom_checkpoint, results=None, label=None):
        self.key, self.base_model = key, base_model
        self.custom_checkpoint = str(custom_checkpoint) if custom_checkpoint else None
        self.results = results or {}
        self.label = label or base_model.split("/")[-1]
        self._tok = self._ren = None
        self._lock = threading.Lock()

    def __getitem__(self, k):  # dict-style access: MODELS["9b"]["base_model"]
        return {"key": self.key, "base_model": self.base_model, "custom_checkpoint": self.custom_checkpoint,
                "results": self.results, "label": self.label, "sample": self.sample}[k]

    def get(self, k, default=None):
        try:
            return self[k]
        except KeyError:
            return default

    def keys(self):
        return ["key", "base_model", "custom_checkpoint", "results", "label", "sample"]

    def _renderer(self):
        with self._lock:
            if self._tok is None:
                if self.base_model == ru.BASE_MODEL:
                    self._tok, self._ren = ru.get_tokenizer(), ru.get_renderer()
                else:
                    import river_client as river
                    from river_client.renderers import get_renderer as _get
                    try:
                        self._tok = river.load_tokenizer(base_model=self.base_model, local_files_only=True)
                    except Exception:
                        self._tok = river.load_tokenizer(base_model=self.base_model)
                    try:
                        self._ren = _get(self.base_model, thinking=False, tokenizer=self._tok)
                    except Exception:
                        self._ren = None
            return self._tok, self._ren

    def render(self, messages):
        tok, ren = self._renderer()
        msgs = [{"role": m["role"], "content": m["content"]} for m in messages]
        if ren is not None:
            return ren.build_prompt_str(msgs)
        try:
            return tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True, enable_thinking=False)
        except TypeError:
            return tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)

    def stop(self):
        tok, ren = self._renderer()
        if ren is not None:
            return list(ren.get_stop_strings())
        return [tok.eos_token] if getattr(tok, "eos_token", None) else []

    def sample(self, prompt_messages, checkpoint=None, max_tokens=MAX_TOKENS, tries=5):
        """prompt_messages: chat messages (or an already-rendered str). checkpoint: None (base), a path to a
        checkpoint json, a record dict, or a river.Checkpoint."""
        text = prompt_messages if isinstance(prompt_messages, str) else self.render(prompt_messages)
        ck = checkpoint
        if ck is not None and not hasattr(ck, "checkpoint_type"):
            ck = _ckpt_obj(ck)
        kw = {"max_tokens": max_tokens, "temperature": 0.0, "stop": self.stop()}
        delay = 2.0
        for i in range(tries):
            try:
                if ck is None:
                    out = ru.get_client().sample(text, base_model=self.base_model, **kw)
                else:
                    out = ru.get_session().sample(text, base_model=self.base_model, checkpoint=ck, **kw)
                return ru._first_text(out)
            except Exception as e:  # noqa: BLE001
                m = str(e).lower()
                if ("auth" in m and "rate" not in m) or i == tries - 1:
                    raise
                if ck is not None:
                    ru.close_session()
                time.sleep(delay)
                delay = min(delay * 2, 20)
        raise RuntimeError("unreachable")


def _load_models():
    out = {"9b": Model("9b", _rj(COMPANY_CKPT, {}).get("base_model", ru.BASE_MODEL), COMPANY_CKPT,
                       _rj(DATA_DIR / "eval_results.json", {}), "Qwen3.5-9B")}
    for key, sub in (("35b", "q35b"), ("122b", "q122b"), ("397b", "q397b")):
        d = ROOT / "rev" / "large" / sub
        ck = _rj(d / "checkpoint.json")
        res = _rj(d / "results.json", {})
        base = (ck or {}).get("base_model") or res.get("base_model")
        if base:
            out[key] = Model(key, base, d / "checkpoint.json" if ck else None, res, base.split("/")[-1])
    return out


MODELS = _load_models()


def _model(key):
    k = str(key or "9b").lower().replace("-", "").rstrip()
    if k not in MODELS:
        k = {"9": "9b", "35": "35b", "122": "122b", "397": "397b"}.get(k.rstrip("b"), k)
    if k not in MODELS:
        raise ValueError(f"unknown model {key!r}; have {sorted(MODELS)}")
    return MODELS[k]


# ============================================================================ roster

def _edir(eid):
    return ENG_DIR / eid


def _roster():
    r = _rj(ROSTER_PATH)
    if not r:
        r = {"company": "49th Labs", "customer": "Acme Devices", "engineers": DEFAULT_ROSTER}
        _wj(ROSTER_PATH, r)
    return r


def _meta(eid):
    for e in _roster()["engineers"]:
        if e["id"] == eid:
            return e
    raise ValueError(f"unknown engineer {eid!r}; have {[e['id'] for e in _roster()['engineers']]}")


def _lessons(eid):
    return _rj(_edir(eid) / "lessons.json", [])


def _profile(eid):
    """Engineer's personal rule overrides (lessons that differ from the company house spec)."""
    prof = {}
    for les in _lessons(eid):
        t, tol = les.get("type"), les.get("tolerance")
        if t and tol is not None and abs(float(tol) - kernel.HOUSE["tolerance"].get(t, -1)) > 1e-6:
            prof[t] = float(tol)
    return prof


def engineer_checkpoint(eid):
    p = _edir(eid) / "checkpoint.json"
    return str(p) if p.exists() else None


def list_engineers():
    out = []
    for e in _roster()["engineers"]:
        ck = _rj(_edir(e["id"]) / "checkpoint.json")
        les = _lessons(e["id"])
        out.append({"id": e["id"], "role": e["role"], "display": e.get("display", e["role"]), "base": "company v1",
                    "lessons": [{k: l.get(k) for k in ("type", "tolerance", "text", "source", "ts", "gbrain_slug")}
                                for l in les],
                    "checkpoint": (ck or {}).get("path"),
                    "trained_steps": int(sum(int(l.get("steps") or 0) for l in les)),
                    "profile": _profile(e["id"])})
    return out


# ============================================================================ test (live eval)

def _load_suite(suite):
    p = SUITES.get(suite)
    if p is None:
        raise ValueError(f"suite must be one of {sorted(SUITES)}")
    if suite == "usb_a" and not p.exists():
        from rev import learn
        learn.usb_a_test()
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def grade(task, text):
    from rev import eval as ev
    return ev.grade(task, text)


def _resolve_ckpt(model, variant, engineer_id):
    m = _model(model)
    if variant == "untrained":
        return m, None
    if variant == "custom":
        if not m.custom_checkpoint:
            raise ValueError(f"no custom checkpoint for {m.key}")
        return m, m.custom_checkpoint
    if variant == "engineer":
        if not engineer_id:
            raise ValueError("variant=engineer needs engineer_id")
        _meta(engineer_id)
        if m.key != "9b":
            raise ValueError("personal branches are trained on the 9b company model")
        return m, engineer_checkpoint(engineer_id) or m.custom_checkpoint
    raise ValueError("variant must be untrained|custom|engineer")


def test(model="9b", variant="untrained", engineer_id=None, suite="heldout", limit=None, workers=12, emit=None):
    m, ck = _resolve_ckpt(model, variant, engineer_id)
    rows = _load_suite(suite)
    if limit:
        rows = rows[: int(limit)]
    t0 = time.time()
    results = [None] * len(rows)

    def job(i):
        t = rows[i]
        try:
            text, err = m.sample(prompts.messages(t), ck), None
        except Exception as e:  # noqa: BLE001
            text, err = "", f"{type(e).__name__}: {e}"
        g = grade(t, text)
        g["id"], g["sample_error"] = t.get("id", str(i)), err
        results[i] = g
        if emit:
            try:
                emit({"task": g["id"], "pass": g["pass"], "failed": g["failed"]})
            except Exception:
                pass
        return g

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        list(ex.map(job, range(len(rows))))
    return {"model": m.key, "base_model": m.base_model, "variant": variant, "engineer": engineer_id, "suite": suite,
            "passed": sum(r["pass"] for r in results), "total": len(results),
            "by_check": {c: sum(r["checks"].get(c, False) for r in results) for c in CHECK_IDS},
            "seconds": round(time.time() - t0, 1),
            "failures": [{"id": r["id"], "failed": r["failed"]} for r in results if not r["pass"]]}


# ============================================================================ refit_as (Rev C)

def _rev_c(rev="C"):
    if str(rev).upper() != "C":
        raise ValueError("only Rev C is staged")
    if not DEMO_REV_C.exists():
        from rev import learn
        learn.write_demo_rev_c()
    return json.loads(DEMO_REV_C.read_text())


def _check_with(board, enc, overrides):
    if not overrides:
        return kernel.check(board, enc)
    with _io:
        saved = dict(kernel.HOUSE["tolerance"])
        try:
            kernel.HOUSE["tolerance"].update(overrides)
            return kernel.check(board, enc)
        finally:
            kernel.HOUSE["tolerance"].clear()
            kernel.HOUSE["tolerance"].update(saved)


def refit_as(who="senior-me", rev="C", model="9b"):
    task = _rev_c(rev)
    m = _model(model)
    if who == "untrained":
        ck, prof = None, {}
    elif who == "company":
        ck, prof = m.custom_checkpoint, {}
    else:
        _meta(who)
        ck, prof = engineer_checkpoint(who) or m.custom_checkpoint, _profile(who)
    t0 = time.time()
    text = m.sample(prompts.messages(task), ck)
    lat = round(time.time() - t0, 2)
    calls = prompts.parse_calls(text)
    if not isinstance(calls, list):
        calls = []
    enc, errs = kernel.apply_calls(task["board_b"], task["enclosure_a"], calls)
    checks = _check_with(task["board_b"], enc, None)
    types = {c["id"]: c["type"] for c in task["board_b"]["connectors"]}
    ops = [f"{c['args'].get('connector')} ({types.get(c['args'].get('connector'), '?')}) "
           f"{c['args'].get('tolerance')} mm/side" for c in calls if c.get("tool") == "place_opening"]
    out = {"who": who, "model": m.key, "checkpoint": (_rj(ck) or {}).get("path") if isinstance(ck, str) else None,
           "calls": calls, "tool_errors": errs,
           "checks": [{"id": c["id"], "pass": c["pass"], "detail": c.get("detail")} for c in checks],
           "pass": all(c["pass"] for c in checks), "latency_s": lat,
           "opening_summary": "; ".join(ops) or "no openings placed", "raw": text[:800]}
    if prof:  # also grade under the engineer's personal (e.g. field-unit) rule profile
        pc = _check_with(task["board_b"], enc, prof)
        out["personal_profile"] = prof
        out["personal_pass"] = all(c["pass"] for c in pc)
    return out


# ============================================================================ GBrain

def _gbrain_put(slug, title, md):
    try:
        from rev import gbrain_io
        return bool(gbrain_io.put_page(slug, title, md))
    except Exception:
        return False


def _lesson_md(eid, role, text, source, ts, lesson):
    return "\n".join([
        f"# Lesson from {role} ({eid})", "",
        f"- engineer: `{eid}` ({role})", f"- when: {ts}", f"- source: {source}",
        f"- rule: {lesson['type']} opening tolerance = {lesson['tolerance']} mm per side" if lesson else "- rule: (unparsed)",
        f"- company house value: {kernel.HOUSE['tolerance'].get(lesson['type'])} mm per side" if lesson else "",
        "", "## What they said", "", f"> {text}", "",
        f"Trains the personal LoRA branch `rev/data/engineers/{eid}/checkpoint.json` (not the company model).", ""])


# ============================================================================ teach (personal LoRA branch)

def _learn_child(eid, text, base_ckpt, out_ckpt, overrides_json):
    """Runs in a child process: learn.learn with the engineer's rule profile, JSON events on stdout."""
    from rev import learn
    ov = json.loads(overrides_json or "{}")
    kernel.HOUSE["tolerance"].update({k: float(v) for k, v in ov.items()})

    def emit(e):
        print("@@EV " + json.dumps(e, default=str), flush=True)

    learn.learn(text, emit=emit, promote=False, base_checkpoint_path=base_ckpt, out_path=out_ckpt)


def teach(engineer_id, text, source="chat", emit=None):
    from rev import learn
    meta = _meta(engineer_id)
    emit = emit or (lambda e: print(json.dumps({k: v for k, v in e.items() if k != "examples"}, default=str), flush=True))
    t0 = time.time()
    lesson = learn.parse_correction(text)
    if not lesson:
        r = {"ok": False, "error": "could not parse correction (need connector type + mm)", "before": None,
             "after": None, "seconds": 0.0, "gbrain_slug": None}
        emit({"type": "learned", **r})
        return r
    ts = _now()
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = f"engineers/{engineer_id}/lessons/{stamp}-{lesson['type']}"
    ok_g = _gbrain_put(slug, f"{meta['role']}: {lesson['type']} {lesson['tolerance']} mm/side",
                       _lesson_md(engineer_id, meta["role"], text, source, ts, lesson))
    emit({"type": "learn", "stage": "verify", "msg": f"GBrain {'<- ' if ok_g else 'write FAILED '}{slug}",
          "t": round(time.time() - t0, 1)})
    d = _edir(engineer_id)
    d.mkdir(parents=True, exist_ok=True)
    base = engineer_checkpoint(engineer_id) or str(COMPANY_CKPT)
    tmp_out = d / f"checkpoint.new-{stamp}.json"
    overrides = dict(_profile(engineer_id))
    if abs(float(lesson["tolerance"]) - kernel.HOUSE["tolerance"].get(lesson["type"], -1)) > 1e-6:
        overrides[lesson["type"]] = float(lesson["tolerance"])  # personal rule: gate lessons with it
    emit({"type": "learn", "stage": "train", "msg": f"continuing {engineer_id}'s branch from "
          f"{'personal checkpoint' if base != str(COMPANY_CKPT) else 'company v1'}"
          + (f" (personal rule profile {overrides})" if overrides else ""), "t": round(time.time() - t0, 1)})
    code = ("import sys; sys.path.insert(0, %r); from rev import engineers as E; "
            "E._learn_child(*sys.argv[1:])" % str(ROOT))
    proc = subprocess.Popen([PY, "-c", code, engineer_id, text, base, str(tmp_out), json.dumps(overrides)],
                            cwd=str(ROOT), env={**os.environ, "HF_HUB_OFFLINE": "1", "PYTHONUNBUFFERED": "1"}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    final = None
    try:
        for line in proc.stdout:
            if not line.startswith("@@EV "):
                continue
            try:
                e = json.loads(line[5:])
            except ValueError:
                continue
            if e.get("type") == "learned":
                final = e
            else:
                e["engineer"] = engineer_id
                emit(e)
        proc.wait()
    except KeyboardInterrupt:
        proc.kill()
        raise
    if not final or not final.get("ok") or not tmp_out.exists():
        r = {"ok": False, "error": (final or {}).get("error", "training failed"), "before": (final or {}).get("before"),
             "after": None, "seconds": round(time.time() - t0, 1), "gbrain_slug": slug if ok_g else None}
        emit({"type": "learned", "engineer": engineer_id, **r})
        return r
    rec = _rj(tmp_out)
    rec.update({"engineer": engineer_id, "role": meta["role"], "lesson_text": text})
    _wj(d / "checkpoint.json", rec)
    tmp_out.unlink(missing_ok=True)
    with _io:
        les = _lessons(engineer_id)
        les.append({"type": lesson["type"], "tolerance": lesson["tolerance"], "text": text, "source": source,
                    "ts": ts, "gbrain_slug": slug if ok_g else None, "steps": final.get("steps"),
                    "checkpoint": rec["path"], "before": final.get("before"), "after": final.get("after"),
                    "seconds": round(time.time() - t0, 1), "eval_profile": overrides or "company"})
        _wj(d / "lessons.json", les)
    r = {"ok": True, "engineer": engineer_id, "lesson": lesson, "before": final.get("before"),
         "after": final.get("after"), "seconds": round(time.time() - t0, 1), "gbrain_slug": slug if ok_g else None,
         "checkpoint": rec["path"], "eval_profile": overrides or "company", "losses": final.get("losses")}
    emit({"type": "learned", **r})
    return r


# ============================================================================ record (capture watcher)

def record(engineer_id, directory="workstation", emit=None, auto_teach=True):
    from rev import watcher as W
    meta = _meta(engineer_id)
    emit = emit or (lambda e: print(json.dumps(e, default=str), flush=True))
    d = Path(directory)
    if not d.is_absolute():
        d = ROOT / d

    class _EngWatcher(W.Watcher):
        def capture(self, p, side, before_params, params, diff):
            rec = super().capture(p, side, before_params, params, diff)
            rec["actor"] = rec["by"] = engineer_id
            rec["role"] = meta["role"]
            slug = f"engineers/{engineer_id}/captures/{rec['id']}"
            rec["gbrain_slug"] = slug
            if not rec["verified"]:
                why = "; ".join(v["detail"] for v in rec["verification"] if not v["pass"])
                emit({"type": "rejected", "engineer": engineer_id, "id": rec["id"], "file": rec["file"], "reason": why})
                return rec
            rule = rec.get("inferred_rule")
            emit({"type": "capture", "engineer": engineer_id, "id": rec["id"], "file": rec["file"],
                  "rule": rule, "calls": rec["calls"]})
            if not rule:
                return rec

            def _after():
                ok = _gbrain_put(slug, f"{meta['role']} capture: {rec['product']} {rec['feature']}",
                                 W.capture_markdown(rec).replace(W.ACTOR, engineer_id))
                emit({"type": "capture_gbrain", "engineer": engineer_id, "slug": slug, "ok": ok})
                if auto_teach:
                    txt = f"{rule['type'].replace('_', '-').upper()} cutouts get {rule['tolerance']} mm per side " \
                          f"(captured from {engineer_id}'s CAD save of {rec['file']})."
                    teach(engineer_id, txt, source="cad", emit=emit)
            threading.Thread(target=_after, daemon=True).start()
            return rec

    w = _EngWatcher(directory=d, captures_path=_edir(engineer_id) / "captures.jsonl", gbrain=False, emit=lambda e: None, auto_learn=False, quiet=True)
    d.mkdir(parents=True, exist_ok=True)
    emit({"type": "recording", "engineer": engineer_id, "role": meta["role"], "dir": str(d)})
    try:
        while True:
            w.tick()
            time.sleep(W.POLL_S)
    except KeyboardInterrupt:
        w.stop()
        emit({"type": "stopped", "engineer": engineer_id, "captures": len(w.captures)})
        return {"engineer": engineer_id, "captures": len(w.captures)}


# ============================================================================ merge into the company model

def merge(engineer_id, emit=None):
    meta = _meta(engineer_id)
    les = _lessons(engineer_id)
    if not les:
        return {"status": "nothing_to_merge", "engineer": engineer_id}
    last = les[-1]
    try:
        from rev import weights_ci
    except Exception:
        weights_ci = None
    if weights_ci is not None and hasattr(weights_ci, "open_pr"):
        src = "slack" if last.get("source") == "chat" else (last.get("source") or "chat")
        r = weights_ci.open_pr(last["text"], author_role=meta["role"], source=src, emit=emit)
        if isinstance(r, dict):
            r.setdefault("engineer", engineer_id)
        return r
    return {"status": "pr_prepared", "engineer": engineer_id, "author": meta["role"], "text": last["text"],
            "lesson": {"type": last["type"], "tolerance": last["tolerance"]}, "from_checkpoint": last.get("checkpoint")}


# ============================================================================ rehearsal resets

def snapshot_pristine(engineer_id=None):
    ids = [engineer_id] if engineer_id else [e["id"] for e in _roster()["engineers"]]
    for eid in ids:
        src, dst = _edir(eid), PRISTINE_DIR / eid
        if dst.exists():
            shutil.rmtree(dst)
        dst.mkdir(parents=True, exist_ok=True)
        for f in ("checkpoint.json", "lessons.json"):
            if (src / f).exists():
                shutil.copy2(src / f, dst / f)
    return ids


def reset_engineer(engineer_id):
    _meta(engineer_id)
    d, p = _edir(engineer_id), PRISTINE_DIR / engineer_id
    d.mkdir(parents=True, exist_ok=True)
    for f in ("checkpoint.json", "lessons.json"):
        if (p / f).exists():
            shutil.copy2(p / f, d / f)
        elif (d / f).exists():
            (d / f).unlink()
    return {"engineer": engineer_id, "checkpoint": (_rj(d / "checkpoint.json") or {}).get("path"),
            "lessons": len(_lessons(engineer_id))}


def reset_all():
    return [reset_engineer(e["id"]) for e in _roster()["engineers"]]


# ============================================================================ CLI

def main():
    ap = argparse.ArgumentParser(prog="rev.engineers")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("list")
    t = sp.add_parser("teach"); t.add_argument("engineer"); t.add_argument("text"); t.add_argument("--source", default="chat")
    r = sp.add_parser("refit"); r.add_argument("--who", default="senior-me"); r.add_argument("--model", default="9b")
    e = sp.add_parser("test"); e.add_argument("--model", default="9b"); e.add_argument("--variant", default="untrained")
    e.add_argument("--engineer"); e.add_argument("--suite", default="heldout"); e.add_argument("--limit", type=int)
    e.add_argument("--workers", type=int, default=12)
    rc = sp.add_parser("record"); rc.add_argument("engineer"); rc.add_argument("--dir", default="workstation")
    rc.add_argument("--no-teach", action="store_true")
    m = sp.add_parser("merge"); m.add_argument("engineer")
    rs = sp.add_parser("reset"); rs.add_argument("--engineer")
    sn = sp.add_parser("snapshot"); sn.add_argument("--engineer")
    a = ap.parse_args()
    if a.cmd == "list":
        print(json.dumps(list_engineers(), indent=1))
    elif a.cmd == "teach":
        print(json.dumps(teach(a.engineer, a.text, a.source), indent=1, default=str))
    elif a.cmd == "refit":
        print(json.dumps({k: v for k, v in refit_as(a.who, "C", a.model).items() if k != "raw"}, indent=1))
    elif a.cmd == "test":
        print(json.dumps(test(a.model, a.variant, a.engineer, a.suite, a.limit, a.workers,
                              emit=lambda ev: print(("PASS " if ev["pass"] else "FAIL ") + ev["task"] + " " + ",".join(ev["failed"]), flush=True)), indent=1))
    elif a.cmd == "record":
        record(a.engineer, a.dir, auto_teach=not a.no_teach)
    elif a.cmd == "merge":
        print(json.dumps(merge(a.engineer), indent=1, default=str))
    elif a.cmd == "reset":
        print(json.dumps(reset_engineer(a.engineer) if a.engineer else reset_all(), indent=1))
    elif a.cmd == "snapshot":
        print(snapshot_pristine(a.engineer))


if __name__ == "__main__":
    main()
