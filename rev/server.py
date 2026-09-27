"""REV API server (SPEC section 8).

Run:  /opt/anaconda3/bin/python3 -m uvicorn rev.server:app --port 8000
"""
from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from rev import agent, kernel

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
DATA_DIR = ROOT / "rev" / "data"
EVAL_PATH = DATA_DIR / "eval_results.json"
DEMO_PATH = DATA_DIR / "demo.json"
HEARTBEAT_S = 15
LOG_MAX = 200

app = FastAPI(title="REV")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# --------------------------------------------------------------------------- state
def load_demo():
    d = json.loads(DEMO_PATH.read_text())
    return {k: d[k] for k in ("board_a", "enclosure_a", "board_b", "change_summary")}


DEMO = load_demo()
_lock = threading.RLock()
STATE: dict = {}
_run = {"gen": 0}          # bumps on reset/revision so a stale run's events are dropped


def _fresh_state(board, prev_board, enc):
    return {"project": {"name": "Sensor Hub", "company": "Acme Devices", "slug": "sensor-hub"},
            "board": copy.deepcopy(board), "prev_board": copy.deepcopy(prev_board),
            "enclosure": copy.deepcopy(enc), "checks": kernel.check(board, enc),
            "log": [], "running": False, "model": None, "cached": False,
            "change_summary": DEMO["change_summary"] if prev_board else None}


def snapshot():
    with _lock:
        return copy.deepcopy(STATE)


def _log(text, kind="info"):
    ev = {"t": time.strftime("%H:%M:%S"), "kind": kind, "text": text}
    with _lock:
        STATE.setdefault("log", []).append(ev)
        del STATE["log"][:-LOG_MAX]
    return ev


def set_rev_a():
    with _lock:
        _run["gen"] += 1
        STATE.clear()
        STATE.update(_fresh_state(DEMO["board_a"], None, DEMO["enclosure_a"]))
    _log("Reset to Rev A: Sensor Hub enclosure, all checks pass")


def set_rev_b():
    with _lock:
        _run["gen"] += 1
        log = STATE.get("log", [])
        STATE.clear()
        STATE.update(_fresh_state(DEMO["board_b"], DEMO["board_a"], DEMO["enclosure_a"]))
        STATE["log"] = log
    n = sum(1 for c in STATE["checks"] if not c["pass"])
    _log(f"Rev B board landed ({DEMO['change_summary']}). Rev A enclosure: {n} checks failing", "error")


set_rev_a()


# --------------------------------------------------------------------------- SSE hub
class Hub:
    def __init__(self):
        self.queues: set[asyncio.Queue] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def publish(self, ev: dict):
        """Thread-safe broadcast to every connected SSE client."""
        if self.loop is None:
            return
        data = json.dumps(ev, default=str)
        for q in list(self.queues):
            try:
                self.loop.call_soon_threadsafe(q.put_nowait, data)
            except RuntimeError:
                pass


hub = Hub()


def publish_state():
    hub.publish({"type": "state", "state": snapshot()})


def publish_log(text, kind="info"):
    hub.publish({"type": "log", **_log(text, kind)})


@app.on_event("startup")
async def _startup():
    hub.loop = asyncio.get_running_loop()
    threading.Thread(target=agent.warmup, daemon=True).start()


@app.get("/api/events")
async def events(request: Request):
    q: asyncio.Queue = asyncio.Queue()
    hub.loop = asyncio.get_running_loop()
    hub.queues.add(q)

    async def gen():
        try:
            yield "retry: 1500\n\n"
            yield f"data: {json.dumps({'type': 'state', 'state': snapshot()}, default=str)}\n\n"
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), HEARTBEAT_S)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield ": heartbeat\n\n"
        finally:
            hub.queues.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})


# --------------------------------------------------------------------------- runs
def _make_emit(gen):
    """Agent emit -> merge into server state -> broadcast. Drops events from a cancelled run."""
    def emit(ev):
        t = ev.get("type")
        with _lock:
            if _run["gen"] != gen:
                return
            if t == "state":
                part = ev.get("state") or {}
                for k in ("board", "enclosure", "checks"):
                    if k in part:
                        STATE[k] = copy.deepcopy(part[k])
                ev = {"type": "state", "state": snapshot()}
            elif t == "log":
                STATE["log"].append({k: ev.get(k) for k in ("t", "kind", "text")})
                del STATE["log"][:-LOG_MAX]
            elif t == "done":
                STATE["running"] = False
        if t == "done":
            publish_state()
        hub.publish(ev)
    return emit


