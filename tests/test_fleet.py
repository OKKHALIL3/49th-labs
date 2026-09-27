"""THE WALL: fleet data invariants + API with a fake sampler (no River calls)."""
import json
import os

os.environ.setdefault("FLEET_SOURCE", "synth")  # these invariants describe the synthetic fleet

import pytest
from fastapi.testclient import TestClient

from rev import fleet, kernel, prompts


def test_fleet_data_invariants():
    rows = fleet.load_fleet()
    assert len(rows) == 48
    assert len({t["id"] for t in rows}) == 48 and len({t["name"] for t in rows}) == 48
    assert sum(t["usb_a"] for t in rows) == (12 if "usb_a" in kernel.CONNECTOR_SPECS else 0)
    for t in rows:
        enc, errs = kernel.apply_calls(t["board_b"], t["enclosure_a"], t["gold_calls"])
        assert not errs and kernel.passes(t["board_b"], enc)
        assert not kernel.passes(t["board_b"], t["enclosure_a"])
        assert not any(c["type"] == "usb_a" for c in t["board_a"]["connectors"])


@pytest.fixture
def gold_sampler(monkeypatch, tmp_path):
    rows = {prompts.user_prompt(t): t for t in fleet.load_fleet()}

    def fake_sampler(model, key=None):
        def s(prompt, **kw):
            t = next(t for up, t in rows.items() if up in prompt)
            if model == "base" or (t["usb_a"] and key == "tuned"):
                return json.dumps([dict(c, args=dict(c["args"], tolerance=0.9)) if c["tool"] == "place_opening" else c
                                   for c in t["gold_calls"]])
            return prompts.completion(t)
        return s, None

    monkeypatch.setattr(fleet, "_sampler", fake_sampler)
    monkeypatch.setattr(fleet, "RESULTS_PATH", str(tmp_path / "fleet_results.json"))
    from rev import river_util as ru
    monkeypatch.setattr(ru, "render", lambda msgs: msgs[-1]["content"])


def test_run_and_replay(gold_sampler):
    ev = []
    done = fleet.run_fleet("tuned", ev.append, workers=8, key="tuned")
    assert done["total"] == 48 and done["passed"] == 48 - 12
    cells = [e for e in ev if e["type"] == "fleet_cell" and e["status"] != "running"]
    assert len(cells) == 48 and {e["status"] for e in cells if e["usb_a"]} == {"fail"}
    assert ev[0]["type"] == "fleet_start" and ev[-1]["type"] == "fleet_done"
    ev2 = []
    d2 = fleet.replay_fleet("tuned", ev2.append, speed=1000)
    assert d2["cached"] and d2["passed"] == 36
    assert sum(e["status"] == "pass" for e in ev2 if e["type"] == "fleet_cell") == 36


def test_api(gold_sampler):
    from rev.fleet_api import app
    c = TestClient(app)
    j = c.get("/api/fleet").json()
    assert j["n"] == 48 and len(j["tasks"]) == 48
    assert c.post("/api/fleet/run", json={"model": "nope"}).status_code == 400
    assert c.post("/api/fleet/run", json={"model": "base", "cached": True, "key": "missing"}).status_code == 404
    assert c.get("/fleet").status_code == 200
