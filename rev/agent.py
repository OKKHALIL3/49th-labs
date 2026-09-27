"""REV agent loop: one engineering change -> model -> tool calls -> checker -> GBrain change record.

    run_refit(board_a, enclosure_a, board_b, change_summary, model="base"|"tuned", emit, cached=False) -> dict

Events passed to emit(ev) (SPEC section 8, plus "log"):
    {"type":"log","t":..,"kind":"info"|"error","text":..}
    {"type":"state","state":{"board","enclosure","checks"}}      (partial state; the server merges it)
    {"type":"thinking","model":..}
    {"type":"tool","index":i,"call":{..},"ok":bool,"error":str|None}
    {"type":"gbrain","slug":..,"title":..}                        (background, after the checks)
    {"type":"done","pass":bool,"model":..,"latency_s":..,"cached":bool,"error":str|None,"n_calls":int}

Every live model output is cached in rev/data/run_cache.json keyed "<model>:<scenario>";
cached=True replays that real, earlier output (the UI labels it cached).
Never raises: River / network / parse errors end as a log event + done pass=false.
"""
from __future__ import annotations

import concurrent.futures as cf
import copy
import hashlib
import json
import os
import threading
import time
from pathlib import Path

from rev import kernel, prompts

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "rev" / "data"
CACHE_PATH = DATA_DIR / "run_cache.json"
CHECKPOINT_PATH = DATA_DIR / "checkpoint.json"

MODELS = ("base", "tuned")
SAMPLE_TIMEOUT_S = 150
STEP_DELAY_S = 0.35

_pool = cf.ThreadPoolExecutor(max_workers=4, thread_name_prefix="rev-agent")
_cache_lock = threading.Lock()


# --------------------------------------------------------------------------- helpers
def _now():
    return time.strftime("%H:%M:%S")


def _base_model_name():
    try:
        from rev import river_util as ru
        return ru.BASE_MODEL
    except Exception:
        try:
            return json.loads((DATA_DIR / "river_config.json").read_text()).get("base_model") or "Qwen/Qwen3.5-9B"
        except Exception:
            return "Qwen/Qwen3.5-9B"


def _checkpoint_info():
    try:
        return json.loads(CHECKPOINT_PATH.read_text())
    except Exception:
        return None


def model_label(model):
    return "Acme-tuned model" if model == "tuned" else "Base model"


def model_name(model):
    """Human-readable model identity, e.g. for the GBrain change record."""
    base = _base_model_name()
    if model == "tuned":
        ck = _checkpoint_info() or {}
        step = ck.get("step")
        return f"Acme-tuned {base} + LoRA" + (f" (step {step})" if step is not None else "")
    return f"base {base}"


def make_task(board_a, enclosure_a, board_b, change_summary):
    return {"board_a": board_a, "enclosure_a": enclosure_a, "board_b": board_b, "change_summary": change_summary}


def scenario_key(task):
    """Stable id of the prompt content (so a changed scenario never replays a stale output)."""
    s = prompts.SYSTEM + "\n" + prompts.user_prompt(task)
    return hashlib.sha1(s.encode()).hexdigest()[:12]


def cache_key(model, scenario):
    return f"{model}:{scenario}"


def load_cache():
    try:
        return json.loads(CACHE_PATH.read_text())
    except Exception:
        return {}


def cache_get(model, scenario):
    return load_cache().get(cache_key(model, scenario))


def cache_put(model, scenario, entry):
    with _cache_lock:
        c = load_cache()
        c[cache_key(model, scenario)] = entry
        try:
            tmp = CACHE_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(c, indent=1))
            os.replace(tmp, CACHE_PATH)
        except Exception:
            pass


def _sample(model, prompt_text):
    from rev import river_util as ru
    if model == "tuned":
        return ru.sample_tuned(prompt_text, temperature=0.0)
    return ru.sample_base(prompt_text, temperature=0.0)


def _render(task):
    from rev import river_util as ru
    return ru.render(prompts.messages(task))


def _safe_emit(emit, ev):
    if emit is None:
        return
    try:
        emit(ev)
    except Exception:
        pass


