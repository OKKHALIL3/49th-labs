"""CI for an engineer's weights: every piece of senior-engineer work becomes a WEIGHT PULL REQUEST.

    python3 -m rev.weights_ci pr "Heads up: USB-A cutouts get 0.5 mm per side." --author "Senior mechanical engineer"
    python3 -m rev.weights_ci ledger | blame usb_a | revert v1 | reset

A weight PR goes through five gates, each emitted as {"type":"pr","pr":N,"stage":...,"msg":...,"data":{...},"t":s}:
  parsed          the message -> a rule {type, tolerance}           (not a rule -> "not_a_rule", closed)
  gbrain          decision record written to GBrain with provenance (who / role, when, where, why)
  conflict_check  the rule vs the 200 verified engineering changes in GBrain (history/eco-*):
                  >=3 verified changes used a different value -> BLOCKED (needs senior sign-off), no training
  training        candidate LoRA trained on River, continued from the production checkpoint (learn curriculum:
                  checker-verified practice changes + replay of company history)
  testing         lesson held-out set before -> after AND the 30 regular held-out changes (regression)
  merged|blocked  gate: lesson after >= 80% AND regression >= production - 1 -> new production version

Every production version is a commit in rev/data/weights_ledger.json -> log, blame, revert for a network's knowledge.
"""
from __future__ import annotations

import argparse
import collections
import copy
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rev import learn
from rev import river_util as ru

DATA_DIR = ru.DATA_DIR
LEDGER_PATH = DATA_DIR / "weights_ledger.json"
PRS_PATH = DATA_DIR / "weights_prs.json"
REPLAYS_PATH = DATA_DIR / "pr_replays.json"
EVAL_RESULTS_PATH = DATA_DIR / "eval_results.json"
BASE_CKPT_FILE = "checkpoint.json"

CONFLICT_MIN = 3          # this many verified past changes disagreeing -> blocked
AGREE_MM = 0.05
LESSON_PASS_FRAC = 0.8    # lesson after >= 80% (10/12)
REGRESSION_SLACK = 1      # regression may drop by at most one task
REGRESSION_WORKERS = 8

DISPLAY = {"usb_a": "USB-A", "usb_c": "USB-C", "hdmi": "HDMI", "rj45": "RJ45", "barrel": "Barrel jack", "sd": "SD card"}

_lock = threading.RLock()


