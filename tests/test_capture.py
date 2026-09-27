"""Workstation capture: scad round-trip, watcher inference + verification.

    /opt/anaconda3/bin/python3 -m pytest -q tests/test_capture.py
"""
import json
import os
import re
import time

import pytest

from rev import capture_api, kernel, scad, watcher


@pytest.fixture
def ws(tmp_path):
    res = capture_api.checkout("fleet", directory=tmp_path)
    side = json.loads(watcher.sidecar_path(tmp_path / res["file"]).read_text())
    events = []
    w = watcher.Watcher(tmp_path, captures_path=tmp_path / "captures.jsonl", gbrain=False,
                        emit=events.append, quiet=True)
    assert w.tick() == []          # baseline: freshly checked-out file -> no capture
    return tmp_path / res["file"], side, w, events


def _usb_a(side):
    return next(c for c in side["board"]["connectors"] if c["type"] == "usb_a")


def _set(path, name, value):
    src = path.read_text()
    new, n = re.subn(rf"^{name} = [^;]*;", f"{name} = {scad.fmt(value)};", src, flags=re.M)
    assert n == 1, name
    time.sleep(0.01)
    path.write_text(new)
    os.utime(path, None)


def test_roundtrip_exact():
    rows = json.loads(open(os.path.join(capture_api.DATA_DIR, "fleet.json")).read())
    for t in rows[:12] + [json.loads(open(os.path.join(capture_api.DATA_DIR, "demo_rev_c.json")).read())]:
        for board, enc in ((t["board_a"], t["enclosure_a"]), (t["board_b"], kernel.enclosure_for(t["board_b"]))):
            src = scad.export(board, enc, "Test Product")
            p = scad.parse_params(src)
            assert "cavity_w" in p and "// Edit and save" in src
            assert scad.to_enclosure(p, board, enc) == enc


def test_parse_rejects_half_written():
    d = json.loads(open(os.path.join(capture_api.DATA_DIR, "demo.json")).read())
    src = scad.export(d["board_a"], d["enclosure_a"], "X")
    with pytest.raises(ValueError):
        scad.parse_params(src[: src.find(scad.END) - 30])
    with pytest.raises(ValueError):
        scad.parse_params(src.replace("cavity_w = ", "cavity_w = 9..1 + "))
    assert scad.parse_params("a = 13.14 + 2*0.5;\n") == {"a": 14.14}


def test_verified_usb_a_capture(ws):
    path, side, w, events = ws
    c = _usb_a(side)
    pre = scad.opening_prefix(side["board"], c["id"])
    _set(path, f"{pre}_w", c["w"] + 2 * 0.5)
    _set(path, f"{pre}_h", c["h"] + 2 * 0.5)
    recs = w.tick()
    assert len(recs) == 1
    r = recs[0]
    assert r["verified"] is True, r["verification"]
    assert r["inferred_rule"] == {"type": "usb_a", "tolerance": 0.5}
    assert r["calls"] == [{"tool": "place_opening", "args": {"connector": c["id"], "tolerance": 0.5}}]
    assert r["actor"] == "senior-engineer" and {d["param"] for d in r["diff"]} == {f"{pre}_w", f"{pre}_h"}
    assert events and events[0]["type"] == "capture"
    assert len(watcher.read_captures(w.captures_path)) == 1
    assert w.tick() == []          # no re-capture of the same save
    md = watcher.capture_markdown(r)
    assert "eco-capture" in md and "```json" in md


def test_rejected_opening_too_small(ws):
    path, side, w, _ = ws
    c = _usb_a(side)
    pre = scad.opening_prefix(side["board"], c["id"])
    _set(path, f"{pre}_w", c["w"] - 1.0)
    recs = w.tick()
    assert len(recs) == 1 and recs[0]["verified"] is False and recs[0]["inferred_rule"] is None
    assert any(not v["pass"] and v["check"] == "opening_fits_connector" for v in recs[0]["verification"])


def test_invalid_file_never_crashes(ws):
    path, side, w, _ = ws
    good = path.read_text()
    path.write_text(good[: len(good) // 3])      # half-written save
    assert w.tick() == []
    path.write_text(good.replace("cavity_w = ", "cavity_w = oops"))
    assert w.tick() == []
    path.write_text(good)
    assert w.tick() == []                        # back to the baseline -> nothing to capture


def test_record_readable_by_gbrain_dataset(ws):
    from rev import gbrain_dataset as gd
    path, side, w, _ = ws
    c = _usb_a(side)
    pre = scad.opening_prefix(side["board"], c["id"])
    _set(path, f"{pre}_w", c["w"] + 1.0)
    _set(path, f"{pre}_h", c["h"] + 1.0)
    r = w.tick()[0]
    back = gd.parse_record(watcher.capture_markdown(r), r["gbrain_slug"] or "captures/x")
    assert back["verified"] is True
    assert gd.observations(back)[0]["type"] == "usb_a" and gd.observations(back)[0]["tolerance"] == 0.5
