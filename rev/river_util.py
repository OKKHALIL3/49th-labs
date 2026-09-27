"""River client layer for REV (SPEC section 7).

Exports:
    get_client() -> river.Client
    BASE_MODEL: str
    STOP: list[str]                     # stop strings for sampling (chat EOS)
    EOS_TEXT: str                       # text appended to completions in training datums
    get_tokenizer(), get_renderer()
    render(messages) -> str             # chat template, add_generation_prompt, thinking off
    make_datum(prompt_text, completion_text) -> dict
    sample_base(prompt_text, **kw) -> str
    sample_tuned(prompt_text, **kw) -> str   # uses rev/data/checkpoint.json
    save_checkpoint_ref(ckpt, path=CHECKPOINT_PATH, **extra) -> dict
    load_checkpoint_ref(path=None) -> river.Checkpoint   # None -> active_checkpoint_path()
    active_checkpoint_path() -> Path                    # checkpoint_current.json (promoted) else checkpoint.json

Checkpoint persistence: model.save_weights(name, mode="inference") returns a
river.Checkpoint(path="river://<run>/weights/<name>", step, checkpoint_type).
We store those fields as JSON; any process rebuilds river.Checkpoint(**fields)
and samples through session.sample(prompt, base_model=BASE, checkpoint=ckpt).
"""

from __future__ import annotations

import atexit
import json
import os
import threading
from pathlib import Path
from typing import Any

# Stale conda protobuf-3 C extension forces the 'cpp' backend, which breaks river_client
# ("FieldDescriptor object has no attribute 'is_repeated'"). Force the modern upb backend.
os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "upb")

import river_client as river  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "rev" / "data"
ENV_PATH = ROOT / ".env"
CONFIG_PATH = DATA_DIR / "river_config.json"
CHECKPOINT_PATH = DATA_DIR / "checkpoint.json"
# Written by `rev.learn --promote` (live one-shot learning); when present it wins over checkpoint.json.
CURRENT_CHECKPOINT_PATH = DATA_DIR / "checkpoint_current.json"
DEFAULT_BASE = "Qwen/Qwen3.5-9B"

_lock = threading.RLock()
_client: river.Client | None = None
_tokenizer = None
_renderer = None
_session_ctx = None
_session = None


# ── env / config ─────────────────────────────────────────────────────────────

def load_env(path: Path = ENV_PATH) -> None:
    """Tiny .env parser: KEY=VALUE lines, optional quotes; never overrides os.environ."""
    try:
        text = Path(path).read_text()
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val


def _read_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return {}


load_env()
BASE_MODEL: str = os.environ.get("RIVER_MODEL") or _read_config().get("base_model") or DEFAULT_BASE


def api_key() -> str:
    load_env()
    key = os.environ.get("RIVER_API_KEY", "").strip()
    if not key:
        raise RuntimeError(f"RIVER_API_KEY not set (expected in {ENV_PATH})")
    return key


def get_client() -> river.Client:
    global _client
    with _lock:
        if _client is None:
            _client = river.Client(api_key=api_key())
        return _client


# ── tokenizer / chat template ───────────────────────────────────────────────

def get_tokenizer():
    global _tokenizer
    with _lock:
        if _tokenizer is None:
            try:  # HF cache first: avoids HF rate limits (429) on every process start
                _tokenizer = river.load_tokenizer(base_model=BASE_MODEL, local_files_only=True)
            except Exception:
                _tokenizer = river.load_tokenizer(base_model=BASE_MODEL)
        return _tokenizer


def get_renderer():
    """River's own renderer for the base model family, thinking disabled (None if unsupported)."""
    global _renderer
    with _lock:
        if _renderer is None:
            from river_client.renderers import get_renderer as _get

            try:
                _renderer = _get(BASE_MODEL, thinking=False, tokenizer=get_tokenizer())
            except Exception:
                _renderer = False
        return _renderer or None


def _stop_strings() -> list[str]:
    r = get_renderer()
    if r is not None:
        return list(r.get_stop_strings())
    tok = get_tokenizer()
    return [tok.eos_token] if getattr(tok, "eos_token", None) else []


def __getattr__(name: str):  # lazy module attrs (need tokenizer download)
    if name == "STOP":
        return _stop_strings()
    if name == "EOS_TEXT":
        return _stop_strings()[0]
    raise AttributeError(name)


def _msg_list(messages) -> list[dict]:
    return [{"role": m["role"], "content": m["content"]} for m in messages]