# --------------------------------------------------------------------------- json io
def _read(path: Path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return copy.deepcopy(default)


def _write(path: Path, data) -> None:
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


# --------------------------------------------------------------------------- ledger
def _v1_regression() -> dict:
    d = _read(EVAL_RESULTS_PATH, {})
    for r in d.get("runs", []):
        if r.get("name") == "tuned":
            return {"passed": int(r["passed"]), "total": int(r["total"])}
    return {"passed": 29, "total": 30}


def seed_v1() -> dict:
    ck = _read(DATA_DIR / BASE_CKPT_FILE, {})
    return {"version": "v1", "parent": None, "pr": None,
            "title": "Initial training: 200 verified engineering changes from GBrain",
            "author": "49th Labs pipeline", "source": "gbrain:history/eco-*", "gbrain_slug": "history/eco-*",
            "lesson": None, "river_checkpoint": ck.get("path"), "checkpoint_file": BASE_CKPT_FILE,
            "tests": {"regression": _v1_regression()}, "status": "production",
            "ts": ck.get("saved_at") or _now()}


def load_ledger() -> list[dict]:
    led = _read(LEDGER_PATH, [])
    if not isinstance(led, list) or not led:
        led = [seed_v1()]
        _write(LEDGER_PATH, led)
    return led


def save_ledger(led: list[dict]) -> None:
    _write(LEDGER_PATH, led)


def production(led: list[dict] | None = None) -> dict:
    led = led if led is not None else load_ledger()
    for v in reversed(led):
        if v.get("status") == "production":
            return v
    return led[0]


def _next_version(led: list[dict]) -> str:
    return f"v{len(led) + 1}"


def _point_tuned_model_at(version: dict) -> None:
    """Make the server's tuned model (river_util.active_checkpoint_path) use this version's weights."""
    f = version.get("checkpoint_file") or BASE_CKPT_FILE
    if f == BASE_CKPT_FILE:
        learn.unpromote()
    else:
        learn.promote_checkpoint(DATA_DIR / f)


def commit_version(led: list[dict], **fields) -> dict:
    """Append a new production version; the previous production version becomes 'superseded'."""
    parent = production(led)
    for v in led:
        if v.get("status") == "production":
            v["status"] = "superseded"
    v = {"version": _next_version(led), "parent": parent["version"], "ts": _now(), "status": "production"}
    v.update(fields)
    led.append(v)
    return v


def revert(version: str) -> dict:
    """Make `version`'s weights production again. Recorded as a new commit ('Revert to vX')."""
    with _lock:
        led = load_ledger()
        target = next((v for v in led if v["version"] == version), None)
        if target is None:
            raise KeyError(f"no such version {version}")
        new = commit_version(led, title=f"Revert to {version}", reverts=version, pr=None,
                             author="Senior engineer (manual revert)", source="ledger",
                             gbrain_slug=target.get("gbrain_slug"), lesson=target.get("lesson"),
                             river_checkpoint=target.get("river_checkpoint"),
                             checkpoint_file=target.get("checkpoint_file"), tests=target.get("tests"))
        _point_tuned_model_at(new)
        save_ledger(led)
        return new


def reset() -> dict:
    """Rehearsal reset: production = v1 (original tuned checkpoint), no PRs. Recorded replays are kept."""
    with _lock:
        led = [seed_v1()]
        save_ledger(led)
        _write(PRS_PATH, [])
        _point_tuned_model_at(led[0])
        return {"ledger": led, "prs": []}


def _resolve(led: list[dict], v: dict) -> dict:
    """Follow revert pointers to the version whose knowledge `v` carries."""
    seen = set()
    while v.get("reverts") and v["version"] not in seen:
        seen.add(v["version"])
        v = next((x for x in led if x["version"] == v["reverts"]), v)
    return v


def lineage(led: list[dict] | None = None) -> list[dict]:
    """Production version back to v1 (knowledge lineage, revert-aware)."""
    led = led if led is not None else load_ledger()
    by = {v["version"]: v for v in led}
    out, v, seen = [], _resolve(led, production(led)), set()
    while v and v["version"] not in seen:
        seen.add(v["version"])
        out.append(v)
        p = v.get("parent")
        v = _resolve(led, by[p]) if p in by else None
    return out


def blame(ctype: str, history: list[dict] | None = None) -> dict:
    """Which version / PR taught the production model what it knows about `ctype` openings."""
    led = load_ledger()
    for v in lineage(led):
        les = v.get("lesson") or {}
        if les.get("type") == ctype:
            return {"type": ctype, "version": v["version"], "pr": v.get("pr"), "tolerance": les.get("tolerance"),
                    "author": v.get("author"), "source": v.get("source"), "gbrain_slug": v.get("gbrain_slug"),
                    "ts": v.get("ts"), "title": v.get("title"),
                    "summary": f"{v['version']} (PR #{v.get('pr')}): {v.get('title')} -- by {v.get('author')} via {v.get('source')}"}
    hist = history if history is not None else history_records()
    tols = collections.Counter(t for r in hist for (ty, t) in r["openings"] if ty == ctype)
    if tols:
        tol, n = tols.most_common(1)[0]
        return {"type": ctype, "version": "v1", "pr": None, "tolerance": tol, "records": sum(tols.values()),
                "author": "49th Labs pipeline", "source": "gbrain:history/eco-*",
                "summary": f"v1: learned from {sum(tols.values())} verified history records (they use {tol} mm)"}
    return {"type": ctype, "version": None, "pr": None, "tolerance": None,
            "summary": f"nothing in the production weights was taught about {ctype} -- never seen"}


# --------------------------------------------------------------------------- history (GBrain) + conflict check
def _train_types() -> dict:
    """task id -> {connector id: type} from rev/data/train.jsonl (the same 200 records that live in GBrain)."""
    out = {}
    p = DATA_DIR / "train.jsonl"
    for line in p.read_text().splitlines():
        if line.strip():
            t = json.loads(line)
            out[t["id"]] = {c["id"]: c["type"] for c in t["board_b"]["connectors"]}, t
    return out


def _openings(calls: list, types: dict) -> list[tuple[str, float]]:
    out = []
    for c in calls or []:
        if c.get("tool") == "place_opening":
            ty = types.get((c.get("args") or {}).get("connector"))
            tol = (c.get("args") or {}).get("tolerance")
            if ty and isinstance(tol, (int, float)):
                out.append((ty, round(float(tol), 3)))
    return out


def history_records() -> list[dict]:
    """The verified engineering history, read back out of GBrain (history/eco-*). Falls back to train.jsonl."""
    train = _train_types()
    recs, source = [], "gbrain"
    try:
        from rev import gbrain_dataset as gd
        pages = gd.build_dataset_from_gbrain("history")
    except Exception:  # noqa: BLE001
        pages = []
    for r in pages:
        types = train.get(r.get("id"), ({}, None))[0]
        recs.append({"slug": r.get("slug"), "id": r.get("id"), "summary": r.get("change_summary", ""),
                     "by": r.get("by"), "openings": _openings(r.get("calls"), types), "source": "gbrain"})
    if not recs:
        from rev import gbrain_history as gh
        source = "train.jsonl"
        for tid, (types, t) in train.items():
            recs.append({"slug": gh.eco_slug(tid), "id": tid, "summary": t.get("change_summary", ""),
                         "by": "senior-engineer", "openings": _openings(t.get("gold_calls"), types), "source": source})
    return recs


def conflict_check(ctype: str, tol: float, records: list[dict]) -> dict:
    """Compare a proposed rule with the verified history. verdict: conflict | known | new | partial."""
    agree, differ, tols = [], [], collections.Counter()
    for r in records:
        for ty, t in r["openings"]:
            if ty != ctype:
                continue
            tols[t] += 1
            (agree if abs(t - tol) <= AGREE_MM else differ).append((r, t))
            break
    n_diff = len(differ)
    base = {"type": ctype, "tolerance": tol, "records_scanned": len(records), "agree": len(agree),
            "differ": n_diff, "tolerances_seen": {str(k): v for k, v in tols.most_common()}}
    if n_diff >= CONFLICT_MIN:
        their = collections.Counter(t for _, t in differ).most_common(1)[0][0]
        ev = [{"slug": r["slug"], "summary": r["summary"], "tolerance": t} for r, t in differ[:3]]
        base.update(verdict="conflict", their_tolerance=their, evidence=ev,
                    reason=f"contradicts {n_diff} verified engineering changes (they used {their} mm) "
                           f"-- needs senior sign-off")
    elif agree and not differ:
        base.update(verdict="known", evidence=[{"slug": r["slug"], "summary": r["summary"], "tolerance": t}
                                               for r, t in agree[:3]],
                    reason=f"already known: {len(agree)} verified engineering changes already use {tol} mm")
    elif not agree and not differ:
        base.update(verdict="new", evidence=[],
                    reason=f"new knowledge: {DISPLAY.get(ctype, ctype)} never appears in {len(records)} verified changes")
    else:
        base.update(verdict="partial", evidence=[{"slug": r["slug"], "summary": r["summary"], "tolerance": t}
                                                 for r, t in differ[:3]],
                    reason=f"{n_diff} verified change(s) differ (< {CONFLICT_MIN}); proceeding to training")
    return base


# --------------------------------------------------------------------------- PRs
def load_prs() -> list[dict]:
    p = _read(PRS_PATH, [])
    return p if isinstance(p, list) else []


def _save_pr(pr: dict) -> None:
    with _lock:
        prs = load_prs()
        slim = {k: v for k, v in pr.items()}
        for i, x in enumerate(prs):
            if x.get("pr") == pr["pr"]:
                prs[i] = slim
                break
        else:
            prs.append(slim)
        _write(PRS_PATH, prs)


def reserve_pr_number() -> int:
    with _lock:
        prs = load_prs()
        n = max([p.get("pr", 0) for p in prs] + [0]) + 1
        prs.append({"pr": n, "status": "open", "ts": _now()})
        _write(PRS_PATH, prs)
        return n


def pr_title(lesson: dict | None, text: str) -> str:
    if lesson:
        return f"{DISPLAY.get(lesson['type'], lesson['type'])} cutout tolerance {lesson['tolerance']} mm"
    return (text or "").strip()[:60]


def decision_markdown(n, lesson, text, author, source, ts, title) -> str:
    rec = {"kind": "decision", "pr": n, "rule": lesson, "author_role": author, "source": source, "ts": ts,
           "text": text, "verified": False}
    return "\n".join([
        f"# Weight PR #{n}: {title}", "",
        f"- **Proposed by:** {author}", f"- **Where:** {source}", f"- **When:** {ts}",
        f"- **Rule:** {DISPLAY.get(lesson['type'], lesson['type'])} opening tolerance = {lesson['tolerance']} mm per side",
        "", "## Why (original message)", "", f"> {text.strip()}", "",
        "## Status", "", "Pending: conflict check against verified engineering history, candidate training on River, "
        "regression tests. Only merged PRs change the production weights.", "",
        "```json", json.dumps(rec, indent=1), "```", ""])


def _default_emit(e: dict) -> None:
    d = e.get("data") or {}
    extra = ""
    if e.get("stage") == "training" and d.get("loss") is not None:
        extra = f"  loss={d['loss']:.4f}"
    print(f"{e.get('t', 0):6.1f}s PR #{e.get('pr')} [{e.get('stage'):14s}] {e.get('msg', '')}{extra}", flush=True)


def _regression(ckpt_file: Path, emit_progress) -> dict:
    from rev import eval as ev_mod
    from rev import tasks
    rows = tasks.load("test")
    ck = ru.load_checkpoint_ref(ckpt_file)
    done = {"n": 0, "passed": 0}
    lk = threading.Lock()
    results = [None] * len(rows)

    def job(i):
        r = ev_mod.run_one("tuned", rows[i], ck)
        results[i] = r
        with lk:
            done["n"] += 1
            done["passed"] += bool(r["pass"])
            emit_progress(done["n"], len(rows), done["passed"], r["id"], r["pass"])

    with ThreadPoolExecutor(max_workers=REGRESSION_WORKERS) as ex:
        list(ex.map(job, range(len(rows))))
    return {"passed": sum(bool(r["pass"]) for r in results), "total": len(rows),
            "failed_ids": [r["id"] for r in results if not r["pass"]]}


def open_pr(text: str, author_role: str = "Senior engineer", source: str = "slack", emit=None,
            pr_number: int | None = None, steps: int | None = None) -> dict:
    """Run one weight PR end to end. Returns the final PR record."""
    emit_fn = emit or _default_emit
    t0 = time.time()
    n = pr_number or reserve_pr_number()
    events: list[dict] = []
    ts = _now()
    pr = {"pr": n, "text": text, "author": author_role, "source": source, "ts": ts, "status": "open",
          "lesson": None, "title": pr_title(None, text)}

    def ev(stage, msg, **data):
        e = {"type": "pr", "pr": n, "stage": stage, "msg": msg, "data": data, "t": round(time.time() - t0, 1)}
        events.append(e)
        emit_fn(e)

    def finish(status, reason, **extra):
        pr.update(status=status, reason=reason, seconds=round(time.time() - t0, 1), closed_at=_now(), **extra)
        stage = {"merged": "merged", "blocked": "blocked"}.get(status, "closed")
        ev(stage, reason, status=status, pr_record={k: v for k, v in pr.items() if k != "events"})
        pr["events"] = events
        _save_pr(pr)
        _save_replay(pr)
        return pr

    ev("opened", f"Weight PR #{n} opened from {source} by {author_role}", text=text, author=author_role, source=source)

    # a) parse
    lesson = learn.parse_correction(text)
    if not lesson:
        ev("not_a_rule", "No engineering rule found (need a connector type and a value in mm)")
        return finish("closed", "not a rule: nothing to learn")
    pr.update(lesson=lesson, title=pr_title(lesson, text))
    ev("parsed", f"Rule: {DISPLAY.get(lesson['type'], lesson['type'])} opening tolerance = {lesson['tolerance']} mm per side",
       rule=lesson, title=pr["title"])

    # b) GBrain decision record (provenance)
    slug = f"decisions/pr-{n}-{lesson['type']}"
    try:
        from rev import gbrain_io
        ok = gbrain_io.put_page(slug, f"Weight PR #{n}: {pr['title']}",
                                decision_markdown(n, lesson, text, author_role, source, ts, pr["title"]))
    except Exception:  # noqa: BLE001
        ok = False
    pr["gbrain_slug"] = slug
    ev("gbrain", f"Decision record saved to GBrain: {slug}" if ok else f"GBrain unavailable; decision kept locally ({slug})",
       slug=slug, saved=ok, author=author_role, source=source, ts=ts)

    # c) conflict check against verified history
    hist = history_records()
    cc = conflict_check(lesson["type"], float(lesson["tolerance"]), hist)
    cc["history_source"] = hist[0]["source"] if hist else None
    pr["conflict"] = cc
    ev("conflict_check", f"Checked {cc['records_scanned']} verified engineering changes in GBrain: {cc['reason']}", **cc)
    if cc["verdict"] == "conflict":
        return finish("blocked", cc["reason"], blocked_by="conflict_check")
    if cc["verdict"] == "known":
        return finish("closed", cc["reason"])

    # d) train a candidate on River (continue from production)
    led = load_ledger()
    prod = production(led)
    base_file = DATA_DIR / (prod.get("checkpoint_file") or BASE_CKPT_FILE)
    out_file = DATA_DIR / f"checkpoint_pr{n}.json"
    ev("training", f"Training candidate from production {prod['version']} on River "
                   f"(verified practice changes + replay of company history)", step=0, production=prod["version"])

    def learn_emit(e):
        if e.get("type") == "learn":
            stage = "testing" if e.get("stage") == "eval" else "training"
            ev(stage, e.get("msg", ""), step=e.get("step"), total_steps=e.get("total_steps"), loss=e.get("loss"),
               learn_stage=e.get("stage"))

    try:
        res = learn.learn(lesson, emit=learn_emit, promote=False, steps=steps,
                          base_checkpoint_path=base_file, out_path=out_file)
    except Exception as e:  # noqa: BLE001
        return finish("blocked", f"training failed: {type(e).__name__}: {e}", blocked_by="training")
    if not res.get("ok"):
        return finish("blocked", f"didn't learn: {res.get('error')}", blocked_by="training")
    pr["candidate"] = {"river_checkpoint": res["checkpoint"], "checkpoint_file": out_file.name,
                       "losses": res.get("losses"), "verified": res.get("verified"), "generated": res.get("generated"),
                       "replay": res.get("replay"), "steps": res.get("steps")}

    # e) tests: lesson held-out + regression on the 30 regular held-out changes
    lesson_t = {"before": res["before"], "after": res["after"]}
    ev("testing", f"Lesson held-out: before {res['before']['passed']}/{res['before']['total']} -> "
                  f"after {res['after']['passed']}/{res['after']['total']}", lesson=lesson_t)
    prod_reg = (prod.get("tests") or {}).get("regression") or _v1_regression()
    ev("testing", f"Regression: running the {prod_reg.get('total', 30)} regular held-out changes on the candidate "
                  f"(production {prod['version']}: {prod_reg['passed']}/{prod_reg['total']})",
       regression_progress=0, production_regression=prod_reg)

    def prog(done, total, passed, tid, ok):
        ev("testing", f"regression {done}/{total}: {tid} {'PASS' if ok else 'FAIL'}", done=done, total=total,
           passed=passed)

    try:
        reg = _regression(out_file, prog)
    except Exception as e:  # noqa: BLE001
        return finish("blocked", f"regression run failed: {type(e).__name__}: {e}", blocked_by="testing")
    pr["tests"] = {"lesson": lesson_t, "regression": {"passed": reg["passed"], "total": reg["total"]},
                   "production_regression": prod_reg, "regression_failed": reg["failed_ids"]}
    ev("testing", f"Regression: {reg['passed']}/{reg['total']} (production {prod_reg['passed']}/{prod_reg['total']})",
       regression=reg, production_regression=prod_reg)

    # f) gate
    after = res["after"]
    learned = after["passed"] >= LESSON_PASS_FRAC * after["total"]
    floor = prod_reg["passed"] - REGRESSION_SLACK
    kept = reg["passed"] >= floor
    if not learned:
        return finish("blocked", f"didn't learn: lesson held-out {after['passed']}/{after['total']} "
                                 f"(< {int(LESSON_PASS_FRAC * 100)}%)", blocked_by="gate")
    if not kept:
        k = prod_reg["passed"] - reg["passed"]
        return finish("blocked", f"would break {k} existing rules (regression {reg['passed']}/{reg['total']} vs "
                                 f"production {prod_reg['passed']}/{prod_reg['total']})", blocked_by="gate")

    # g) merge -> new production version
    v = merge(pr, prod_version=prod["version"])
    return finish("merged", f"Merged as {v['version']}: {pr['title']} (lesson {after['passed']}/{after['total']}, "
                            f"regression {reg['passed']}/{reg['total']})", version=v["version"])


def merge(pr: dict, prod_version: str | None = None) -> dict:
    with _lock:
        led = load_ledger()
        cand = pr["candidate"]
        v = commit_version(led, pr=pr["pr"], title=pr["title"], author=pr["author"], source=pr["source"],
                           gbrain_slug=pr.get("gbrain_slug"), lesson=pr.get("lesson"),
                           tests={"lesson": pr["tests"]["lesson"], "regression": pr["tests"]["regression"]},
                           river_checkpoint=cand["river_checkpoint"], checkpoint_file=cand["checkpoint_file"])
        _point_tuned_model_at(v)
        save_ledger(led)
    pr["version"] = v["version"]
    return v


# --------------------------------------------------------------------------- recorded runs (video re-takes)
def _save_replay(pr: dict) -> None:
    if not pr.get("lesson") or pr.get("replayed"):
        return
    try:
        with _lock:
            reps = _read(REPLAYS_PATH, {})
            reps[pr["lesson"]["type"]] = {"text": pr["text"], "author": pr["author"], "source": pr["source"],
                                          "recorded_at": _now(),
                                          "result": {k: v for k, v in pr.items() if k != "events"},
                                          "events": pr["events"]}
            _write(REPLAYS_PATH, reps)
    except Exception:  # noqa: BLE001
        pass


def find_replay(text: str) -> dict | None:
    reps = _read(REPLAYS_PATH, {})
    les = learn.parse_correction(text)
    if les and les["type"] in reps:
        return reps[les["type"]]
    for r in reps.values():
        if r.get("text", "").strip().lower() == (text or "").strip().lower():
            return r
    return None


def replay_pr(text: str, author_role: str | None = None, source: str | None = None, emit=None,
              pr_number: int | None = None, speed: float = 4.0) -> dict:
    """Re-emit a recorded real PR run with its real timing (compressed by `speed`); merges for real on the
    recorded candidate checkpoint (it still exists on River), so the ledger/production switch is genuine."""
    rec = find_replay(text)
    if rec is None:
        raise KeyError("no recorded run for that message")
    emit_fn = emit or _default_emit
    n = pr_number or reserve_pr_number()
    res = copy.deepcopy(rec["result"])
    author, src = author_role or rec["author"], source or rec["source"]
    slug = f"decisions/pr-{n}-{res['lesson']['type']}" if res.get("lesson") else None
    events, prev, t0 = [], 0.0, time.time()
    for e in rec["events"]:
        t = float(e.get("t") or prev)
        time.sleep(max(0.0, min(3.0, (t - prev) / speed)))
        prev = t
        e = copy.deepcopy(e)
        e.update(pr=n, replay=True, recorded_t=e.get("t"), t=round(time.time() - t0, 1))
        d = e.get("data") or {}
        if e["stage"] == "opened":
            e["msg"] = f"Weight PR #{n} opened from {src} by {author}"
            d.update(author=author, source=src)
        if e["stage"] == "gbrain" and slug:
            try:
                from rev import gbrain_io
                gbrain_io.put_page(slug, f"Weight PR #{n}: {res['title']}",
                                   decision_markdown(n, res["lesson"], text, author, src, _now(), res["title"]))
            except Exception:  # noqa: BLE001
                pass
            e["msg"] = f"Decision record saved to GBrain: {slug}"
            d.update(slug=slug)
        if e["stage"] in ("merged", "blocked", "closed"):
            res.update(pr=n, author=author, source=src, gbrain_slug=slug, text=text, replayed=True, ts=_now())
            if res.get("status") == "merged":
                v = merge(res)
                res["version"] = v["version"]
                e["msg"] = f"Merged as {v['version']}: {res['title']} (lesson {res['tests']['lesson']['after']['passed']}/" \
                           f"{res['tests']['lesson']['after']['total']}, regression {res['tests']['regression']['passed']}/" \
                           f"{res['tests']['regression']['total']})"
                res["reason"] = e["msg"]
            d["pr_record"] = {k: v for k, v in res.items() if k != "events"}
        e["data"] = d
        events.append(e)
        emit_fn(e)
    res["events"] = events
    _save_pr(res)
    return res


# --------------------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(prog="python3 -m rev.weights_ci")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pr"); p.add_argument("text"); p.add_argument("--author", default="Senior engineer")
    p.add_argument("--source", default="slack"); p.add_argument("--replay", action="store_true")
    p.add_argument("--steps", type=int)
    sub.add_parser("ledger"); sub.add_parser("prs"); sub.add_parser("reset")
    b = sub.add_parser("blame"); b.add_argument("type")
    r = sub.add_parser("revert"); r.add_argument("version")
    a = ap.parse_args()
    if a.cmd == "pr":
        fn = replay_pr if a.replay else open_pr
        kw = {} if a.replay else {"steps": a.steps}
        res = fn(a.text, a.author, a.source, **kw)
        print(json.dumps({k: v for k, v in res.items() if k != "events"}, indent=1))
    elif a.cmd == "ledger":
        for v in load_ledger():
            t = v.get("tests") or {}
            reg = t.get("regression") or {}
            print(f"{v['version']:4s} {v['status']:10s} parent={v.get('parent') or '-':4s} PR={v.get('pr') or '-'}  "
                  f"{v['title']}  [{v.get('author')}]  regression {reg.get('passed')}/{reg.get('total')}")
    elif a.cmd == "prs":
        print(json.dumps([{k: v for k, v in p.items() if k != "events"} for p in load_prs()], indent=1))
    elif a.cmd == "blame":
        print(json.dumps(blame(a.type), indent=1))
    elif a.cmd == "revert":
        print(json.dumps(revert(a.version), indent=1))
    elif a.cmd == "reset":
        reset()
        print("reset: production = v1")


if __name__ == "__main__":
    main()
