"""The 49th Engineer (49th Labs) -- skill transfer between engineer agents, via named checkpoints.

    49th checkpoints                                   lineage tree + which agent uses which checkpoint
    49th agents                                        agent -> checkpoint
    49th test --checkpoint NAME --suite heldout|usb_a [--limit N] [--cases]    LIVE eval, saved per case
    49th pr --from senior-me [--approve] [--replay]    skill pull request -> company candidate -> tests -> gate
    49th inherit --agent junior-me --from company-v2   explicit rebase / new agent, then ask it (Rev C, USB-A)

Every number printed comes from real model outputs graded by the deterministic enclosure checker, or from the
recorded PR run (labelled "prerecorded real run"). Test cases are synthetic Acme Devices history, held out.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from rev import checkpoints as C

DATA = C.DATA_DIR
EVAL_DIR = DATA / "evals"
SUITES = {"heldout": DATA / "test.jsonl", "usb_a": DATA / "usb_a_test.jsonl"}
SUITE_LABEL = {"heldout": "regular held-out suite (other connectors: scope / regression)",
               "usb_a": "usb_a suite (where the lesson applies)"}
CHECKS = ["cavity_fit", "cavity_height", "standoffs", "openings", "orphans"]
TEAM = ("senior-me", "junior-me")
con = Console(width=110, highlight=False)
ACC, OK, BAD, DIM = "bold #d97757", "bold green", "bold red", "grey58"


def _head(title, sub=""):
    con.print(Text.assemble((" 49th Labs ", "bold black on #d97757"), ("  The 49th Engineer  ", "bold"),
                            (f"· {title}", ACC), (f"   {sub}" if sub else "", DIM)))


def _users(name):
    return [a for a, v in C.agents().items() if v.get("checkpoint") == name]


def _team_ckpts():
    return [c for c in C.list_checkpoints() if not c.get("engineer") or c["engineer"] in TEAM]


# ---------------------------------------------------------------------------------------------- checkpoints
def cmd_checkpoints(_a):
    _head("checkpoints", "named checkpoints resolved from rev/data (ledger + engineer branches)")
    cks = _team_ckpts()
    kids = {}
    for c in cks:
        kids.setdefault(c["parent"], []).append(c)

    def label(c):
        users = _users(c["name"])
        t = Text.assemble((c["name"], ACC), ("  " + C.short_ref(c["river_ref"]), DIM))
        if c.get("status"):
            t.append(f"  [{c['status']}]", OK if c["status"] == "production" else DIM)
        if users:
            t.append("  ← used by " + ", ".join(users), "bold cyan")
        t.append("\n" + c["trained_from"], "white")
        tests = c.get("tests") or {}
        if tests:
            parts = []
            if tests.get("lesson"):
                la = tests["lesson"]["after"]
                parts.append(f"usb_a {la['passed']}/{la['total']}")
            if tests.get("regression"):
                parts.append(f"held-out {tests['regression']['passed']}/{tests['regression']['total']}")
            t.append("\ntests at publish: " + " · ".join(parts), DIM)
        return t

    def add(node, parent):
        for c in kids.get(parent, []):
            add(node.add(label(c)), c["name"])

    base = next(c for c in cks if c["name"] == C.BASE_NAME)
    root = Tree(label(base), guide_style="#d97757")
    add(root, C.BASE_NAME)
    con.print(Panel(root, title="lineage", border_style="#d97757", box=box.ROUNDED))
    con.print(Text("company-vN = approved examples promoted + company CANDIDATE retrained from production "
                   "(not a parameter merge). A new company version never changes a personal checkpoint.", DIM))


def cmd_agents(_a):
    _head("agents", "which checkpoint each agent samples from")
    t = Table(box=box.SIMPLE_HEAVY, header_style=ACC)
    for h in ("agent", "role", "checkpoint", "river ref", "since", "how"):
        t.add_column(h)
    roles = C._roles()
    for a, v in C.agents().items():
        c = C.get(v["checkpoint"]) or {}
        t.add_row(a, roles.get(a, "agent"), v["checkpoint"], C.short_ref(c.get("river_ref")), v.get("since", "")[11:16],
                  v.get("how", ""))
    con.print(t)


# ---------------------------------------------------------------------------------------------- test
def _tolerances(task, calls):
    types = {c["id"]: c["type"] for c in task["board_b"]["connectors"]}
    usb, other = [], []
    for c in calls or []:
        if isinstance(c, dict) and c.get("tool") == "place_opening":
            a = c.get("args") or {}
            ty = types.get(a.get("connector"), "?")
            (usb if ty == "usb_a" else other).append(f"{a.get('tolerance')}" if ty == "usb_a"
                                                     else f"{ty}:{a.get('tolerance')}")
    return ", ".join(usb) or "-", ", ".join(other) or "-"


def _short(task):
    s = task.get("change_summary") or ""
    return s if len(s) <= 44 else s[:43] + "…"


def run_suite(name, suite, limit=None, workers=12, on_case=None):
    from rev import eval as ev
    from rev import prompts
    from rev import river_util as ru
    ck = C.resolve(name)
    rows = [json.loads(l) for l in SUITES[suite].read_text().splitlines() if l.strip()]
    if limit:
        rows = rows[:limit]
    out = [None] * len(rows)
    if rows:  # warm the tokenizer/renderer once (parallel first-loads race on the vocab file)
        ru.render(prompts.messages(rows[0]))

    def job(i):
        t = rows[i]
        pt = ru.render(prompts.messages(t))
        t0 = time.time()
        err, text = None, ""
        for k in range(5):
            try:
                kw = {"max_tokens": 768, "temperature": 0.0}
                text = ru.sample_base(pt, **kw) if ck is None else ru.sample_tuned(pt, checkpoint=ck, **kw)
                err = None
                break
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
                time.sleep(2 * (k + 1))
        g = ev.grade(t, text)
        usb, oth = _tolerances(t, g["calls"])
        r = {"id": t.get("id", str(i)), "change": t.get("change_summary"), "pass": g["pass"], "checks": g["checks"],
             "failed": g["failed"], "failed_detail": g["failed_detail"], "parse_error": g["parse_error"],
             "tool_errors": g["tool_errors"], "calls": g["calls"], "usb_a_tol": usb, "other_tol": oth,
             "raw": text, "sample_error": err, "latency_s": round(time.time() - t0, 2)}
        out[i] = r
        if on_case:
            on_case(i, r, t)
        return r

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(job, range(len(rows))))
    return rows, out


def save_run(name, suite, results, extra=None):
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    c = C.get(name) or {}
    ts = time.strftime("%Y%m%d-%H%M%S")
    p = EVAL_DIR / f"{name}-{suite}-{ts}.json"
    p.write_text(json.dumps({"checkpoint": name, "river_ref": c.get("river_ref"), "base_model": C.BASE_MODEL,
                             "suite": suite, "suite_file": str(SUITES[suite].relative_to(C.DATA_DIR.parent.parent)),
                             "cases_label": "synthetic Acme Devices history (held out, never trained on)",
                             "grader": "rev.eval.grade (deterministic enclosure checker)",
                             "passed": sum(r["pass"] for r in results), "total": len(results),
                             "by_check": {k: sum(r["checks"].get(k, False) for r in results) for k in CHECKS},
                             "evaluated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "cases": results, **(extra or {})},
                            indent=1, default=str))
    return p


def cmd_test(a):
    c = C.get(a.checkpoint)
    if c is None:
        con.print(f"[red]unknown checkpoint {a.checkpoint}[/]; try: 49th checkpoints")
        return 2
    _head(f"test {c['name']}", SUITE_LABEL[a.suite])
    con.print(Text.assemble(("checkpoint ", DIM), (c["name"], ACC), ("  ", ""), (C.short_ref(c["river_ref"]), DIM),
                            ("  · trained from: ", DIM), (c["trained_from"], "white")))
    con.print(Text("cases: synthetic Acme history (held out, never trained on) · grader: deterministic enclosure "
                   "checker · 12 workers · LIVE", DIM))
    lock = threading.Lock()
    done = {}

    def table():
        t = Table(box=box.SIMPLE, header_style=ACC, expand=False, pad_edge=False)
        t.add_column("case", style="white", no_wrap=True)
        t.add_column("change", max_width=40, no_wrap=True)
        t.add_column("result", no_wrap=True)
        t.add_column("failed checks", no_wrap=True, max_width=24)
        t.add_column("usb_a tol", justify="right", no_wrap=True)
        t.add_column("other tol", max_width=18, no_wrap=True)
        for i in sorted(done):
            r = done[i]
            res = Text("PASS", OK) if r["pass"] else Text("FAIL", BAD)
            if r["sample_error"]:
                res = Text("ERR", BAD)
            t.add_row(r["id"], (r["change"] or "")[:44], res, ", ".join(r["failed"]) or "-", r["usb_a_tol"],
                      r["other_tol"])
        n = len(done)
        p = sum(r["pass"] for r in done.values())
        return Group(t, Text(f"  {n} graded · {p} pass", DIM))

    with Live(table(), console=con, refresh_per_second=6) as live:
        def on_case(i, r, t):
            with lock:
                done[i] = r
                live.update(table())
        t0 = time.time()
        rows, res = run_suite(c["name"], a.suite, a.limit, 12, on_case)
    con.print()
    p = save_run(c["name"], a.suite, res, {"seconds": round(time.time() - t0, 1)})
    passed = sum(r["pass"] for r in res)
    con.print(Text.assemble((f"{c['name']} · {a.suite} suite: ", "bold"),
                            (f"{passed}/{len(res)}", OK if passed == len(res) else (BAD if passed == 0 else ACC))))
    con.print(Text(f"per-case outputs (raw model text + checker results): {p.relative_to(C.DATA_DIR.parent.parent)}",
                   DIM))
    if a.cases:
        for r in res:
            con.print(Panel(Text(r["raw"][:700] or "(empty)"), title=f"{r['id']} · {'PASS' if r['pass'] else 'FAIL'}",
                            border_style="green" if r["pass"] else "red", box=box.MINIMAL))
    return 0


# ---------------------------------------------------------------------------------------------- pr --from
def _latest_lesson(eid):
    les = C._rj(C.ENG_DIR / eid / "lessons.json", []) or []
    return les[-1] if les else None


def _capture_for(eid, lesson):
    caps = C.ENG_DIR / eid / "captures.jsonl"
    if not caps.exists():
        return None
    rows = [json.loads(l) for l in caps.read_text().splitlines() if l.strip()]
    return rows[-1] if rows else None


def cmd_pr(a):
    from rev import learn
    from rev import weights_ci as wc
    eid = a.from_
    roles = C._roles()
    role = roles.get(eid, eid)
    les = _latest_lesson(eid)
    if not les:
        con.print(f"[red]{eid} has no reviewed lesson yet[/] (49th record / 49th teach first)")
        return 2
    personal = next((c for c in C.list_checkpoints() if c.get("engineer") == eid), None)
    prod = wc.production()
    cap = _capture_for(eid, les)
    ty = wc.DISPLAY.get(les["type"], les["type"])
    _head("skill pull request", f"from {eid} ({role})")
    src = Table.grid(padding=(0, 2))
    src.add_column(style=DIM)
    src.add_column()
    src.add_row("Proposed lesson", Text(f"{ty} opening tolerance = {les['tolerance']} mm per side", "bold"))
    src.add_row("", Text(f"“{les['text']}”", "italic"))
    src.add_row("Source", f"GBrain {les.get('gbrain_slug') or '-'} · via {les.get('source', 'chat')}")
    if cap:
        f = cap.get("file") or cap.get("path") or "-"
        diff = cap.get("diff") or cap.get("summary") or ""
        if isinstance(diff, list):
            diff = "; ".join(f"{d.get('param')} {d.get('before')}→{d.get('after')}" if isinstance(d, dict) else str(d)
                             for d in diff[:2])
        when = cap.get("ts") or cap.get("time") or ""
        src.add_row("", f"reviewed CAD change: {f} · {str(diff)[:72]} {when}")
    src.add_row("Author", f"{role} ({eid}) · {les.get('ts', '')}")
    if personal:
        src.add_row("Personal branch", f"{personal['name']}  {C.short_ref(personal['river_ref'])}  "
                                       f"(usb_a {les['before']['passed']}/{les['before']['total']} → "
                                       f"{les['after']['passed']}/{les['after']['total']} when taught)")
    src.add_row("Scope", f"{ty} openings; must not change other connector types")
    src.add_row("Permission", Text("requires approval to share company-wide", "bold yellow"))
    src.add_row("Plan", f"add {learn.N_LESSON} approved, checker-verified training examples + {learn.N_REPLAY} "
                        f"replayed history examples; retrain company candidate from company-{prod['version']} on River;")
    src.add_row("", f"tests: new-skill usb_a suite ({learn.N_TEST} cases), scope/regression held-out suite (30 cases); "
                    f"gate: usb_a ≥ 80% and held-out ≥ production − 1")
    con.print(Panel(src, title=f"Skill PR · {ty} {les['tolerance']} mm", border_style="#d97757", box=box.ROUNDED))
    if not a.approve:
        if not sys.stdin.isatty() or input("Approve sharing company-wide? [y/N] ").strip().lower() not in ("y", "yes"):
            con.print(Text("not approved -- nothing trained; personal checkpoints unchanged", DIM))
            return 0
    con.print(Text.assemble(("approved", OK), (f" · {'prerecorded real run (replayed with recorded timing)' if a.replay else 'LIVE run on River'}",
                                               "bold yellow" if a.replay else ACC)))

    def emit(e):
        st, msg = e.get("stage"), e.get("msg", "")
        d = e.get("data") or {}
        if st == "testing" and d.get("done") and d.get("done") not in (d.get("total"),) and d["done"] % 10:
            return
        col = {"merged": OK, "blocked": BAD, "training": "cyan", "testing": "magenta"}.get(st, "white")
        if st == "merged":  # retrained candidate passed the gate and was promoted -- not a parameter merge
            st, msg = "promoted", msg.replace("Merged as v", "Gate passed · candidate promoted to company-v")
        loss = f" loss {d['loss']:.4f}" if isinstance(d.get("loss"), (int, float)) else ""
        con.print(Text.assemble((f"{e.get('t', 0):>6.1f}s ", DIM), (f"{st:<15}", col), (msg[:80] + loss, "")))

    author = role
    if a.replay:
        res = wc.replay_pr(les["text"], author_role=author, source=f"engineer:{eid}", emit=emit)
    else:
        res = wc.open_pr(les["text"], author_role=author, source=f"engineer:{eid}", emit=emit)
    cand = res.get("candidate") or {}
    tests = res.get("tests") or {}
    con.print()
    g = Table.grid(padding=(0, 2))
    g.add_column(style=DIM)
    g.add_column()
    g.add_row("PR", f"#{res.get('pr')} · {res.get('title')}  {'(prerecorded real run)' if res.get('replayed') else ''}")
    g.add_row("candidate", f"{cand.get('river_checkpoint', '-')}  ({cand.get('checkpoint_file', '-')}, "
                           f"{cand.get('steps', '?')} steps, {cand.get('verified', '?')} verified + "
                           f"{cand.get('replay', '?')} replay examples)")
    if tests.get("lesson"):
        b, af = tests["lesson"]["before"], tests["lesson"]["after"]
        g.add_row("usb_a suite", f"{b['passed']}/{b['total']} → {af['passed']}/{af['total']}")
    if tests.get("regression"):
        pr_ = tests.get("production_regression") or {}
        g.add_row("held-out", f"{tests['regression']['passed']}/{tests['regression']['total']} "
                              f"(production {pr_.get('passed')}/{pr_.get('total')})")
    _dec = (f"gate passed: retrained candidate promoted to company-{res.get('version')} (not a parameter merge)"
            if res.get("status") == "merged" else f"{res.get('status')}: {res.get('reason')}")
    g.add_row("decision", Text(_dec, OK if res.get("status") == "merged" else BAD))
    con.print(Panel(g, title="result", border_style="green" if res.get("status") == "merged" else "red"))
    if res.get("status") == "merged":
        ag = C.agents()
        pers = ", ".join(f"{k}→{v['checkpoint']}" if not v["checkpoint"].startswith(k) else v["checkpoint"]
                         for k, v in ag.items())
        con.print(Text.assemble((f"company-{res.get('version')} published", OK),
                                (f" · personal checkpoints unchanged ({pers})", "white")))
        con.print(Text("existing agents keep their checkpoint until an explicit rebase: 49th inherit --agent NAME "
                       f"--from company-{res.get('version')}", DIM))
    return 0


# ---------------------------------------------------------------------------------------------- inherit
def ask(agent_ckpt):
    from rev import kernel, prompts
    from rev import river_util as ru
    task = json.loads((DATA / "demo_rev_c.json").read_text())
    ck = C.resolve(agent_ckpt)
    pt = ru.render(prompts.messages(task))
    t0 = time.time()
    kw = {"max_tokens": 768, "temperature": 0.0}
    text = ru.sample_base(pt, **kw) if ck is None else ru.sample_tuned(pt, checkpoint=ck, **kw)
    lat = round(time.time() - t0, 1)
    calls = prompts.parse_calls(text)
    calls = calls if isinstance(calls, list) else []
    enc, errs = kernel.apply_calls(task["board_b"], task["enclosure_a"], calls)
    checks = kernel.check(task["board_b"], enc)
    return task, text, calls, errs, checks, lat


def cmd_inherit(a):
    _head("inherit", f"explicit rebase of {a.agent} onto {a.from_}")
    if C.get(a.from_) is None:
        con.print(f"[red]unknown checkpoint {a.from_}[/] -- run the skill PR first (49th pr --from senior-me --approve)")
        return 2
    exists = a.agent in C.agents()
    r = C.set_agent_checkpoint(a.agent, a.from_) if exists else C.new_agent(a.agent, a.from_)
    con.print(Text.assemble((f"{a.agent}: ", "bold"), (str(r["before"] or "(new agent)"), BAD), ("  →  ", DIM),
                            (r["after"], OK), (f"   {C.short_ref(C.get(r['after'])['river_ref'])}", DIM)))
    con.print(Text("other agents untouched: " + ", ".join(f"{k}→{v['checkpoint']}" for k, v in C.agents().items()
                                                        if k != a.agent), DIM))
    if a.no_ask:
        return 0
    con.print()
    con.print(Text(f"ask {a.agent}: refit the enclosure for Rev C (unseen task with a USB-A port) · no retrieval, "
                   f"no lesson text in the prompt · LIVE", ACC))
    task, text, calls, errs, checks, lat = ask(r["after"])
    types = {c["id"]: c["type"] for c in task["board_b"]["connectors"]}
    t = Table(box=box.SIMPLE, header_style=ACC, title=f"tool calls ({lat}s)")
    t.add_column("#", style=DIM)
    t.add_column("tool")
    t.add_column("args")
    for i, c in enumerate(calls, 1):
        args = dict(c.get("args") or {})
        s = json.dumps(args)
        if c.get("tool") == "place_opening":
            ty = types.get(args.get("connector"), "?")
            s = Text.assemble((f"{args.get('connector')} ({ty}) ", ""),
                              (f"{args.get('tolerance')} mm/side", OK if ty == "usb_a" else ""),
                              (("  " + json.dumps(rest)) if (rest := {k: v for k, v in args.items() if k not in ('connector', 'tolerance')}) else "", DIM))
        t.add_row(str(i), c.get("tool", "?"), s)
    con.print(t)
    if errs:
        con.print(Text("tool errors: " + "; ".join(map(str, errs))[:200], BAD))
    ct = Table(box=box.SIMPLE, header_style=ACC, title="enclosure checker (5 checks)")
    ct.add_column("check")
    ct.add_column("result")
    ct.add_column("detail", max_width=70)
    for c in checks:
        ct.add_row(c["id"], Text("PASS", OK) if c["pass"] else Text("FAIL", BAD), str(c.get("detail") or ""))
    con.print(ct)
    ok = all(c["pass"] for c in checks)
    con.print(Text.assemble((f"{a.agent} on {r['after']}: ", "bold"),
                            (f"{sum(c['pass'] for c in checks)}/5 checks · {'PASS' if ok else 'FAIL'}", OK if ok else BAD)))
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    p = EVAL_DIR / f"{r['after']}-ask-rev_c-{time.strftime('%Y%m%d-%H%M%S')}.json"
    p.write_text(json.dumps({"agent": a.agent, "checkpoint": r["after"], "task": "rev/data/demo_rev_c.json",
                             "raw": text, "calls": calls, "tool_errors": errs, "checks": checks}, indent=1, default=str))
    con.print(Text(f"saved: {p.relative_to(C.DATA_DIR.parent.parent)}", DIM))
    return 0


def cmd_reset(_a):
    C.reset_agents()
    con.print("agents reset:", json.dumps({k: v["checkpoint"] for k, v in C.agents().items()}))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="49th")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("checkpoints")
    sp.add_parser("agents")
    sp.add_parser("reset-agents")
    t = sp.add_parser("test")
    t.add_argument("--checkpoint", required=True)
    t.add_argument("--suite", choices=list(SUITES), default="heldout")
    t.add_argument("--limit", type=int)
    t.add_argument("--cases", action="store_true")
    p = sp.add_parser("pr")
    p.add_argument("--from", dest="from_", required=True)
    p.add_argument("--approve", action="store_true")
    p.add_argument("--replay", action="store_true")
    i = sp.add_parser("inherit")
    i.add_argument("--agent", required=True)
    i.add_argument("--from", dest="from_", default="company-v2")
    i.add_argument("--no-ask", action="store_true")
    a = ap.parse_args(argv)
    fn = {"checkpoints": cmd_checkpoints, "agents": cmd_agents, "test": cmd_test, "pr": cmd_pr,
          "inherit": cmd_inherit, "reset-agents": cmd_reset}[a.cmd]
    return fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
