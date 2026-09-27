"""Weight PRs: conflict check + ledger ops (no River, no GBrain)."""
import json

import pytest

from rev import weights_ci as w


def _rec(slug, *openings):
    return {"slug": slug, "id": slug, "summary": slug, "openings": list(openings), "source": "test"}


HIST = [_rec(f"history/eco-{i:03d}", ("hdmi", 0.6)) for i in range(5)] + [_rec("history/eco-100", ("sd", 1.0))]


def test_conflict_blocks_contradiction():
    cc = w.conflict_check("hdmi", 0.2, HIST)
    assert cc["verdict"] == "conflict"
    assert cc["differ"] == 5 and cc["their_tolerance"] == 0.6
    assert len(cc["evidence"]) == 3
    assert "contradicts 5 verified engineering changes" in cc["reason"]


def test_conflict_known_and_new_and_partial():
    assert w.conflict_check("hdmi", 0.6, HIST)["verdict"] == "known"
    assert w.conflict_check("usb_a", 0.5, HIST)["verdict"] == "new"
    few = HIST[:2] + [_rec("x", ("hdmi", 0.2))]
    assert w.conflict_check("hdmi", 0.2, few)["verdict"] == "partial"


def test_history_fallback_has_200_records(monkeypatch):
    import rev.gbrain_dataset as gd
    monkeypatch.setattr(gd, "build_dataset_from_gbrain", lambda kind="captures": [])
    recs = w.history_records()
    assert len(recs) == 200 and recs[0]["slug"].startswith("history/eco-")
    assert w.conflict_check("hdmi", 0.2, recs)["verdict"] == "conflict"
    assert w.conflict_check("usb_a", 0.5, recs)["verdict"] == "new"


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(w, "PRS_PATH", tmp_path / "prs.json")
    pointed = []
    monkeypatch.setattr(w, "_point_tuned_model_at", lambda v: pointed.append(v["checkpoint_file"]))
    w.reset()
    return pointed


def _merge(n, ctype, tol, ck):
    pr = {"pr": n, "title": f"{ctype} {tol}", "author": "Senior mechanical engineer", "source": "slack",
          "gbrain_slug": f"decisions/pr-{n}-{ctype}", "lesson": {"type": ctype, "tolerance": tol},
          "tests": {"lesson": {"before": {"passed": 0, "total": 12}, "after": {"passed": 12, "total": 12}},
                    "regression": {"passed": 29, "total": 30}},
          "candidate": {"river_checkpoint": f"river://{ck}", "checkpoint_file": ck}}
    return w.merge(pr)


def test_ledger_seed_merge_blame_revert(ledger):
    led = w.load_ledger()
    assert [v["version"] for v in led] == ["v1"] and led[0]["status"] == "production"
    assert led[0]["tests"]["regression"]["total"] == 30
    v2 = _merge(2, "usb_a", 0.5, "checkpoint_pr2.json")
    led = w.load_ledger()
    assert v2["version"] == "v2" and v2["parent"] == "v1"
    assert [v["status"] for v in led] == ["superseded", "production"]
    assert ledger[-1] == "checkpoint_pr2.json"
    b = w.blame("usb_a", history=HIST)
    assert b["version"] == "v2" and b["pr"] == 2
    assert w.blame("hdmi", history=HIST)["version"] == "v1"
    v3 = w.revert("v1")
    assert v3["version"] == "v3" and v3["reverts"] == "v1" and ledger[-1] == "checkpoint.json"
    assert w.production()["version"] == "v3"
    assert w.blame("usb_a", history=HIST)["version"] is None      # reverted away
    w.revert("v2")
    assert w.blame("usb_a", history=HIST)["version"] == "v2"
    w.reset()
    assert [v["version"] for v in w.load_ledger()] == ["v1"] and w.load_prs() == []


def test_pr_numbers(ledger):
    assert w.reserve_pr_number() == 1
    assert w.reserve_pr_number() == 2
    assert json.loads(w.PRS_PATH.read_text())[1]["pr"] == 2
