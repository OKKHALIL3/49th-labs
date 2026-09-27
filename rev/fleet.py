"""THE WALL: a whole product line (48 boards, each with a pending engineering change) refit in parallel.

  python3 -m rev.fleet build          # (re)write rev/data/fleet.json (held-out seed; 36 product-line + 12 USB-A boards)
  python3 -m rev.fleet run base       # live run of all 48 on River, prints wall-clock + pass count, caches results

Runtime API (used by rev/fleet_api.py):
  load_fleet() -> list[task]            each task has extra keys: "name", "code", "family", "usb_a" (bool)
  run_fleet(model, emit, workers=16, key=None) -> summary      emit(event_dict) for fleet_start / fleet_cell / fleet_done
  replay_fleet(key, emit, speed=1.0) -> summary                 replays cached REAL outputs with their stagger
  load_results() -> {key: {...}}                                rev/data/fleet_results.json
"""
from __future__ import annotations

import copy
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from rev import kernel, prompts, tasks

DATA_DIR = tasks.DATA_DIR
SYNTH_FLEET_PATH = os.path.join(DATA_DIR, "fleet.json")
REAL_FLEET_PATH = os.path.join(DATA_DIR, "real_fleet.json")  # 48 real open-source KiCad boards (see REAL_DATA.md)
FLEET_SOURCE = os.environ.get("FLEET_SOURCE", "real")
FLEET_PATH = REAL_FLEET_PATH if FLEET_SOURCE == "real" and os.path.exists(REAL_FLEET_PATH) else SYNTH_FLEET_PATH
RESULTS_PATH = os.path.join(DATA_DIR, "fleet_results_real.json" if FLEET_PATH == REAL_FLEET_PATH else "fleet_results.json")
FLEET_SEED = 4242
N_LINE = 36
N_USBA = 12

# Invented product families (no real brands). code -> family name
FAMILIES = [
    ("SH", "Sensor Hub"), ("GW", "Gateway"), ("MD", "Motor Driver"), ("LN", "LoRa Node"),
    ("CB", "Camera Bridge"), ("PM", "Power Monitor"), ("AI", "Audio Interface"), ("DC", "Display Controller"),
    ("RB", "Robot Base"), ("WS", "Weather Station"), ("DK", "Dev Kit"), ("SR", "Smart Relay"),
]


# ---------------------------------------------------------------- build

def _usb_a_task(rng, tid):
    """A product-line board whose Rev B ADDS a USB-A port (a connector type the company history never had)."""
    for _ in range(400):
        n_types = rng.choice([1, 2, 2, 3])
        types_a = rng.sample(tasks.TYPES, n_types)
        board_a = tasks.random_board(rng, types_a)
        if not board_a["connectors"]:
            continue
        board_b = copy.deepcopy(board_a)
        board_b["rev"] = "B"
        touched, summaries = set(), []
        extra = rng.choice([None, None, "resize", "move_connector", "move_hole"])
        if extra == "resize":
            s = tasks.ch_resize(rng, board_b)
        elif extra == "move_connector":
            s = tasks.ch_move_connector(rng, board_b, touched)
        elif extra == "move_hole":
            s = tasks.ch_move_hole(rng, board_b, touched)
        else:
            s = ""
        if s is None:
            continue
        if s:
            summaries.append(s)
        s = tasks.ch_add_connector(rng, board_b, touched, False, set(), types=["usb_a"])
        if not s:
            continue
        summaries.append(s)
        if not tasks.board_valid(board_b):
            continue
        enc_a = kernel.enclosure_for(board_a)
        gold = tasks.gold_calls(board_a, enc_a, board_b)
        enc_b, errs = kernel.apply_calls(board_b, enc_a, gold)
        if errs or not kernel.passes(board_b, enc_b) or kernel.passes(board_b, enc_a):
            continue
        summary = "; ".join(summaries)
        return {"id": tid, "board_a": board_a, "enclosure_a": enc_a, "board_b": board_b,
                "change_summary": summary[0].upper() + summary[1:], "gold_calls": gold,
                "kinds": ([extra] if extra else []) + ["add_connector"], "new_type_added": True}
    raise RuntimeError("could not generate usb_a task")


