"""Offline tests for rev.agent + rev.server (no River, no GBrain writes).

Run: /opt/anaconda3/bin/python3 -m pytest -q tests/test_server.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from rev import agent, kernel, prompts, tasks
from rev import server


@pytest.fixture()
def fake(monkeypatch, tmp_path):
    """Model returns the demo gold calls (or whatever `fake.raw` says); cache + GBrain are sandboxed."""
    demo = tasks.load("demo")

    class F:
        raw = json.dumps(demo["gold_calls"])
        calls = 0
        records = []
    f = F()

    def sample(model, prompt_text):
        f.calls += 1
        if isinstance(f.raw, Exception):
            raise f.raw
        return f.raw

    monkeypatch.setattr(agent, "CACHE_PATH", tmp_path / "run_cache.json")
    monkeypatch.setattr(agent, "_render", lambda task: "PROMPT")
    monkeypatch.setattr(agent, "_sample", sample)
    monkeypatch.setattr(agent, "_read_design", lambda slug: None)
    from rev import gbrain_io
    monkeypatch.setattr(gbrain_io, "write_change_record",
                        lambda a, b, calls, checks, name: f.records.append(name) or "sensor-hub/rev-b-enclosure-change")
    f.demo = demo
    return f


def _run(f, model="base", **kw):
    d = f.demo
    evs = []
    r = agent.run_refit(d["board_a"], d["enclosure_a"], d["board_b"], d["change_summary"], model,
                        emit=evs.append, step_delay=0, wait_gbrain=5, **kw)
    return r, evs


def test_gold_output_passes_and_events_follow_spec(fake):
    r, evs = _run(fake)
    assert r["pass"] and r["errors"] == [] and r["error"] is None
    assert len(r["calls"]) == len(fake.demo["gold_calls"])
    types = [e["type"] for e in evs]
    assert types.index("thinking") < types.index("tool")
    assert types[-1] == "done" or "gbrain" in types  # gbrain may land after done
    tools = [e for e in evs if e["type"] == "tool"]
    assert [t["index"] for t in tools] == list(range(len(r["calls"])))
    assert all(t["ok"] and t["error"] is None for t in tools)
    # a state event follows every tool event
    for i, e in enumerate(evs):
        if e["type"] == "tool":
            assert evs[i + 1]["type"] == "state"
            assert set(evs[i + 1]["state"]) >= {"board", "enclosure", "checks"}
    done = [e for e in evs if e["type"] == "done"][0]
    assert done["pass"] is True and done["model"] == "base" and done["cached"] is False
    assert r["gbrain_slug"] == "sensor-hub/rev-b-enclosure-change"
    assert any(e["type"] == "gbrain" for e in evs)


def test_cache_replay_uses_recorded_output(fake):
    _run(fake)
    n = fake.calls
    fake.raw = "garbage that would fail"
    r, evs = _run(fake, cached=True)
    assert fake.calls == n, "cached run must not call the model"
    assert r["pass"] and r["cached"]
    assert [e for e in evs if e["type"] == "done"][0]["cached"] is True


def test_wrong_tolerance_fails_and_skips_gbrain(fake):
    calls = json.loads(fake.raw)
    for c in calls:
        if c["tool"] == "place_opening":
            c["args"]["tolerance"] = 0.5
    fake.raw = "<think></think>```json\n" + json.dumps(calls) + "\n```"
    r, evs = _run(fake)
    assert not r["pass"]
    assert not [c for c in r["checks"] if c["id"] == "openings"][0]["pass"]
    assert fake.records == []  # a failed refit never overwrites project memory
    assert r["gbrain_slug"] is None


def test_bad_calls_reported_not_raised(fake):
    fake.raw = json.dumps([{"tool": "place_standoff", "args": {"hole": "H99"}}, {"tool": "nope", "args": {}}, 5])
    r, evs = _run(fake)
    assert not r["pass"] and len(r["errors"]) == 3
    assert [e["ok"] for e in evs if e["type"] == "tool"] == [False, False, False]


def test_unparseable_and_river_errors_end_with_done(fake):
    fake.raw = "I cannot help with that."
    r, evs = _run(fake)
    assert not r["pass"] and "not a JSON list" in r["error"] and evs[-1]["type"] == "done"
    fake.raw = RuntimeError("connection refused")
    r, evs = _run(fake)
    assert not r["pass"] and "River error" in r["error"]
    assert evs[-1] == {**evs[-1], "type": "done", "pass": False}
    assert any(e["type"] == "log" and e["kind"] == "error" for e in evs)


def test_tuned_without_checkpoint_is_clear_error(fake, monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "CHECKPOINT_PATH", tmp_path / "missing.json")
    r, evs = _run(fake, model="tuned")
    assert not r["pass"] and "checkpoint.json" in r["error"]
    assert evs[-1]["type"] == "done" and evs[-1]["pass"] is False


def test_prompt_has_no_house_values():
    d = tasks.load("demo")
    text = prompts.SYSTEM + prompts.user_prompt(d)
    assert "house" not in json.dumps(kernel.HOUSE["tolerance"]).lower()
    assert "0.4" not in prompts.SYSTEM and "clearance 1.5" not in text


# ------------------------------------------------------------------ server
@pytest.fixture()
def client(fake):
    with TestClient(server.app) as c:
        c.post("/api/reset")
        yield c


def test_state_reset_revision(client):
    st = client.get("/api/state").json()
    for k in ("project", "board", "prev_board", "enclosure", "checks", "log", "running", "model"):
        assert k in st
    assert st["board"]["rev"] == "A" and st["prev_board"] is None
    assert all(c["pass"] for c in st["checks"])
    st = client.post("/api/revision").json()
    assert st["board"]["rev"] == "B" and st["prev_board"]["rev"] == "A"
    assert not all(c["pass"] for c in st["checks"])
    assert [c["id"] for c in st["checks"]] == ["cavity_fit", "cavity_height", "standoffs", "openings", "orphans"]


def test_qm_refit_from_rev_a(client):
    r = client.post("/api/qm/refit", json={"model": "base"})
    assert r.status_code == 200
    j = r.json()
    assert j["pass"] is True and j["viewer_url"].startswith("http")
    assert "PASS" in j["summary"] and "fit_cavity" in j["summary"] and j["viewer_url"] in j["summary"]
    st = client.get("/api/state").json()
    assert st["board"]["rev"] == "B" and st["running"] is False and st["model"] == "base"
    assert all(c["pass"] for c in st["checks"])


def test_run_conflict_and_bad_model(client):
    assert client.post("/api/agent/run", json={"model": "gpt"}).status_code == 400
    with server._lock:
        server.STATE["running"] = True
    try:
        assert client.post("/api/agent/run", json={"model": "base"}).status_code == 409
        assert client.post("/api/qm/refit", json={"model": "base"}).status_code == 409
    finally:
        with server._lock:
            server.STATE["running"] = False


def test_eval_endpoint(client, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "EVAL_PATH", tmp_path / "nope.json")
    assert client.get("/api/eval").status_code == 404
    p = tmp_path / "eval.json"
    p.write_text(json.dumps({"base_model": "x", "runs": [], "examples": []}))
    monkeypatch.setattr(server, "EVAL_PATH", p)
    assert client.get("/api/eval").json()["base_model"] == "x"


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "<html" in r.text.lower()


def test_hub_publish_threadsafe():
    import asyncio

    async def go():
        h = server.Hub()
        h.loop = asyncio.get_running_loop()
        q = asyncio.Queue()
        h.queues.add(q)
        import threading
        threading.Thread(target=h.publish, args=({"type": "log", "text": "hi"},)).start()
        return json.loads(await asyncio.wait_for(q.get(), 2))

    assert asyncio.run(go())["text"] == "hi"
