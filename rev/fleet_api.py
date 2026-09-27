"""FastAPI router for THE WALL (rev/fleet.py).

Integrate into rev/server.py:
    from rev.fleet_api import router as fleet_router, set_broadcast
    app.include_router(fleet_router)
    set_broadcast(<server broadcast fn taking one event dict>)   # optional: mirror fleet_* events onto /api/events

Standalone (no main server):  python3 -m uvicorn rev.fleet_api:app --port 8001   -> http://localhost:8001/fleet

Endpoints:
  GET  /fleet                      web/fleet.html
  GET  /api/fleet                  tasks + last results per run key
  POST /api/fleet/run              {"model":"base"|"tuned", "key"?: str, "cached"?: bool, "speed"?: float, "workers"?: int}
  GET  /api/fleet/events           SSE (data: JSON) fleet_snapshot on connect, then fleet_start / fleet_cell / fleet_done / fleet_error
"""
from __future__ import annotations

import asyncio
import json
import os
import threading

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from rev import fleet

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLEET_HTML = os.path.join(ROOT, "web", "fleet.html")

router = APIRouter()

_subs: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
_subs_lock = threading.Lock()
_broadcast = None
_state = {"run": None, "cells": {}}  # current/last run snapshot for late joiners
_state_lock = threading.Lock()


def set_broadcast(fn):
    """Optional: also push every fleet event into the main server's broadcast (e.g. /api/events)."""
    global _broadcast
    _broadcast = fn


def emit(event: dict):
    with _state_lock:
        t = event.get("type")
        if t == "fleet_start":
            _state["run"] = {k: v for k, v in event.items() if k != "type"}
            _state["run"]["done"] = None
            _state["cells"] = {}
        elif t == "fleet_cell":
            _state["cells"][event["id"]] = event
        elif t == "fleet_done" and _state["run"] is not None:
            _state["run"]["done"] = event
    with _subs_lock:
        subs = list(_subs)
    for loop, q in subs:
        try:
            loop.call_soon_threadsafe(q.put_nowait, event)
        except RuntimeError:
            pass
    if _broadcast is not None:
        try:
            _broadcast(event)
        except Exception:
            pass


def _worker(body: dict):
    model = body.get("model", "base")
    key = body.get("key") or model
    try:
        if body.get("cached"):
            fleet.replay_fleet(key, emit, speed=float(body.get("speed", 1.0)))
        else:
            fleet.run_fleet(model, emit, workers=int(body.get("workers", 16)), key=key)
    except Exception as e:  # surface to the wall instead of dying silently
        emit({"type": "fleet_error", "model": model, "key": key, "error": f"{type(e).__name__}: {e}"})


@router.get("/fleet")
def fleet_page():
    return FileResponse(FLEET_HTML, media_type="text/html")


@router.get("/api/fleet")
def fleet_get():
    return JSONResponse(fleet.summary())


@router.post("/api/fleet/run")
async def fleet_run(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body or {}
    model = body.get("model", "base")
    if model not in ("base", "tuned"):
        raise HTTPException(400, "model must be 'base' or 'tuned'")
    key = body.get("key") or model
    if fleet.is_running():
        raise HTTPException(409, "a fleet run is already in progress")
    if body.get("cached"):
        if key not in fleet.load_results():
            raise HTTPException(404, f"no cached fleet run for '{key}'")
    elif model == "tuned":
        if not os.path.exists(fleet.checkpoint_path_for(key)):
            raise HTTPException(409, "no trained checkpoint yet (rev/data/checkpoint.json)")
    threading.Thread(target=_worker, args=(body,), daemon=True).start()
    return {"ok": True, "model": model, "key": key, "cached": bool(body.get("cached"))}


@router.get("/api/fleet/events")
async def fleet_events(request: Request):
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    with _subs_lock:
        _subs.append((loop, q))
    with _state_lock:
        snap = {"type": "fleet_snapshot", "run": _state["run"], "cells": list(_state["cells"].values()),
                "running": fleet.is_running()}

    async def gen():
        try:
            yield f"data: {json.dumps(snap)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            with _subs_lock:
                if (loop, q) in _subs:
                    _subs.remove((loop, q))

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# Standalone app (only used when running this module directly with uvicorn).
app = FastAPI(title="REV fleet")
app.include_router(router)