def fmt_call(c):
    if not isinstance(c, dict):
        return json.dumps(c)
    a = c.get("args") or {}
    if not isinstance(a, dict):
        return f"{c.get('tool', '?')}({a!r})"
    return f"{c.get('tool', '?')}(" + ", ".join(f"{k}={v}" for k, v in a.items()) + ")"


def warmup():
    """Load tokenizer/renderer + client so the first live run is fast. Never raises."""
    try:
        from rev import river_util as ru
        ru.get_tokenizer()
        ru.get_renderer()
        ru.get_client()
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- main loop
def run_refit(board_a, enclosure_a, board_b, change_summary, model="base", emit=None, cached=False,
              scenario=None, step_delay=STEP_DELAY_S, gbrain="pass", wait_gbrain=0.0,
              timeout_s=SAMPLE_TIMEOUT_S):
    """Refit enclosure_a (designed for board_a) to board_b with the given model.

    gbrain: "pass" (default) writes the change record only when all checks pass, so a failed
            refit never overwrites the project's verified design in memory; "always"; "never".
    wait_gbrain: seconds to wait for the background GBrain write (result["gbrain_slug"]).
    Returns {pass, calls, raw, checks, enclosure, latency_s, errors, model, model_name, cached,
             error, gbrain_slug}.
    """
    t_run = time.time()
    model = model if model in MODELS else "base"
    task = make_task(board_a, enclosure_a, board_b, change_summary)
    scen = f"{scenario}@{scenario_key(task)}" if scenario else scenario_key(task)
    name = model_name(model)
    res = {"pass": False, "calls": [], "raw": None, "checks": [], "enclosure": copy.deepcopy(enclosure_a),
           "latency_s": None, "errors": [], "model": model, "model_name": name, "cached": bool(cached),
           "error": None, "gbrain_slug": None}

    def log(text, kind="info"):
        _safe_emit(emit, {"type": "log", "t": _now(), "kind": kind, "text": text})

    def finish(error=None):
        res["error"] = error
        if error:
            log(error, "error")
        _safe_emit(emit, {"type": "done", "pass": bool(res["pass"]), "model": model,
                          "latency_s": res["latency_s"], "cached": bool(res["cached"]), "error": error,
                          "n_calls": len(res["calls"]), "run_s": round(time.time() - t_run, 2)})
        return res

    try:
        enc = copy.deepcopy(enclosure_a)
        checks = kernel.check(board_b, enc)
        res["checks"] = checks
        _safe_emit(emit, {"type": "state", "state": {"board": board_b, "enclosure": enc, "checks": checks}})
        _safe_emit(emit, {"type": "thinking", "model": model})

        # -- memory: the current design comes from GBrain (facts, not skill). Display only; the prompt
        #    carries the same design as JSON. Never load change records into the prompt.
        design_slug = f"sensor-hub/rev-{str(board_a.get('rev', 'a')).lower()}-enclosure"
        fut_mem = _pool.submit(_read_design, design_slug)

        # -- model output (live or cached)
        raw = None
        if cached:
            entry = cache_get(model, scen)
            if entry and isinstance(entry.get("raw"), str):
                raw = entry["raw"]
                res["latency_s"] = entry.get("latency_s")
                log(f"Replaying cached {model_label(model)} output recorded {entry.get('recorded_at', '?')} "
                    f"({entry.get('model_name', name)})")
            else:
                log(f"No cached {model} output for this scenario yet; calling the model live")
                res["cached"] = False
        try:
            if fut_mem.result(timeout=4):
                log(f"Loaded current design from GBrain: {design_slug}")
        except Exception:
            pass

        if raw is None:
            if model == "tuned" and not CHECKPOINT_PATH.exists():
                return finish("Tuned model not available yet: rev/data/checkpoint.json is missing "
                              "(River training still running?). Try the base model or a cached run.")
            log(f"Sampling {name} on River (temperature 0)")
            t0 = time.time()
            try:
                prompt_text = _pool.submit(_render, task).result(timeout=60)
                raw = _pool.submit(_sample, model, prompt_text).result(timeout=timeout_s)
            except cf.TimeoutError:
                return finish(f"River call timed out after {timeout_s}s")
            except Exception as e:  # River / network / auth / tokenizer errors
                msg = str(e).replace(os.environ.get("RIVER_API_KEY", "\0") or "\0", "***")
                return finish(f"River error ({type(e).__name__}): {msg[:300]}")
            res["latency_s"] = round(time.time() - t0, 2)
            cache_put(model, scen, {"raw": raw, "latency_s": res["latency_s"], "model_name": name,
                                    "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                    "scenario": scen})
            log(f"{model_label(model)} answered in {res['latency_s']}s")
        res["raw"] = raw

        calls = prompts.parse_calls(raw)
        if calls is None:
            res["checks"] = checks
            return finish(f"Model output was not a JSON list of tool calls: {str(raw)[:200]!r}")
        res["calls"] = calls

        # -- apply one call at a time, animating the geometry
        for i, call in enumerate(calls):
            ok, err = True, None
            try:
                enc = kernel.apply_call(board_b, enc, call)
            except kernel.ToolError as e:
                ok, err = False, str(e)
            except Exception as e:
                ok, err = False, f"{type(e).__name__}: {e}"
            if err:
                res["errors"].append(f"call {i + 1} {fmt_call(call)}: {err}")
            checks = kernel.check(board_b, enc)
            _safe_emit(emit, {"type": "tool", "index": i, "call": call, "ok": ok, "error": err})
            _safe_emit(emit, {"type": "state", "state": {"board": board_b, "enclosure": enc, "checks": checks}})
            if step_delay:
                time.sleep(step_delay)

        res["enclosure"], res["checks"] = enc, checks
        res["pass"] = bool(checks) and all(c["pass"] for c in checks)
        npass = sum(1 for c in checks if c["pass"])
        log(f"Checker: {npass}/{len(checks)} checks pass" +
            ("" if res["pass"] else " - failing: " + ", ".join(c["label"] for c in checks if not c["pass"])),
            "info" if res["pass"] else "error")

        # -- project memory: change record, in the background (never blocks / breaks the run)
        if gbrain == "always" or (gbrain == "pass" and res["pass"]):
            th = threading.Thread(target=_write_record, daemon=True,
                                  args=(board_a, board_b, calls, checks, name, model, res["pass"], emit, res))
            th.start()
            if wait_gbrain:
                th.join(wait_gbrain)
        elif gbrain == "pass":
            log("Checks failing: change record not written to GBrain (memory keeps only verified designs)")
        return finish(None)
    except Exception as e:  # last-resort guard: never crash the caller
        return finish(f"Agent error ({type(e).__name__}): {e}")


def _read_design(slug):
    try:
        from rev import gbrain_io
        return gbrain_io.get_page(slug)
    except Exception:
        return None


def _write_record(board_a, board_b, calls, checks, name, model, passed, emit, res):
    try:
        from rev import gbrain_io
        slug = gbrain_io.write_change_record(board_a, board_b, calls, checks, name)
    except Exception:
        slug = None
    rb = board_b.get("rev", "B")
    if slug:
        res["gbrain_slug"] = slug
        _safe_emit(emit, {"type": "gbrain", "slug": slug,
                          "title": f"Rev {rb} enclosure refit by {model_label(model)}: "
                                   f"{'all checks pass' if passed else 'checks failing'}"})
    else:
        _safe_emit(emit, {"type": "log", "t": _now(), "kind": "error",
                          "text": "GBrain write failed (change record not saved); run result unaffected"})


# --------------------------------------------------------------------------- CLI
if __name__ == "__main__":  # python3 -m rev.agent [base|tuned] [--cached]
    import sys
    from rev import tasks

    d = tasks.load("demo")
    m = sys.argv[1] if len(sys.argv) > 1 else "base"
    r = run_refit(d["board_a"], d["enclosure_a"], d["board_b"], d["change_summary"], m,
                  emit=lambda ev: print(json.dumps(ev)[:200]), cached="--cached" in sys.argv,
                  scenario="demo", step_delay=0, wait_gbrain=10)
    print(json.dumps({k: r[k] for k in ("pass", "latency_s", "errors", "error", "gbrain_slug")}))
