"""GBrain round-trip tests. Uses a throwaway brain (REV_GBRAIN_HOME) so the project brain isn't polluted.
Run: /opt/anaconda3/bin/python3 -m pytest -q tests/test_gbrain.py   (or python3 tests/test_gbrain.py)"""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["REV_GBRAIN_HOME"] = tempfile.mkdtemp(prefix="rev-gbrain-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rev import gbrain_io as g  # noqa: E402

BOARD_A = {"rev": "A", "name": "Sensor Hub", "w": 85.0, "d": 56.0, "t": 1.6, "max_component_h": 12.0,
           "holes": [{"id": "H1", "x": 3.5, "y": 3.5, "dia": 2.7}, {"id": "H2", "x": 81.5, "y": 3.5, "dia": 2.7}],
           "connectors": [{"id": "J1", "type": "usb_c", "side": "front", "pos": 34.0, "z": 1.63, "w": 8.94, "h": 3.26}]}
BOARD_B = {**BOARD_A, "rev": "B", "w": 91.0,
           "holes": [{"id": "H1", "x": 3.5, "y": 3.5, "dia": 2.7}, {"id": "H2", "x": 87.5, "y": 3.5, "dia": 2.7}],
           "connectors": [{**BOARD_A["connectors"][0], "pos": 20.0}]}


def _t(label, fn, *a):
    t0 = time.time()
    r = fn(*a)
    print(f"  {label}: {time.time() - t0:.2f}s")
    return r


def test_put_get_search_list_roundtrip():
    assert _t("put_page", g.put_page, "demo/widget", "Widget Notes", "# Widget Notes\nThe zebra gasket seals the lid.\n")
    body = _t("get_page", g.get_page, "demo/widget")
    assert body and "zebra gasket" in body and not body.startswith("---")
    hits = _t("search", g.search, "zebra gasket")
    assert hits and hits[0]["slug"] == "demo/widget" and hits[0]["title"] == "Widget Notes" and "zebra" in hits[0]["snippet"]
    # overwrite (same slug) must replace content
    assert g.put_page("demo/widget", "Widget Notes", "# Widget Notes\nNow with a walrus latch.\n")
    assert "walrus" in (g.get_page("demo/widget") or "")
    assert any(p["slug"] == "demo/widget" for p in _t("list_pages", g.list_pages))


def test_missing_and_bad_inputs_never_raise():
    assert g.get_page("does/not-exist") is None
    assert g.search("") == []
    assert g.search("qwxyzzy nonexistentterm") == []


def test_seed_and_change_record_answer_why_question():
    assert set(g.seed()) == {"sensor-hub", "sensor-hub/rev-a-enclosure"}
    calls = [{"tool": "fit_cavity", "args": {"clearance": 1.5, "headroom": 3.0}},
             {"tool": "place_standoff", "args": {"hole": "H2"}},
             {"tool": "place_opening", "args": {"connector": "J1", "tolerance": 0.4}}]
    checks = [{"id": "cavity_fit", "label": "Board-to-wall clearance", "pass": True, "detail": "ok", "refs": []}]
    slug = _t("write_change_record", g.write_change_record, BOARD_A, BOARD_B, calls, checks, "rev-tuned")
    assert slug == "sensor-hub/rev-b-enclosure-change"
    page = g.get_page(slug)
    assert page and "J1" in page and "rev-tuned" in page and "34 mm to front 20 mm" in page
    hits = g.search("why is the USB-C opening here?")
    assert any(h["slug"] == slug for h in hits), hits


def test_house_rules_not_in_seed_pages():
    for p in (g.BRAIN_DIR / "sensor-hub.md", g.BRAIN_DIR / "sensor-hub" / "rev-a-enclosure.md"):
        low = p.read_text().lower()
        assert "tolerance" not in low and "clearance" not in low and "headroom" not in low


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            print(name)
            fn()
    print("OK")
