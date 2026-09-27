"""Named checkpoint registry + which checkpoint each AGENT uses (49th Labs / The 49th Engineer).

Names are resolved at runtime from files on disk -- nothing here is hard-coded scores:
    qwen3.5-9b        untrained base model, no LoRA
    company-v1        rev/data/checkpoint.json (200 verified ECOs, synthetic Acme company history)
    company-vN        rev/data/weights_ledger.json version vN (+ its river_checkpoint / checkpoint_file)
    <engineer>-vN     rev/data/engineers/<id>/checkpoint.json + lessons.json (N = number of reviewed lessons)

company-vN is produced by promoting approved training examples and retraining a company CANDIDATE that
continues from the production checkpoint -- not a parameter merge. A new company version never rewrites a
personal checkpoint: agents keep whatever rev/data/agents.json says until an explicit inherit/rebase.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
LEDGER = DATA_DIR / "weights_ledger.json"
COMPANY_V1 = DATA_DIR / "checkpoint.json"
ENG_DIR = DATA_DIR / "engineers"
AGENTS_PATH = DATA_DIR / "agents.json"
BASE_NAME = "qwen3.5-9b"
BASE_MODEL = "Qwen/Qwen3.5-9B"
DEFAULT_AGENTS = {"senior-me": "senior-me-v1", "junior-me": "company-v1"}


def _rj(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return default


def _wj(p, obj):
    p = Path(p)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    os.replace(tmp, p)


def short_ref(ref: str | None) -> str:
    if not ref:
        return "(no LoRA)"
    s = ref.replace("river://", "")
    run, _, rest = s.partition("/")
    return f"river://{run[:8]}…/{rest.split('/')[-1]}"


def _roles() -> dict:
    ros = _rj(DATA_DIR / "engineers.json", {}) or {}
    return {e["id"]: e.get("role", e["id"]) for e in ros.get("engineers", [])}


def list_checkpoints() -> list[dict]:
    out = [{"name": BASE_NAME, "base_model": BASE_MODEL, "river_ref": None, "parent": None,
            "trained_from": "untrained base model (no LoRA)", "record": None}]
    v1 = _rj(COMPANY_V1, {}) or {}
    led = _rj(LEDGER, []) or []
    for v in led:
        name = f"company-{v['version']}"
        rec = None
        if v.get("checkpoint_file"):
            rec = _rj(DATA_DIR / v["checkpoint_file"])
        if not rec and v.get("river_checkpoint"):
            rec = {"path": v["river_checkpoint"], "step": 0, "checkpoint_type": "inference"}
        if v["version"] == "v1":
            tf = f"{v1.get('n_train', 200)} verified ECOs (synthetic company history)"
        else:
            les = v.get("lesson") or {}
            tf = (f"PR #{v.get('pr')}: approved {les.get('type', '?')} {les.get('tolerance', '?')} mm examples + "
                  f"replayed history; retrained candidate from company-{v.get('parent')}")
        out.append({"name": name, "base_model": BASE_MODEL, "river_ref": (rec or {}).get("path"),
                    "parent": f"company-{v['parent']}" if v.get("parent") else BASE_NAME,
                    "trained_from": tf, "status": v.get("status"), "tests": v.get("tests"), "record": rec,
                    "author": v.get("author")})
    if not led and v1:
        out.append({"name": "company-v1", "base_model": BASE_MODEL, "river_ref": v1.get("path"), "parent": BASE_NAME,
                    "trained_from": f"{v1.get('n_train', 200)} verified ECOs (synthetic company history)",
                    "status": "production", "record": v1})
    by_ref = {c["river_ref"]: c["name"] for c in out if c["river_ref"]}
    roles = _roles()
    if ENG_DIR.exists():
        for d in sorted(ENG_DIR.iterdir()):
            if not d.is_dir() or d.name.startswith("_") or d.name not in ("senior-me", "junior-me"):
                continue
            ck = _rj(d / "checkpoint.json")
            if not ck:
                continue
            les = _rj(d / "lessons.json", []) or []
            n = max(1, len(les))
            last = les[-1] if les else {}
            lz = ck.get("lesson") or {}
            who = roles.get(d.name, ck.get("role") or d.name)
            src = last.get("gbrain_slug") or last.get("source") or "chat"
            out.append({"name": f"{d.name}-v{n}", "base_model": ck.get("base_model", BASE_MODEL),
                        "river_ref": ck.get("path"), "parent": by_ref.get(ck.get("parent"), "company-v1"),
                        "trained_from": f"lesson: {lz.get('type', '?')} {lz.get('tolerance', '?')} mm from "
                                        f"{d.name}'s reviewed change ({who}; {src})",
                        "engineer": d.name, "lessons": les, "record": ck})
    return out


def get(name: str) -> dict | None:
    for c in list_checkpoints():
        if c["name"] == name:
            return c
    # "<engineer>-vN" with a stale N -> latest personal checkpoint of that engineer
    for c in list_checkpoints():
        if c.get("engineer") and name.startswith(c["engineer"] + "-v"):
            return c
    return None


def resolve(name: str):
    """-> river.Checkpoint usable by river_util.sample_tuned(checkpoint=...), or None for the untrained base."""
    c = get(name)
    if c is None:
        raise KeyError(f"unknown checkpoint {name!r}; known: {[x['name'] for x in list_checkpoints()]}")
    rec = c.get("record")
    if not rec:
        return None
    import river_client as river
    return river.Checkpoint(path=rec["path"], step=int(rec.get("step", 0)),
                            checkpoint_type=rec.get("checkpoint_type", "inference"))


# --------------------------------------------------------------------------- agents -> checkpoint
def _default_agents() -> dict:
    names = {c["name"] for c in list_checkpoints()}
    out = {}
    for a, ck in DEFAULT_AGENTS.items():
        if ck not in names:
            c = get(ck)
            ck = c["name"] if c else "company-v1"
        out[a] = {"checkpoint": ck, "since": time.strftime("%Y-%m-%dT%H:%M:%S"), "how": "initial assignment"}
    return out


def agents() -> dict:
    data = _rj(AGENTS_PATH)
    if not data:
        data = _default_agents()
        _wj(AGENTS_PATH, data)
    return data


def set_agent_checkpoint(agent: str, name: str, how: str = "explicit rebase") -> dict:
    if get(name) is None:
        raise KeyError(f"unknown checkpoint {name!r}")
    data = agents()
    before = (data.get(agent) or {}).get("checkpoint")
    data[agent] = {"checkpoint": get(name)["name"], "since": time.strftime("%Y-%m-%dT%H:%M:%S"), "how": how,
                   "previous": before}
    _wj(AGENTS_PATH, data)
    return {"agent": agent, "before": before, "after": data[agent]["checkpoint"]}


def new_agent(name: str, from_checkpoint: str) -> dict:
    return set_agent_checkpoint(name, from_checkpoint, how=f"new agent from {from_checkpoint}")


def reset_agents() -> dict:
    data = _default_agents()
    _wj(AGENTS_PATH, data)
    return data
