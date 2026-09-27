"""Plain-assert tests for rev.kernel / rev.tasks / rev.prompts. Run: python3 tests/test_kernel.py"""
import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rev import kernel, prompts, tasks  # noqa: E402

IDS = ["cavity_fit", "cavity_height", "standoffs", "openings", "orphans"]
LABELS = ["Board-to-wall clearance", "Headroom above tallest part", "Standoffs on mounting holes",
          "Connector openings", "No orphan features"]


def test_demo_numbers():
    d = tasks.demo_task()
    enc = d["enclosure_a"]
    assert enc["cavity"] == {"w": 88.0, "d": 59.0, "h": 21.6}, enc["cavity"]
    assert enc["board_origin"] == {"x": 1.5, "y": 1.5}
    assert enc["standoffs"][0] == {"hole": "H1", "x": 5.0, "y": 5.0, "dia": 5.5, "bore": 2.2}
    assert enc["openings"][0] == {"connector": "J1", "side": "front", "pos": 35.5, "z": 8.23, "w": 9.74, "h": 4.06}
    assert d["board_b"]["w"] == 91.0
    res = kernel.check(d["board_a"], enc)
    assert [c["id"] for c in res] == IDS and [c["label"] for c in res] == LABELS
    assert all(c["pass"] for c in res)
    stale = {c["id"]: c for c in kernel.check(d["board_b"], enc)}
    assert not stale["cavity_fit"]["pass"] and stale["cavity_fit"]["refs"] == ["board"]
    assert not stale["standoffs"]["pass"] and stale["standoffs"]["refs"] == ["H2", "H3"]
    assert not stale["openings"]["pass"] and stale["openings"]["refs"] == ["J1", "J4"]
    enc_b, errs = kernel.apply_calls(d["board_b"], enc, d["gold_calls"])
    assert not errs and kernel.passes(d["board_b"], enc_b)
    # saved file matches generator
    assert tasks.load("demo") == json.loads(json.dumps(d))


def test_tools():
    d = tasks.demo_task()
    b, enc = d["board_b"], d["enclosure_a"]
    for bad in [{"tool": "nope", "args": {}},
                {"tool": "place_standoff", "args": {"hole": "H99"}},
                {"tool": "place_opening", "args": {"connector": "J1"}},
                {"tool": "place_opening", "args": {"connector": "J99", "tolerance": 0.2}},
                {"tool": "fit_cavity", "args": {"clearance": "x", "headroom": 1}},
                {"tool": "remove_opening", "args": {"connector": "J99"}},
                {"tool": "remove_standoff", "args": {}}, "garbage"]:
        try:
            kernel.apply_call(b, enc, bad)
            assert False, f"expected ToolError for {bad}"
        except kernel.ToolError:
            pass
    before = copy.deepcopy(enc)
    e2 = kernel.apply_call(b, enc, {"tool": "remove_opening", "args": {"connector": "J2"}})
    assert enc == before, "apply_call must be pure"
    assert len(e2["openings"]) == len(enc["openings"]) - 1
    # upsert, not duplicate
    e3 = kernel.apply_call(b, enc, {"tool": "place_opening", "args": {"connector": "J1", "tolerance": 0.4}})
    assert len(e3["openings"]) == len(enc["openings"])
    assert e3["openings"][0]["pos"] == 21.5
    e4, errs = kernel.apply_calls(b, enc, [{"tool": "bogus"}, {"tool": "place_standoff", "args": {"hole": "H2"}}])
    assert len(errs) == 1 and e4["standoffs"][1]["x"] == 89.0
    # orphans
    e5 = kernel.apply_call(d["board_a"], enc, {"tool": "place_opening", "args": {"connector": "J3", "tolerance": 0.8}})
    b2 = copy.deepcopy(d["board_a"]); b2["connectors"] = b2["connectors"][:2]
    o = kernel.check(b2, e5)[4]
    assert not o["pass"] and o["refs"] == ["J3"]


def test_all_tasks():
    train, test = tasks.load("train"), tasks.load("test")
    assert len(train) == 200 and len(test) == 30
    keys = {tasks._key(t) for t in train}
    assert not any(tasks._key(t) in keys for t in test), "train/test overlap"
    for t in train + test:
        assert set(["id", "board_a", "enclosure_a", "board_b", "change_summary", "gold_calls"]) <= set(t)
        assert t["enclosure_a"] == kernel.enclosure_for(t["board_a"])
        assert kernel.passes(t["board_a"], t["enclosure_a"])
        enc, errs = kernel.apply_calls(t["board_b"], t["enclosure_a"], t["gold_calls"])
        assert not errs, (t["id"], errs)
        assert kernel.passes(t["board_b"], enc), t["id"]
        assert not kernel.passes(t["board_b"], t["enclosure_a"]), t["id"]
        assert 1 <= len(t["kinds"]) <= 3
        assert tasks.board_valid(t["board_b"]) and tasks.board_valid(t["board_a"])
        for b in (t["board_a"], t["board_b"]):
            for v in [b["w"], b["d"]] + [h[k] for h in b["holes"] for k in ("x", "y")] + [c["pos"] for c in b["connectors"]]:
                assert abs(v * 2 - round(v * 2)) < 1e-9, (t["id"], v)
        # gold order: fit_cavity, removals, places
        order = {"fit_cavity": 0, "remove_standoff": 1, "remove_opening": 1, "place_standoff": 2, "place_opening": 2}
        seq = [order[c["tool"]] for c in t["gold_calls"]]
        assert seq == sorted(seq), t["id"]
        assert prompts.parse_calls(prompts.completion(t)) == t["gold_calls"]
    frac_new = sum(t["new_type_added"] for t in train) / len(train)
    assert frac_new >= 0.6, frac_new
    # regenerate deterministically
    tr2, te2, _ = tasks.build()
    assert tr2 == train and te2 == test


def test_prompts():
    for v in ["1.5", "3.0", "0.4", "0.6", "0.3", "0.8", "1.0", "5.0", "5.5", "2.0", "0.5"]:
        assert v not in prompts.SYSTEM, v
        assert v not in kernel.TOOLS_DOC, v
    t = tasks.load("demo")
    up = prompts.user_prompt(t)
    assert "Rev B" in up and t["change_summary"] in up
    m = prompts.messages(t)
    assert m[0]["role"] == "system" and m[1]["role"] == "user"
    gold = t["gold_calls"]
    s = json.dumps(gold)
    for txt in [s, f"<think>maybe [1,2]</think>\n{s}", f"```json\n{s}\n```\nDone.", f"Sure! Here: {s} hope it helps [x]",
                f"<think>\nhmm {{\"a\":1}} [\n</think>```\n{s}```", f"[1, 2] then {s}", json.dumps({"calls": gold})]:
        assert prompts.parse_calls(txt) == gold, txt
    assert prompts.parse_calls("no json here") is None
    assert prompts.parse_calls("[]") == []
    assert prompts.parse_calls('[{"name":"fit_cavity","arguments":{"clearance":1,"headroom":2}}]') == \
        [{"tool": "fit_cavity", "args": {"clearance": 1, "headroom": 2}}]


if __name__ == "__main__":
    test_demo_numbers(); print("ok demo")
    test_tools(); print("ok tools")
    test_prompts(); print("ok prompts")
    test_all_tasks(); print("ok all tasks")
    for split in ("train", "test"):
        st = tasks.stats(tasks.load(split))
        print(split, json.dumps(st))
    print("ALL PASS")