def _begin_run(model, cached):
    """Validate + mark running. Returns run generation. Caller must be on the event loop thread."""
    if model not in agent.MODELS:
        raise HTTPException(400, f"model must be one of {list(agent.MODELS)}")
    with _lock:
        if STATE.get("running"):
            raise HTTPException(409, "a run is already in progress")
        if STATE.get("prev_board") is None:
            set_rev_b()
        STATE["running"] = True
        STATE["model"] = model
        STATE["cached"] = bool(cached)
        gen = _run["gen"]
    publish_log(f"Refit started: {agent.model_label(model)}{' (cached replay)' if cached else ''}")
    publish_state()
    return gen


def _do_run(model, cached, gen, **kw):
    emit = _make_emit(gen)
    try:
        with _lock:
            board_a = copy.deepcopy(STATE["prev_board"])
            board_b = copy.deepcopy(STATE["board"])
            summary = STATE.get("change_summary") or DEMO["change_summary"]
        enc_a = DEMO["enclosure_a"] if board_a == DEMO["board_a"] else kernel.enclosure_for(board_a)
        return agent.run_refit(board_a, enc_a, board_b, summary, model, emit, cached=cached,
                               scenario="demo", **kw)
    except Exception as e:  # never crash the server
        emit({"type": "log", "t": time.strftime("%H:%M:%S"), "kind": "error", "text": f"Run failed: {e}"})
        emit({"type": "done", "pass": False, "model": model, "latency_s": None, "cached": bool(cached),
              "error": str(e)})
        return None
    finally:
        with _lock:
            if _run["gen"] == gen and STATE.get("running"):
                STATE["running"] = False
                publish_state()


async def _json(request: Request):
    try:
        b = await request.json()
        return b if isinstance(b, dict) else {}
    except Exception:
        return {}


@app.post("/api/agent/run")
async def agent_run(request: Request):
    body = await _json(request)
    model = str(body.get("model") or "tuned")
    cached = bool(body.get("cached", False))
    gen = _begin_run(model, cached)
    threading.Thread(target=_do_run, args=(model, cached, gen), daemon=True).start()
    return {"ok": True, "model": model, "cached": cached}


def refit_summary(res, model, base_url):
    checks = res.get("checks") or []
    npass = sum(1 for c in checks if c.get("pass"))
    lines = [f"49th Engineer refit of the Sensor Hub enclosure for board Rev {(STATE.get('board') or {}).get('rev', 'B')} - {agent.model_label(model)} "
             f"({res.get('model_name') or model}){' [cached replay]' if res.get('cached') else ''}.",
             f"Result: {'PASS' if res.get('pass') else 'FAIL'} - {npass}/{len(checks)} checks pass"
             + (f", model latency {res['latency_s']}s" if res.get("latency_s") is not None else "") + "."]
    calls = res.get("calls") or []
    if calls:
        lines.append(f"Tool calls ({len(calls)}): " + "; ".join(agent.fmt_call(c) for c in calls))
    for c in checks:
        lines.append(f"- {c.get('label')}: {'pass' if c.get('pass') else 'FAIL'} ({c.get('detail')})")
    if res.get("errors"):
        lines.append("Rejected calls: " + "; ".join(res["errors"]))
    if res.get("error"):
        lines.append(f"Error: {res['error']}")
    if res.get("gbrain_slug"):
        lines.append(f"Change record saved to GBrain: {res['gbrain_slug']}")
    lines.append(f"Live 3D view: {base_url}")
    return "\n".join(lines)


def _base_url(request: Request):
    h = request.headers
    proto = h.get("x-forwarded-proto") or request.url.scheme
    host = h.get("x-forwarded-host") or h.get("host") or request.url.netloc
    return f"{proto}://{host}/"


@app.post("/api/qm/refit")
async def qm_refit(request: Request):
    body = await _json(request)
    model = str(body.get("model") or "tuned")
    cached = bool(body.get("cached", False))
    gen = _begin_run(model, cached)
    res = await asyncio.to_thread(_do_run, model, cached, gen, wait_gbrain=8.0)
    res = res or {"pass": False, "checks": snapshot()["checks"], "error": "run failed"}
    url = _base_url(request)
    return {"summary": refit_summary(res, model, url), "pass": bool(res.get("pass")),
            "checks": res.get("checks") or [], "viewer_url": url, "model": model,
            "calls": res.get("calls") or [], "latency_s": res.get("latency_s"),
            "cached": bool(res.get("cached")), "gbrain_slug": res.get("gbrain_slug"),
            "error": res.get("error")}


# --------------------------------------------------------------------------- state endpoints
@app.get("/api/state")
def get_state():
    return snapshot()


@app.post("/api/reset")
def reset():
    set_rev_a()
    st = snapshot()
    hub.publish({"type": "state", "state": st})
    return st


