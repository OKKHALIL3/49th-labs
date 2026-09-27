"""FastAPI router for live one-shot learning (rev/learn.py).

Integrate into rev/server.py:
    from rev.learn_api import router as learn_router, set_broadcast as learn_set_broadcast
    app.include_router(learn_router)
    learn_set_broadcast(<server broadcast fn taking one event dict>)   # learn events go out on /api/events

Endpoints:
  POST /api/learn          {"text": str, "promote"?: bool=true, "steps"?: int, "cached"?: bool}
                           -> 202 {"started": true, "lesson": {"type","tolerance"}}   (400 not understood, 409 busy)
                           cached=true replays the last successful real run (rev/data/learn_last.json) and promotes
                           its checkpoint -- stage fallback only.
  GET  /api/learn/status   {"running", "lesson", "events": [...], "result": <learned event>|null, "active_checkpoint"}
  POST /api/learn/reset    un-promote (tuned model goes back to rev/data/checkpoint.json) -- for rehearsals
  GET  /api/learn/events   SSE fallback stream of the same events (if the server didn't wire set_broadcast)

Events (also pushed through the server broadcast):
  {"type":"learn","stage":"verify"|"train"|"eval","step":int,"total_steps":int,"loss":float|null,"msg":str,"t":float}
  {"type":"learned","ok":bool,"lesson":{...},"before":{"passed","total"},"after":{"passed","total"},"seconds":float,
   "verified","generated","replay","steps","losses":[...],"checkpoint","promoted":bool}
"""
from __future__ import annotations

import asyncio
import json
import threading
import time

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from rev import learn

router = APIRouter()
LAST_RUN_PATH = learn.DATA_DIR / "learn_last.json"

_broadcast = None
_subs: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
_lock = threading.Lock()
_state = {"running": False, "lesson": None, "events": [], "result": None, "started_at": None}


def set_broadcast(fn):
    """Push every learn event into the main server's SSE broadcast (/api/events)."""
    global _broadcast
    _broadcast = fn


def _emit(event: dict):
    with _lock:
        _state["events"].append(event)
        if event.get("type") == "learned":
            _state["result"] = event
        subs = list(_subs)
    for loop, q in subs:
        try:
            loop.call_soon_threadsafe(q.put_nowait, event)
        except RuntimeError:
            pass
    if _broadcast is not None:
        try:
            _broadcast(event)
        except Exception as e:  # noqa: BLE001
            print("learn_api broadcast failed:", e, flush=True)


def _run(text, promote, steps):
    events = []

    def emit(e):
        events.append(e)
        _emit(e)

    try:
        res = learn.learn(text, emit=emit, promote=promote, steps=steps)
        if res.get("ok"):
            try:
                LAST_RUN_PATH.write_text(json.dumps({"text": text, "events": events}, indent=1))
            except OSError:
                pass
    except Exception as e:  # noqa: BLE001
        _emit({"type": "learned", "ok": False, "error": f"{type(e).__name__}: {e}"})
    finally:
        with _lock:
            _state["running"] = False


def _replay(promote, speed=4.0):
    """Replay the last real learn run with compressed timing (stage fallback)."""
    try:
        rec = json.loads(LAST_RUN_PATH.read_text())
        prev_t = 0.0
        for e in rec["events"]:
            t = float(e.get("t", prev_t) or prev_t)
            time.sleep(max(0.0, min(3.0, (t - prev_t) / speed)))
            prev_t = t
            e = dict(e, replay=True)
            if e.get("type") == "learned" and promote and e.get("ok"):
                learn.promote_checkpoint(learn.DATA_DIR / e.get("checkpoint_file", learn.CKPT_V2_PATH.name))
                e["promoted"] = True
            _emit(e)
    except Exception as e:  # noqa: BLE001
        _emit({"type": "learned", "ok": False, "error": f"replay failed: {e}"})
    finally:
        with _lock:
            _state["running"] = False


@router.post("/api/learn")
async def api_learn(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    text = (body or {}).get("text") or ""
    lesson = learn.parse_correction(text)
    if not lesson:
        raise HTTPException(400, "Could not understand the correction: need a connector type and a tolerance in mm, "
                                 "e.g. 'Our USB-A cutouts always get 0.5 mm per side.'")
    promote = bool(body.get("promote", True))
    steps = body.get("steps")
    cached = bool(body.get("cached", False))
    if cached and not LAST_RUN_PATH.exists():
        raise HTTPException(404, "no recorded learn run to replay")
    with _lock:
        if _state["running"]:
            raise HTTPException(409, "a learn run is already in progress")
        _state.update(running=True, lesson=lesson, events=[], result=None, started_at=time.time())
    if cached:
        threading.Thread(target=_replay, args=(promote,), daemon=True).start()
    else:
        threading.Thread(target=_run, args=(text, promote, int(steps) if steps else None), daemon=True).start()
    return JSONResponse({"started": True, "lesson": lesson, "cached": cached}, status_code=202)


@router.get("/api/learn/status")
def api_learn_status():
    with _lock:
        st = dict(_state, events=list(_state["events"]))
    try:
        st["active_checkpoint"] = learn.current_checkpoint_path().name
    except Exception:  # noqa: BLE001
        st["active_checkpoint"] = None
    return st


@router.post("/api/learn/reset")
def api_learn_reset():
    with _lock:
        if _state["running"]:
            raise HTTPException(409, "a learn run is in progress")
        _state.update(lesson=None, events=[], result=None)
    return {"unpromoted": learn.unpromote(), "active_checkpoint": learn.current_checkpoint_path().name}


@router.get("/api/learn/events")
async def api_learn_events(request: Request):
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    with _lock:
        _subs.append((loop, q))

    async def gen():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    e = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(e)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            with _lock:
                if (loop, q) in _subs:
                    _subs.remove((loop, q))

    return StreamingResponse(gen(), media_type="text/event-stream")


# standalone: python3 -m uvicorn rev.learn_api:app --port 8002
app = FastAPI()
app.include_router(router)