def render(messages: list[dict]) -> str:
    """Render chat messages into a prompt string ending with the assistant generation prompt.

    Thinking is disabled (Qwen3.5: prefilled empty <think></think> block).
    """
    msgs = _msg_list(messages)
    r = get_renderer()
    if r is not None:
        return r.build_prompt_str(msgs)
    tok = get_tokenizer()
    try:
        return tok.apply_chat_template(
            msgs, tokenize=False, add_generation_prompt=True, enable_thinking=False
        )
    except TypeError:
        return tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)


def encode(text: str) -> list[int]:
    return list(get_tokenizer().encode(text, add_special_tokens=False))


def make_datum(prompt_text: str, completion_text: str) -> dict:
    """SFT datum in River's prediction-position layout.

    input_ids = prompt + completion + EOS; target_tokens[i] = input_ids[i+1];
    weights[i] = 1 where the *predicted* token (input_ids[i+1]) is completion/EOS, else 0.
    """
    p = encode(prompt_text)
    c = encode(completion_text + _stop_strings()[0])
    ids = p + c
    targets = ids[1:] + [0]
    weights = [0.0] * (len(p) - 1) + [1.0] * len(c) + [0.0]
    assert len(ids) == len(targets) == len(weights)
    return {"input_ids": ids, "target_tokens": targets, "weights": weights}


# ── checkpoint persistence ──────────────────────────────────────────────────

def save_checkpoint_ref(ckpt: river.Checkpoint, path: Path | str = CHECKPOINT_PATH, **extra) -> dict:
    rec = {
        "path": ckpt.path,
        "step": ckpt.step,
        "checkpoint_type": ckpt.checkpoint_type,
        "base_model": BASE_MODEL,
        **extra,
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(rec, indent=2))
    os.replace(tmp, path)
    return rec


def active_checkpoint_path() -> Path:
    """The checkpoint the "tuned" model uses: checkpoint_current.json if promoted, else checkpoint.json."""
    return CURRENT_CHECKPOINT_PATH if CURRENT_CHECKPOINT_PATH.exists() else CHECKPOINT_PATH


def load_checkpoint_ref(path: Path | str | None = None) -> river.Checkpoint:
    rec = json.loads(Path(path or active_checkpoint_path()).read_text())
    return river.Checkpoint(
        path=rec["path"], step=int(rec.get("step", 0)), checkpoint_type=rec.get("checkpoint_type", "inference")
    )


# ── sampling ────────────────────────────────────────────────────────────────

def _first_text(result: Any) -> str:
    s = result
    while isinstance(s, (list, tuple)):
        if not s:
            return ""
        s = s[0]
    return getattr(s, "text", str(s))


def _sample_kwargs(kw: dict) -> dict:
    out = {"max_tokens": 1024, "temperature": 0.0, "stop": _stop_strings()}
    out.update({k: v for k, v in kw.items() if v is not None})
    return out


def sample_base(prompt_text: str, **kw) -> str:
    """Greedy sample from the base model (stateless, no session)."""
    return _first_text(get_client().sample(prompt_text, base_model=BASE_MODEL, **_sample_kwargs(kw)))


def get_session():
    """Long-lived process-wide session (for checkpoint sampling). Closed at exit."""
    global _session_ctx, _session
    with _lock:
        if _session is None:
            ctx = get_client().session(app="rev")
            _session = ctx.__enter__()
            _session_ctx = ctx
        return _session


def close_session() -> None:
    global _session_ctx, _session
    with _lock:
        ctx, _session_ctx, _session = _session_ctx, None, None
    if ctx is not None:
        try:
            ctx.__exit__(None, None, None)
        except Exception:
            pass


atexit.register(close_session)


def sample_tuned(prompt_text: str, checkpoint: river.Checkpoint | str | None = None,
                 checkpoint_path: Path | str | None = None, **kw) -> str:
    """Greedy sample from the fine-tuned LoRA (checkpoint_current.json if promoted, else checkpoint.json)."""
    ckpt = checkpoint if checkpoint is not None else load_checkpoint_ref(checkpoint_path)
    args = _sample_kwargs(kw)
    for attempt in range(2):
        try:
            out = get_session().sample(prompt_text, base_model=BASE_MODEL, checkpoint=ckpt, **args)
            return _first_text(out)
        except (river.AuthenticationError, river.ModelNotFoundError):
            raise
        except river.RiverError:
            if attempt:
                raise
            close_session()  # session may have been lost; rebuild once
    raise RuntimeError("unreachable")