def set_rev_c():
    """Rev C (live-learning scenario, rev/learn.py): Rev B board + its correct enclosure -> Rev C adds J5 usb_a."""
    d = json.loads((DATA_DIR / "demo_rev_c.json").read_text())
    with _lock:
        _run["gen"] += 1
        log = STATE.get("log", [])
        STATE.clear()
        STATE.update(_fresh_state(d["board_b"], d["board_a"], d["enclosure_a"]))
        STATE["log"], STATE["change_summary"] = log, d["change_summary"]
    n = sum(1 for c in STATE["checks"] if not c["pass"])
    _log(f"Rev C board landed ({d['change_summary']}). Rev B enclosure: {n} checks failing", "error")


@app.post("/api/revision")
async def revision(request: Request):
    body = await _json(request)
    set_rev_c() if str(body.get("rev") or "B").upper() == "C" else set_rev_b()
    st = snapshot()
    hub.publish({"type": "state", "state": st})
    return st


@app.get("/api/eval")
def get_eval():
    if not EVAL_PATH.exists():
        raise HTTPException(404, "eval_results.json not found (eval not run yet)")
    try:
        return JSONResponse(json.loads(EVAL_PATH.read_text()))
    except Exception as e:
        raise HTTPException(500, f"eval_results.json unreadable: {e}")


# --------------------------------------------------------------------------- GBrain
@app.get("/api/gbrain/pages")
def gbrain_pages(limit: int = 100):
    from rev import gbrain_io
    return {"pages": gbrain_io.list_pages(limit=limit)}


@app.get("/api/gbrain/page")
def gbrain_page(slug: str):
    from rev import gbrain_io
    md = gbrain_io.get_page(slug)
    if md is None:
        raise HTTPException(404, f"page not found: {slug}")
    return {"slug": slug, "markdown": md}


@app.get("/api/gbrain/search")
def gbrain_search(q: str, limit: int = 8):
    from rev import gbrain_io
    return {"q": q, "results": gbrain_io.search(q, limit=limit)}


@app.get("/api/health")
def health():
    return {"ok": True, "running": STATE.get("running"), "clients": len(hub.queues),
            "tuned_available": agent.CHECKPOINT_PATH.exists(), "eval_available": EVAL_PATH.exists()}


# --------------------------------------------------------------------------- live learning (rev/learn_api.py)
# Optional add-ons: a broken add-on must never take the core demo server down.
try:
    from rev.learn_api import router as learn_router, set_broadcast as learn_set_broadcast  # noqa: E402

    app.include_router(learn_router)
    learn_set_broadcast(hub.publish)
except Exception as _e:  # noqa: BLE001
    print(f"[rev.server] live-learning API disabled: {type(_e).__name__}: {_e}", flush=True)

# --------------------------------------------------------------------------- THE WALL (rev/fleet_api.py): GET /fleet, /api/fleet*
try:
    from rev.fleet_api import router as fleet_router  # noqa: E402

    app.include_router(fleet_router)
except Exception as _e:  # noqa: BLE001
    print(f"[rev.server] fleet API disabled: {type(_e).__name__}: {_e}", flush=True)

# --------------------------------------------------------------------------- GBrain -> River (rev/pipeline_api.py)
try:
    from rev.pipeline_api import router as pipeline_router, set_broadcast as pipeline_set_broadcast  # noqa: E402

    app.include_router(pipeline_router)
    pipeline_set_broadcast(hub.publish)
except Exception as _e:  # noqa: BLE001
    print(f"[rev.server] pipeline API disabled: {type(_e).__name__}: {_e}", flush=True)

# --------------------------------------------------------------------------- workstation capture (rev/capture_api.py)
try:
    from rev.capture_api import router as capture_router, set_broadcast as capture_set_broadcast  # noqa: E402

    app.include_router(capture_router)
    capture_set_broadcast(hub.publish)
except Exception as _e:  # noqa: BLE001
    print(f"[rev.server] capture API disabled: {type(_e).__name__}: {_e}", flush=True)

# --------------------------------------------------------------------------- weight PRs / ledger (rev/weights_ci_api.py)
try:
    from rev.weights_ci_api import router as wci_router, set_broadcast as wci_set_broadcast  # noqa: E402

    app.include_router(wci_router)
    wci_set_broadcast(hub.publish)
except Exception as _e:  # noqa: BLE001
    print(f"[rev.server] weights CI API disabled: {type(_e).__name__}: {_e}", flush=True)

# --------------------------------------------------------------------------- web
@app.get("/")
def index():
    p = WEB_DIR / "index.html"
    if not p.exists():
        return JSONResponse({"error": "web/index.html missing"}, status_code=404)
    return FileResponse(p, headers={"Cache-Control": "no-cache"})


if WEB_DIR.exists():
    app.mount("/web", StaticFiles(directory=str(WEB_DIR)), name="web")