def build(seed=FLEET_SEED):
    have_usba = "usb_a" in kernel.CONNECTOR_SPECS
    exclude = set()
    for split in ("train", "test"):
        try:
            exclude |= {tasks._key(t) for t in tasks.load(split)}
        except OSError:
            pass
    line = tasks.generate(N_LINE if have_usba else N_LINE + N_USBA, seed, "fleet", exclude=exclude)
    rng = random.Random(seed + 1)
    usba = [_usb_a_task(rng, f"fleet-u{i:02d}") for i in range(N_USBA)] if have_usba else []
    rows = line + usba
    # interleave: spread the USB-A boards across the wall (deterministic)
    order = list(range(len(rows)))
    random.Random(seed + 2).shuffle(order)
    rows = [rows[i] for i in order]
    counters = {}
    fam_rng = random.Random(seed + 3)
    out = []
    for i, t in enumerate(rows):
        code, fam = fam_rng.choice(FAMILIES)
        counters[code] = counters.get(code, 0) + 1
        name = f"{code}-{counters[code]:02d} {fam}"
        t = dict(t)
        t["id"] = f"fleet-{i:02d}"
        t["name"] = name
        t["code"] = f"{code}-{counters[code]:02d}"
        t["family"] = fam
        for k in ("board_a", "board_b"):
            t[k] = dict(t[k], name=name)
        t["usb_a"] = any(c["type"] == "usb_a" for c in t["board_b"]["connectors"])
        # invariants
        enc_b, errs = kernel.apply_calls(t["board_b"], t["enclosure_a"], t["gold_calls"])
        assert not errs and kernel.passes(t["board_b"], enc_b), t["id"]
        assert not kernel.passes(t["board_b"], t["enclosure_a"]), t["id"]
        out.append(t)
    return out


def write_fleet(seed=FLEET_SEED):
    rows = build(seed)
    with open(SYNTH_FLEET_PATH, "w") as f:
        json.dump(rows, f, separators=(",", ":"))
    return rows


_fleet_cache = {"mtime": None, "rows": None}


def load_fleet():
    m = os.path.getmtime(FLEET_PATH)
    if _fleet_cache["mtime"] != m:
        with open(FLEET_PATH) as f:
            _fleet_cache["rows"] = json.load(f)
        _fleet_cache["mtime"] = m
    return _fleet_cache["rows"]


# ---------------------------------------------------------------- results cache

_res_lock = threading.Lock()


