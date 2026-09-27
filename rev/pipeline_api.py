"""FastAPI router: GBrain (system of record) -> River (company model).

Wire into rev/server.py:
    from rev.pipeline_api import router as pipeline_router, set_broadcast as pipeline_set_broadcast
    app.include_router(pipeline_router); pipeline_set_broadcast(hub.publish)

Endpoints:
  POST /api/capture/learn  {"promote"?: bool=true, "steps"?: int, "type"?: str, "cached"?: bool, "dry_run"?: bool}
        -> 202 {"started": true}   (409 if a learn run is already going)
        runs gbrain_dataset.learn_from_gbrain in a background thread; events go out on /api/events:
        {"type":"learn","stage":"gbrain","msg":"pulled N verified captures from GBrain → lesson usb_a 0.5 mm","lesson",...}
        then the usual rev.learn events ({"type":"learn","stage":"verify|train|eval",...}, {"type":"learned",...}).
        Also recorded in /api/learn/status (shared with rev.learn_api, so the two can't run River concurrently).
  GET  /api/capture/lessons -> {"lessons":[{"type","tolerance","evidence","n"}],"conflicts":[...],"unverified":[...],"captures":N}
  GET  /api/gbrain/stats    -> {"history_records": int, "capture_records": int}
"""
from __future__ import annotations

import threading
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from rev import gbrain_dataset as gd
from rev import gbrain_history as gh

router = APIRouter()
_broadcast = None
_lock = threading.Lock()
_state = {"running": False, "result": None, "started_at": None}


def set_broadcast(fn):
    global _broadcast
    _broadcast = fn


def _learn_api():
    try:
        from rev import learn_api
        return learn_api
    except Exception:  # noqa: BLE001
        return None


def _emit(event: dict):
    la = _learn_api()
    if la is not None:
        try:
            la._emit(event)          # records into /api/learn/status + its SSE; broadcasts if the server wired it
            if getattr(la, "_broadcast", None) is not None:
                return
        except Exception:  # noqa: BLE001
            pass
    if _broadcast is not None:
        try:
            _broadcast(event)
        except Exception as e:  # noqa: BLE001
            print("pipeline_api broadcast failed:", e, flush=True)


def _claim() -> bool:
    la = _learn_api()
    with _lock:
        if _state["running"]:
            return False
        if la is not None:
            with la._lock:
                if la._state.get("running"):
                    return False
                la._state.update(running=True, lesson=None, events=[], result=None, started_at=time.time())
        _state.update(running=True, result=None, started_at=time.time())
        return True


def _release():
    la = _learn_api()
    if la is not None:
        with la._lock:
            la._state["running"] = False
    with _lock:
        _state["running"] = False


def _run(kw):
    try:
        _state["result"] = gd.learn_from_gbrain(emit=_emit, **kw)
    except Exception as e:  # noqa: BLE001
        _emit({"type": "learned", "ok": False, "error": f"{type(e).__name__}: {e}"})
    finally:
        _release()


@router.post("/api/capture/learn")
async def capture_learn(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    body = body or {}
    kw = {"promote": bool(body.get("promote", True)),
          "steps": int(body["steps"]) if body.get("steps") else None,
          "ctype": body.get("type") or None,
          "cached": bool(body.get("cached", False)),
          "dry_run": bool(body.get("dry_run", False))}
    if not _claim():
        raise HTTPException(409, "a learn run is already in progress")
    threading.Thread(target=_run, args=(kw,), daemon=True).start()
    return JSONResponse({"started": True, **kw}, status_code=202)


@router.get("/api/capture/lessons")
def capture_lessons():
    caps = gd.build_dataset_from_gbrain("captures", include_unverified=True)
    rep = gd.derive_lessons_report(caps)
    return {**rep, "captures": len(caps)}


@router.get("/api/gbrain/stats")
def gbrain_stats():
    return gh.stats()
