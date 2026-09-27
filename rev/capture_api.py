"""FastAPI router: workstation capture (REV learns from the senior engineer's real CAD edits).

    from rev.capture_api import router as capture_router; app.include_router(capture_router)

Endpoints:
  POST /api/capture/checkout {"source":"fleet"|"real_fleet"|"demo", "task_id"?, "open"?: bool, "key"?: "tuned"}
        -> writes workstation/<product>.scad (+ sidecar workstation/.rev/<file>.json) from the current tuned-model
           result enclosure (rev/data/fleet_results.json) if available, else enclosure_a refit with generic values.
        -> {"file", "path", "product", "task_id", "source", "enclosure_from", "opened"}
  POST /api/capture/event        body = watcher event -> broadcast on /api/events
                                 ({"type":"capture","record":{...}} | {"type":"capture_gbrain","id","slug","ok"})
  GET  /api/captures             {"captures": [...]} from rev/data/captures.jsonl (newest last)
  POST /api/capture/watcher/start {"dir"?, "gbrain"?: true, "auto_learn"?: false}  -> run watcher inside the server
  POST /api/capture/watcher/stop
  GET  /api/capture/watcher      {"running", "dir", "captures"}
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from rev import kernel, scad, watcher
from rev.tasks import LESSON_TYPES

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "rev" / "data"
GENERIC_TOL = 0.25   # what a generic tool would do for a connector the company has no rule for

router = APIRouter()
_broadcast = None
_w = {"watcher": None, "thread": None}
_wlock = threading.Lock()


def set_broadcast(fn):
    global _broadcast
    _broadcast = fn


def broadcast(ev: dict):
    fn = _broadcast
    if fn is None:  # fall back to the main server's SSE hub without a circular import
        srv = sys.modules.get("rev.server")
        hub = getattr(srv, "hub", None)
        fn = getattr(hub, "publish", None)
    if fn is not None:
        try:
            fn(ev)
        except Exception as e:  # noqa: BLE001
            print("capture broadcast failed:", e, flush=True)


# --------------------------------------------------------------------------- checkout
def _load(name):
    try:
        return json.loads((DATA_DIR / name).read_text())
    except Exception:
        return None


def _complete(board, enc):
    """Every connector gets an opening and every hole a standoff (generic values) so the engineer can edit them."""
    enc = copy.deepcopy(enc)
    have_o = {o["connector"] for o in enc.get("openings", [])}
    have_s = {s["hole"] for s in enc.get("standoffs", [])}
    calls = [{"tool": "place_standoff", "args": {"hole": h["id"]}} for h in board.get("holes", []) if h["id"] not in have_s]
    calls += [{"tool": "place_opening", "args": {"connector": c["id"], "tolerance": GENERIC_TOL}}
              for c in board.get("connectors", []) if c["id"] not in have_o]
    enc, _ = kernel.apply_calls(board, enc, calls)
    ids_o = {c["id"] for c in board.get("connectors", [])}
    ids_s = {h["id"] for h in board.get("holes", [])}
    enc["openings"] = [o for o in enc.get("openings", []) if o["connector"] in ids_o]
    enc["standoffs"] = [s for s in enc.get("standoffs", []) if s["hole"] in ids_s]
    return enc


def _generic_refit(task):
    """enclosure_a refit with generic values: the task's minimal edit, but new connector types get GENERIC_TOL."""
    board = task["board_b"]
    types = {c["id"]: c["type"] for c in board.get("connectors", [])}
    calls = []
    for c in task.get("gold_calls") or []:
        c = copy.deepcopy(c)
        if c.get("tool") == "place_opening" and types.get((c.get("args") or {}).get("connector")) in LESSON_TYPES:
            c["args"]["tolerance"] = GENERIC_TOL
        calls.append(c)
    enc, _ = kernel.apply_calls(board, task["enclosure_a"], calls)
    return enc


def pick_task(source="fleet", task_id=None):
    if source == "demo":
        d = _load("demo_rev_c.json") or _load("demo.json")
        if not d:
            raise ValueError("no demo data")
        task = dict(d, id="demo-rev-c", name=f"{d['board_b'].get('name', 'Sensor Hub')} Rev C")
        return task, "demo"
    order = ["fleet.json", "real_fleet.json"] if source != "real_fleet" else ["real_fleet.json", "fleet.json"]
    for fname in order:
        rows = _load(fname) or []
        if isinstance(rows, dict):
            rows = rows.get("tasks", [])
        if task_id:
            hit = [t for t in rows if t.get("id") == task_id]
        else:
            hit = [t for t in rows if t.get("usb_a")] or rows[:1]
        if hit:
            return hit[0], fname.replace(".json", "")
    raise ValueError(f"no task found (source={source}, task_id={task_id})")