def load_results():
    try:
        with open(RESULTS_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_result(key, rec):
    with _res_lock:
        allr = load_results()
        allr[key] = rec
        tmp = RESULTS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(allr, f, separators=(",", ":"))
        os.replace(tmp, RESULTS_PATH)


# ---------------------------------------------------------------- run

MODEL_LABELS = {"base": "Generic Qwen3.5-9B", "tuned": "Acme's own model v1", "tuned_v2": "v2 — after 1 correction"}


def checkpoint_path_for(key):
    """Which weights a tuned run uses: key 'tuned_v2' -> checkpoint_v2.json (the post-correction weights) if present,
    otherwise the active tuned checkpoint (checkpoint_current.json if promoted, else checkpoint.json)."""
    from rev import river_util as ru
    active = ru.active_checkpoint_path() if hasattr(ru, "active_checkpoint_path") else ru.CHECKPOINT_PATH
    if key and "v2" in key:
        v2 = ru.DATA_DIR / "checkpoint_v2.json"
        if v2.exists():
            return v2
    return active


def _sampler(model, key=None):
    from rev import river_util as ru
    if model == "base":
        return ru.sample_base, None
    path = checkpoint_path_for(key)
    ckpt = ru.load_checkpoint_ref(path)  # read once per run so all 48 agents share ONE set of weights
    return (lambda p, **kw: ru.sample_tuned(p, checkpoint=ckpt, **kw)), path


def _sample_retry(fn, prompt, attempts=5):
    delay = 1.5
    last = None
    for a in range(attempts):
        try:
            return fn(prompt, max_tokens=700)
        except Exception as e:  # rate limits / transient network
            last = e
            name = type(e).__name__
            if name in ("AuthenticationError", "ModelNotFoundError"):
                raise
            time.sleep(delay + random.random())
            delay *= 2
    raise last


def evaluate(task, text):
    calls = prompts.parse_calls(text)
    if calls is None:
        calls, errors = [], ["could not parse tool calls from model output"]
        enc = copy.deepcopy(task["enclosure_a"])
    else:
        enc, errors = kernel.apply_calls(task["board_b"], task["enclosure_a"], calls)
    checks = kernel.check(task["board_b"], enc)
    failed = [c["id"] for c in checks if not c["pass"]]
    refs = sorted({r for c in checks if not c["pass"] for r in c["refs"]})
    return {"calls": calls, "errors": errors, "enclosure": enc, "checks": checks, "failed": failed, "refs": refs,
            "pass": not failed}


def cell_event(task, r, cached=False):
    return {"type": "fleet_cell", "id": task["id"], "name": task["name"],
            "status": "pass" if r["pass"] else "fail", "failed": r["failed"], "refs": r["refs"],
            "latency_s": r.get("latency_s"), "calls": r["calls"], "errors": r["errors"], "checks": r["checks"],
            "enclosure": r["enclosure"], "usb_a": task.get("usb_a", False), "cached": cached}


_run_lock = threading.Lock()


def is_running():
    return _run_lock.locked()


def run_fleet(model, emit, workers=16, key=None, limit=None):
    """Live: all tasks concurrently against River. emit(event) is called from worker threads."""
    key = key or model
    rows = load_fleet()[:limit] if limit else load_fleet()
    if not _run_lock.acquire(blocking=False):
        raise RuntimeError("a fleet run is already in progress")
    try:
        from rev import river_util as ru
        sample, ckpt_path = _sampler(model, key)
        prompts_ = {t["id"]: ru.render(prompts.messages(t)) for t in rows}
        t0 = time.time()
        emit({"type": "fleet_start", "model": model, "key": key, "label": MODEL_LABELS.get(key, model),
              "n": len(rows), "cached": False, "ids": [t["id"] for t in rows]})
        for t in rows:
            emit({"type": "fleet_cell", "id": t["id"], "name": t["name"], "status": "running", "failed": [], "refs": []})
        results = {}

        def one(t):
            s = time.time()
            try:
                text = _sample_retry(sample, prompts_[t["id"]])
                err = None
            except Exception as e:
                text, err = "", f"{type(e).__name__}: {e}"
            r = evaluate(t, text)
            if err:
                r["errors"] = [err] + r["errors"]
            r["latency_s"] = round(time.time() - s, 2)
            r["t_done"] = round(time.time() - t0, 2)
            r["text"] = text
            return t, r

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(one, t) for t in rows]
            for f in as_completed(futs):
                t, r = f.result()
                results[t["id"]] = r
                emit(cell_event(t, r))
        secs = round(time.time() - t0, 2)
        passed = sum(r["pass"] for r in results.values())
        rec = {"model": model, "key": key, "label": MODEL_LABELS.get(key, model), "passed": passed,
               "total": len(rows), "seconds": secs, "workers": workers, "at": time.time(),
               "base_model": ru.BASE_MODEL, "results": results}
        if ckpt_path is not None:
            try:
                rec["checkpoint"] = dict(json.load(open(ckpt_path)), file=os.path.basename(str(ckpt_path)))
            except Exception:
                pass
        if not limit:
            save_result(key, rec)
        done = {"type": "fleet_done", "model": model, "key": key, "label": rec["label"], "passed": passed,
                "total": len(rows), "seconds": secs, "cached": False}
        emit(done)
        return done
    finally:
        _run_lock.release()


