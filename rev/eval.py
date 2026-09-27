"""Evaluate base vs River-tuned model on the 30 held-out engineering changes (rev/data/test.jsonl).

Run from repo root:
    /opt/anaconda3/bin/python3 -m rev.eval --model base [--limit N] [--workers 8]
    /opt/anaconda3/bin/python3 -m rev.eval --model tuned [--limit N]
    /opt/anaconda3/bin/python3 -m rev.eval --demo 3            # demo scenario x3 through tuned model

Grading is purely the deterministic kernel checker: parse_calls -> apply_calls(board_b, enclosure_a) -> passes.
Results merge into rev/data/eval_results.json (SPEC section 8).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rev import kernel, prompts, tasks
from rev import river_util as ru

RESULTS_PATH = ru.DATA_DIR / "eval_results.json"
CHECK_IDS = ["cavity_fit", "cavity_height", "standoffs", "openings", "orphans"]
MAX_TOKENS = 768  # longest gold completion is ~340 chars (~150 tokens); generous margin for base-model rambling
_io_lock = threading.Lock()


def short_model(name: str) -> str:
    return name.split("/")[-1]


def sample(which: str, prompt_text: str, checkpoint=None) -> str:
    kw = {"max_tokens": MAX_TOKENS, "temperature": 0.0}
    if which == "base":
        return ru.sample_base(prompt_text, **kw)
    return ru.sample_tuned(prompt_text, checkpoint=checkpoint, **kw)


def sample_with_retry(which: str, prompt_text: str, checkpoint=None, tries: int = 6) -> str:
    delay = 2.0
    for i in range(tries):
        try:
            return sample(which, prompt_text, checkpoint)
        except Exception as e:  # rate limits / transient errors -> back off
            msg = str(e).lower()
            if "auth" in msg and "rate" not in msg:
                raise
            if i == tries - 1:
                raise
            time.sleep(delay + random.random())
            delay = min(delay * 2, 30)
    raise RuntimeError("unreachable")


def grade(task: dict, text: str) -> dict:
    calls = prompts.parse_calls(text)
    parse_error = None
    errors: list[str] = []
    if calls is None:
        parse_error = "no JSON array of tool calls found"
        calls = []
    enc, errors = kernel.apply_calls(task["board_b"], task["enclosure_a"], calls)
    checks = kernel.check(task["board_b"], enc)
    return {
        "pass": all(c["pass"] for c in checks),
        "checks": {c["id"]: bool(c["pass"]) for c in checks},
        "failed": [c["id"] for c in checks if not c["pass"]],
        "failed_detail": {c["id"]: c.get("detail") for c in checks if not c["pass"]},
        "parse_error": parse_error,
        "tool_errors": errors,
        "n_calls": len(calls),
        "calls": calls,
    }


def run_one(which: str, task: dict, checkpoint=None) -> dict:
    prompt_text = ru.render(prompts.messages(task))
    t0 = time.time()
    try:
        text = sample_with_retry(which, prompt_text, checkpoint)
        err = None
    except Exception as e:
        text, err = "", f"{type(e).__name__}: {e}"
    lat = time.time() - t0
    g = grade(task, text)
    g.update({"id": task.get("id", "demo"), "latency_s": round(lat, 2), "raw": text[:2000], "sample_error": err,
              "gold": task.get("gold_calls")})
    return g


def run_eval(which: str, rows: list[dict], workers: int = 8, checkpoint=None) -> list[dict]:
    if which == "tuned" and checkpoint is None:
        checkpoint = ru.load_checkpoint_ref()
    results: list[dict | None] = [None] * len(rows)

    def job(i):
        r = run_one(which, rows[i], checkpoint)
        results[i] = r
        with _io_lock:
            print(f"[{which}] {r['id']}: {'PASS' if r['pass'] else 'FAIL ' + ','.join(r['failed'])}"
                  f"{' PARSE' if r['parse_error'] else ''}{' ERR ' + r['sample_error'] if r['sample_error'] else ''}"
                  f" ({r['latency_s']}s)", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(job, range(len(rows))))
    return results  # type: ignore[return-value]


def summarize(which: str, results: list[dict]) -> dict:
    ckpt_info = {}
    if which == "tuned":
        try:
            ckpt_info = json.loads(ru.CHECKPOINT_PATH.read_text())
        except Exception:
            pass
    label = f"Base {short_model(ru.BASE_MODEL)}" if which == "base" else f"Acme-tuned {short_model(ru.BASE_MODEL)}"
    return {
        "name": which,
        "label": label,
        "passed": sum(r["pass"] for r in results),
        "total": len(results),
        "by_check": {cid: sum(r["checks"].get(cid, False) for r in results) for cid in CHECK_IDS},
        "parse_failures": sum(1 for r in results if r["parse_error"]),
        "sample_errors": sum(1 for r in results if r["sample_error"]),
        "mean_latency_s": round(sum(r["latency_s"] for r in results) / max(1, len(results)), 2),
        "checkpoint": ckpt_info.get("path") if which == "tuned" else None,
        "evaluated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def load_results() -> dict:
    try:
        return json.loads(RESULTS_PATH.read_text())
    except (OSError, ValueError):
        return {}


def write_results(data: dict) -> None:
    tmp = RESULTS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, RESULTS_PATH)


def _slim(r: dict) -> dict:
    return {"pass": r["pass"], "failed": r["failed"], "parse_error": r["parse_error"], "calls": r["calls"],
            "raw": r["raw"][:600]}


def rebuild_examples(data: dict) -> list[dict]:
    det = data.get("details", {})
    base = {r["id"]: r for r in det.get("base", [])}
    tuned = {r["id"]: r for r in det.get("tuned", [])}
    ids = [i for i in tuned if i in base]
    if not ids:
        return data.get("examples", [])
    # prefer flips (base fail -> tuned pass), then others
    flips = [i for i in ids if tuned[i]["pass"] and not base[i]["pass"]]
    others = [i for i in ids if i not in flips]
    picked = (flips[:4] + others[:2])[:5]
    test_rows = {t["id"]: t for t in tasks.load("test")}
    out = []
    for i in picked:
        out.append({
            "id": i,
            "change_summary": test_rows.get(i, {}).get("change_summary"),
            "gold": test_rows.get(i, {}).get("gold_calls"),
            "base": _slim(base[i]),
            "tuned": _slim(tuned[i]),
        })
    return out


def merge(which: str, results: list[dict]) -> dict:
    with _io_lock:
        data = load_results()
        data["base_model"] = ru.BASE_MODEL
        data["test_set"] = "rev/data/test.jsonl (30 held-out tasks, seed 2, disjoint from train)"
        run = summarize(which, results)
        runs = [r for r in data.get("runs", []) if r.get("name") != which]
        runs.append(run)
        runs.sort(key=lambda r: 0 if r["name"] == "base" else 1)
        data["runs"] = runs
        data.setdefault("details", {})[which] = results
        data["examples"] = rebuild_examples(data)
        write_results(data)
        return run


def run_demo(n: int = 3, which: str = "tuned") -> dict:
    demo = tasks.load("demo")
    demo = dict(demo, id="demo")
    ckpt = ru.load_checkpoint_ref() if which == "tuned" else None
    trials = []
    for _ in range(n):
        r = run_one(which, demo, ckpt)
        trials.append({"pass": r["pass"], "failed": r["failed"], "latency_s": r["latency_s"], "calls": r["calls"],
                       "raw": r["raw"][:1000], "sample_error": r["sample_error"]})
        print(f"[demo/{which}] {'PASS' if r['pass'] else 'FAIL ' + ','.join(r['failed'])} ({r['latency_s']}s)", flush=True)
    with _io_lock:
        data = load_results()
        data.setdefault("demo", {})[which] = {"passed": sum(t["pass"] for t in trials), "total": n, "trials": trials,
                                              "gold": demo["gold_calls"]}
        write_results(data)
    return data["demo"][which]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", choices=["base", "tuned"], default="base")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--demo", type=int, default=0, help="run the demo scenario N times instead of the test set")
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args()
    if a.demo:
        print(json.dumps({k: v for k, v in run_demo(a.demo, a.model).items() if k != "trials"}))
        return
    rows = tasks.load("test")
    if a.limit:
        rows = rows[: a.limit]
    t0 = time.time()
    results = run_eval(a.model, rows, a.workers)
    run = merge(a.model, results) if not a.no_write else summarize(a.model, results)
    print(json.dumps(run, indent=1))
    print(f"elapsed {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
