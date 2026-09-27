"""FastAPI router for weight pull requests (rev/weights_ci.py).

  POST /api/pr              {"text", "author", "source", "replay"?: bool, "speed"?: float} -> 202 {"pr": N}
                            replay=true re-emits the recorded real run (rev/data/pr_replays.json) with its real
                            timing / speed and merges the recorded candidate for real (video re-takes).
  GET  /api/prs             all PRs (newest last)
  GET  /api/ledger          {"versions": [...], "production": "vN"}
  GET  /api/blame?type=usb_a
  POST /api/ledger/revert   {"version": "v1"}
  POST /api/ledger/reset    production = v1, no PRs (rehearsals)

Events go out on the server broadcast (/api/events): {"type":"pr","pr":N,"stage":...,"msg":...,"data":{...},"t":s}
"""
from __future__ import annotations

import threading

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from rev import weights_ci as wci

router = APIRouter()
_broadcast = None
_busy = threading.Lock()


def set_broadcast(fn):
    global _broadcast
    _broadcast = fn


def _emit(e: dict):
    if _broadcast is not None:
        try:
            _broadcast(e)
        except Exception as ex:  # noqa: BLE001
            print("weights_ci broadcast failed:", ex, flush=True)


def _run(fn, n, *args, **kw):
    try:
        fn(*args, emit=_emit, pr_number=n, **kw)
    except Exception as e:  # noqa: BLE001
        _emit({"type": "pr", "pr": n, "stage": "blocked", "msg": f"PR failed: {type(e).__name__}: {e}",
               "data": {"status": "error"}})
    finally:
        _busy.release()


@router.post("/api/pr")
async def api_pr(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    text = (body or {}).get("text") or ""
    if not text.strip():
        raise HTTPException(400, "text required")
    author = body.get("author") or "Senior engineer"
    source = body.get("source") or "slack"
    replay = bool(body.get("replay", False))
    if replay and wci.find_replay(text) is None:
        raise HTTPException(404, "no recorded run matches that message")
    if not _busy.acquire(blocking=False):
        raise HTTPException(409, "a weight PR is already running")
    try:
        n = wci.reserve_pr_number()
    except Exception:
        _busy.release()
        raise
    if replay:
        th = threading.Thread(target=_run, args=(wci.replay_pr, n, text, author, source),
                              kwargs={"speed": float(body.get("speed") or 4.0)}, daemon=True)
    else:
        th = threading.Thread(target=_run, args=(wci.open_pr, n, text, author, source), daemon=True)
    th.start()
    return JSONResponse({"pr": n, "replay": replay, "lesson": wci.learn.parse_correction(text)}, status_code=202)


@router.get("/api/prs")
def api_prs():
    return {"prs": wci.load_prs(), "running": _busy.locked()}


@router.get("/api/ledger")
def api_ledger():
    led = wci.load_ledger()
    return {"versions": led, "production": wci.production(led)["version"]}


@router.get("/api/blame")
def api_blame(type: str = "usb_a"):  # noqa: A002
    return wci.blame(type)


@router.post("/api/ledger/revert")
async def api_revert(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    try:
        v = wci.revert((body or {}).get("version") or "v1")
    except KeyError as e:
        raise HTTPException(404, str(e)) from None
    _emit({"type": "ledger", "action": "revert", "version": v})
    return {"version": v, "production": v["version"]}


@router.post("/api/ledger/reset")
def api_reset():
    if _busy.locked():
        raise HTTPException(409, "a weight PR is running")
    out = wci.reset()
    _emit({"type": "ledger", "action": "reset", "production": "v1"})
    return {"production": "v1", "versions": out["ledger"]}