def replay_fleet(key, emit, speed=1.0):
    """Replay a cached REAL run: same outputs, same completion order/timing (scaled by 1/speed)."""
    rec = load_results().get(key)
    if not rec:
        raise KeyError(f"no cached fleet run for '{key}'")
    if not _run_lock.acquire(blocking=False):
        raise RuntimeError("a fleet run is already in progress")
    try:
        rows = {t["id"]: t for t in load_fleet()}
        ids = [i for i in rec["results"] if i in rows]
        emit({"type": "fleet_start", "model": rec["model"], "key": key, "label": rec.get("label", key),
              "n": len(ids), "cached": True, "ids": ids})
        for i in ids:
            emit({"type": "fleet_cell", "id": i, "name": rows[i]["name"], "status": "running", "failed": [], "refs": []})
        order = sorted(ids, key=lambda i: rec["results"][i].get("t_done", 0))
        t0 = time.time()
        for i in order:
            r = rec["results"][i]
            wait = r.get("t_done", 0) / max(speed, 1e-3) - (time.time() - t0)
            if wait > 0:
                time.sleep(wait)
            t = rows[i]
            r2 = evaluate(t, r.get("text", "")) if "text" in r else r  # re-grade with the current checker
            r2["latency_s"] = r.get("latency_s")
            emit(cell_event(t, r2, cached=True))
        done = {"type": "fleet_done", "model": rec["model"], "key": key, "label": rec.get("label", key),
                "passed": rec["passed"], "total": len(ids), "seconds": rec["seconds"], "cached": True}
        emit(done)
        return done
    finally:
        _run_lock.release()


def summary():
    """GET /api/fleet payload: tasks (board_b summaries, enclosure_a) + last results per key (enclosures, checks)."""
    rows = load_fleet()
    res = load_results()
    out_tasks = []
    for t in rows:
        out_tasks.append({"id": t["id"], "name": t["name"], "code": t["code"], "family": t["family"],
                          "usb_a": t.get("usb_a", False), "change_summary": t["change_summary"],
                          "board_a": t["board_a"], "board_b": t["board_b"], "enclosure_a": t["enclosure_a"],
                          "gold_calls": t["gold_calls"]})
    runs = {}
    for k, rec in res.items():
        runs[k] = {"model": rec["model"], "key": k, "label": rec.get("label", k), "passed": rec["passed"],
                   "total": rec["total"], "seconds": rec["seconds"], "at": rec.get("at"),
                   "cells": {i: {"status": "pass" if r["pass"] else "fail", "failed": r["failed"], "refs": r["refs"],
                                 "latency_s": r.get("latency_s"), "calls": r["calls"], "errors": r["errors"],
                                 "checks": r["checks"], "enclosure": r["enclosure"]}
                             for i, r in rec["results"].items()}}
    return {"n": len(rows), "labels": MODEL_LABELS, "tasks": out_tasks, "runs": runs, "running": is_running()}


def _cli():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    if cmd == "build":
        rows = write_fleet()
        print(f"wrote {FLEET_PATH}: {len(rows)} tasks, usb_a={sum(t['usb_a'] for t in rows)}")
        print(" ".join(t["name"] for t in rows[:8]), "...")
    elif cmd == "run":
        model = sys.argv[2] if len(sys.argv) > 2 else "base"
        workers = int(sys.argv[3]) if len(sys.argv) > 3 else 16
        limit = int(sys.argv[4]) if len(sys.argv) > 4 else None
        key = os.environ.get("FLEET_KEY") or model

        def emit(e):
            if e["type"] == "fleet_cell" and e["status"] != "running":
                print(f"  {e['id']} {e['name']:<24} {e['status']:<4} {e['latency_s']:>6}s {','.join(e['failed'])} "
                      f"{('ERR ' + e['errors'][0][:80]) if e.get('errors') else ''}", flush=True)
            elif e["type"] in ("fleet_start", "fleet_done"):
                print(json.dumps({k: v for k, v in e.items() if k != "ids"}), flush=True)
        run_fleet(model, emit, workers=workers, key=key, limit=limit)


if __name__ == "__main__":
    _cli()