def tuned_enclosure(task_id, key="tuned"):
    res = _load("fleet_results.json") or {}
    for k in ([key] if key else []) + ["tuned"]:
        cell = ((res.get(k) or {}).get("results") or {}).get(task_id)
        if cell and cell.get("enclosure"):
            return cell["enclosure"], f"fleet_results.json:{k}"
    return None, None


def checkout(source="fleet", task_id=None, directory=None, key="tuned", open_editor=False):
    task, src = pick_task(source, task_id)
    board = task["board_b"]
    enc, how = tuned_enclosure(task.get("id"), key) if src != "demo" else (None, None)
    if enc is None:
        enc, how = _generic_refit(task), f"enclosure_a refit (generic tolerance {GENERIC_TOL} for new connector types)"
    enc = _complete(board, enc)
    name = task.get("name") or board.get("name") or task.get("id")
    d = Path(directory) if directory else watcher.DEFAULT_DIR
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{watcher.slugify(name)}.scad"
    sc = watcher.sidecar_path(path)
    sc.parent.mkdir(parents=True, exist_ok=True)
    side = {"checkout_id": uuid.uuid4().hex[:12], "checked_out_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "product": {"id": task.get("id"), "name": name, "code": task.get("code"), "family": task.get("family"),
                        "source": src, "change_summary": task.get("change_summary")},
            "board": board, "base_enclosure": enc, "params": scad.to_params(board, enc), "enclosure_from": how}
    sc.write_text(json.dumps(side, indent=1))          # sidecar BEFORE the .scad so the watcher sees a clean baseline
    tmp = path.with_suffix(".scad.tmp")
    tmp.write_text(scad.export(board, enc, name))
    os.replace(tmp, path)
    opened = _open(path) if open_editor else False
    return {"file": path.name, "path": str(path), "product": name, "task_id": task.get("id"), "source": src,
            "enclosure_from": how, "opened": opened, "checks": kernel.check(board, enc)}


def _open(path):
    if sys.platform != "darwin":
        return False
    try:
        if Path("/Applications/Visual Studio Code.app").exists() or shutil.which("code"):
            subprocess.Popen(["open", "-a", "Visual Studio Code", str(path)])
        else:
            subprocess.Popen(["open", "-e", str(path)])
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- routes
async def _body(request: Request):
    try:
        b = await request.json()
        return b if isinstance(b, dict) else {}
    except Exception:
        return {}


@router.post("/api/capture/checkout")
async def api_checkout(request: Request):
    b = await _body(request)
    try:
        res = checkout(str(b.get("source") or ("real_fleet" if os.environ.get("FLEET_SOURCE", "real") == "real" else "fleet")), b.get("task_id"), key=b.get("key") or "tuned",
                       open_editor=bool(b.get("open", False)))
    except ValueError as e:
        raise HTTPException(404, str(e))
    broadcast({"type": "capture_checkout", "file": res["file"], "product": res["product"], "task_id": res["task_id"]})
    return res


@router.post("/api/capture/event")
async def api_capture_event(request: Request):
    ev = await _body(request)
    if ev.get("type") not in ("capture", "capture_gbrain"):
        ev = {"type": "capture", "record": ev}
    broadcast(ev)
    return {"ok": True}


@router.get("/api/captures")
def api_captures(limit: int = 200):
    return {"captures": watcher.read_captures(limit=limit)}


@router.post("/api/capture/watcher/start")
async def api_watcher_start(request: Request):
    b = await _body(request)
    with _wlock:
        w = _w["watcher"]
        if w is not None and _w["thread"] is not None and _w["thread"].is_alive():
            return {"running": True, "dir": str(w.dir), "already": True}
        w = watcher.Watcher(b.get("dir") or watcher.DEFAULT_DIR, gbrain=bool(b.get("gbrain", True)),
                            emit=broadcast, auto_learn=bool(b.get("auto_learn", False)))
        t = threading.Thread(target=w.run, daemon=True, name="rev-capture-watcher")
        _w.update(watcher=w, thread=t)
        t.start()
    return {"running": True, "dir": str(w.dir)}


@router.post("/api/capture/watcher/stop")
def api_watcher_stop():
    with _wlock:
        w = _w["watcher"]
        if w is not None:
            w.stop()
        _w.update(watcher=None, thread=None)
    return {"running": False}


@router.get("/api/capture/watcher")
def api_watcher_status():
    w, t = _w["watcher"], _w["thread"]
    running = bool(w and t and t.is_alive())
    return {"running": running, "dir": str(w.dir) if w else None, "captures": len(w.captures) if w else 0}
