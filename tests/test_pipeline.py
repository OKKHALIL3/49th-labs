"""GBrain -> lessons pipeline tests (no River needed).
Run: /opt/anaconda3/bin/python3 -m pytest -q tests/test_pipeline.py"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["REV_GBRAIN_HOME"] = tempfile.mkdtemp(prefix="rev-gbrain-pipeline-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rev import gbrain_dataset as gd  # noqa: E402
from rev import gbrain_history as gh  # noqa: E402


def cap(cid, ctype="usb_a", tol=0.5, verified=True):
    return {"kind": "capture", "id": cid, "by": "senior-engineer", "verified": verified,
            "observations": [{"connector": "J5", "type": ctype, "tolerance": tol}]}


def test_agreeing_captures_become_one_lesson():
    lessons = gd.derive_lessons([cap("c1", tol=0.5), cap("c2", tol=0.52), cap("c3", tol=0.5)])
    assert lessons == [{"type": "usb_a", "tolerance": 0.5, "evidence": ["c1", "c2", "c3"], "n": 3}]


def test_single_capture_is_enough():
    assert gd.derive_lessons([cap("c1")]) == [{"type": "usb_a", "tolerance": 0.5, "evidence": ["c1"], "n": 1}]


def test_conflicting_captures_are_flagged_not_learned():
    rep = gd.derive_lessons_report([cap("c1", tol=0.5), cap("c2", tol=0.8), cap("c3", "rj45", 0.3)])
    assert [l["type"] for l in rep["lessons"]] == ["rj45"]
    assert rep["conflicts"][0]["type"] == "usb_a" and rep["conflicts"][0]["tolerances"] == [0.5, 0.8]
    assert gd.derive_lessons([cap("c1", tol=0.5), cap("c2", tol=0.8)]) == []


def test_unverified_captures_are_ignored():
    rep = gd.derive_lessons_report([cap("c1", tol=0.5), cap("bad", tol=0.9, verified=False)])
    assert rep["lessons"][0]["evidence"] == ["c1"] and rep["unverified"] == ["bad"]


def test_tolerance_from_calls_and_board():
    rec = {"id": "c9", "verified": True,
           "board": {"connectors": [{"id": "J5", "type": "usb_a"}, {"id": "J1", "type": "usb_c"}]},
           "calls": [{"tool": "place_opening", "args": {"connector": "J5", "tolerance": 0.5}},
                     {"tool": "place_opening", "args": {"connector": "J1", "tolerance": 0.4}},
                     {"tool": "fit_cavity", "args": {"clearance": 1.5, "headroom": 3.0}}]}
    lessons = {l["type"]: l["tolerance"] for l in gd.derive_lessons([rec])}
    assert lessons == {"usb_a": 0.5, "usb_c": 0.4}
    assert gd.pick_lesson(gd.derive_lessons([rec]))["type"] == "usb_a"   # new-to-history type preferred


def test_capture_page_roundtrip_markdown():
    md = gd.capture_markdown(cap("c7"))
    rec = gd.parse_record(md, "captures/c7")
    assert rec["verified"] is True and rec["id"] == "c7" and gd.observations(rec)[0]["tolerance"] == 0.5


def test_eco_markdown_parses_back():
    t = gh.load_history(1)[0]
    rec = gd.parse_record(gh.eco_markdown(t), gh.eco_slug(t["id"]))
    assert rec["verified"] is True and rec["calls"] == t["gold_calls"] and rec["by"] == "senior-engineer"


def test_gbrain_roundtrip_captures_and_learn_dry_run():
    assert gd.write_capture(cap("cap-usb-a-fix"))
    assert gd.write_capture(cap("cap-draft", tol=0.9, verified=False))
    recs = gd.build_dataset_from_gbrain("captures")
    assert [r["id"] for r in recs] == ["cap-usb-a-fix"]
    events = []
    res = gd.learn_from_gbrain(emit=events.append, dry_run=True)
    assert res["lesson"]["type"] == "usb_a" and res["lesson"]["tolerance"] == 0.5
    assert events[0]["stage"] == "gbrain" and "lesson usb_a 0.5 mm" in events[0]["msg"]
    assert gh.stats()["capture_records"] == 2


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
