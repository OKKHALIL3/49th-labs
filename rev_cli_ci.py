"""CI for an engineer's weights: git-style views of The 49th Engineer's weight ledger.

Every piece of senior-engineer work becomes a WEIGHT PULL REQUEST (GBrain record -> conflict check
against verified history -> candidate trained on River -> lesson + regression tests -> MERGED or
BLOCKED). Every merged PR is a version in a ledger, so a neural network's knowledge gets
log / blame / revert.

Functions (the CLI owner wires these in as `49th log`, `49th blame`, `49th prs`, `49th pr`):
    log()                       ledger, newest first (git log)
    blame(ctype)                which version / PR taught production about a connector type
    prs()                       all weight PRs
    pr(text, author, replay)    open a weight PR and watch it live (SSE), then print the verdict
    revert(version)             new production version that points at an older one

Run directly:
    python3 -m rev_cli_ci log | prs | blame usb_a | revert v1
    python3 -m rev_cli_ci pr "USB-A cutouts get 0.5 mm per side" --author "Senior mechanical engineer" [--replay]
Server: $FORTYNINE_SERVER (default http://localhost:8000).
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time

import httpx
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

SERVER = os.environ.get("FORTYNINE_SERVER", "http://localhost:8000").rstrip("/")
AGENT = "The 49th Engineer"
W = 84
console = Console(highlight=False)

PRETTY = {"usb_a": "USB-A", "usb_c": "USB-C", "hdmi": "HDMI", "rj45": "RJ45", "dc_jack": "DC jack",
          "barrel": "Barrel jack", "sd": "SD card"}
STAGES = [("parsed", "Rule parsed"), ("gbrain", "GBrain record"), ("conflict_check", "Conflict check"),
          ("training", "Train on River"), ("testing", "Tests"), ("merge", "Merge")]
FINAL = {"merged", "blocked", "closed"}


# ----------------------------------------------------------------------------- helpers
def _print(*r):
    for x in r:
        console.print(x, width=min(W, console.width))


def _get(path, timeout=5.0):
    r = httpx.get(SERVER + path, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _down(e=None):
    _print(Text(f"✗  server not reachable at {SERVER}" + (f" ({e})" if e else ""), style="bold red"))
    sys.exit(2)


def _header(title, sub=None):
    t = Text()
    t.append(" 49th ", style="bold black on cyan")
    t.append(f"  {title}", style="bold")
    if sub:
        t.append(f"   {sub}", style="dim")
    return t


def _pretty(ctype):
    return PRETTY.get(ctype or "", ctype or "?")


def _ts(s):
    return (s or "").replace("T", " ")[:16]


def _tests_str(t):
    t = t or {}
    parts = []
    les = t.get("lesson") or {}
    if les.get("before") and les.get("after"):
        b, a = les["before"], les["after"]
        parts.append(f"lesson {b['passed']}/{b['total']} → {a['passed']}/{a['total']}")
    reg = t.get("regression") or {}
    if reg.get("total"):
        parts.append(f"regression {reg['passed']}/{reg['total']}")
    return " · ".join(parts)


def summary_line(p: dict) -> str:
    """One PR-style line, e.g. for Slack/QM:
    Weight PR #2 — USB-A cutout tolerance 0.5 mm · GBrain ✓ · conflict check ✓ · trained on River (8 steps) ·
    tests: lesson 0/12→12/12, regression 30/30 · MERGED → v2"""
    n, title, st = p.get("pr"), p.get("title") or p.get("text", ""), p.get("status")
    bits = [f"Weight PR #{n} — {title}"]
    if p.get("gbrain_slug"):
        bits.append("GBrain ✓")
    cc = p.get("conflict") or {}
    if cc:
        bits.append("conflict check ✗" if cc.get("verdict") == "conflict" else "conflict check ✓")
    cand = p.get("candidate") or {}
    if cand.get("steps"):
        bits.append(f"trained on River ({cand['steps']} steps)")
    t = p.get("tests") or {}
    les, reg = t.get("lesson") or {}, t.get("regression") or {}
    if les.get("before") and reg.get("total"):
        b, a = les["before"], les["after"]
        bits.append(f"tests: lesson {b['passed']}/{b['total']}→{a['passed']}/{a['total']}, "
                    f"regression {reg['passed']}/{reg['total']}")
    if st == "merged":
        bits.append(f"MERGED → {p.get('version')}")
    elif st == "blocked":
        if cc.get("verdict") == "conflict":
            bits.append(f"BLOCKED: contradicts {cc.get('differ')} verified changes "
                        f"(they used {cc.get('their_tolerance')} mm) — needs senior sign-off")
        else:
            bits.append(f"BLOCKED: {p.get('reason')}")
    elif st == "closed":
        bits.append(f"CLOSED: {p.get('reason')}")
    else:
        bits.append((st or "open").upper())
    return " · ".join(bits)


# ----------------------------------------------------------------------------- log
def log():
    """git log for the weights: every production version, newest first."""
    try:
        d = _get("/api/ledger")
    except Exception as e:  # noqa: BLE001
        _down(e)
    vs = d.get("versions") or []
    body = [_header("log", f"{len(vs)} version(s) · production {d.get('production')}"), Text()]
    for i, v in enumerate(reversed(vs)):
        prod = v.get("status") == "production"
        head = Text()
        head.append("● " if prod else "○ ", style="bold green" if prod else "grey50")
        head.append(f"{v['version']}", style="bold yellow")
        head.append(f"  ({v.get('status')})", style="bold green" if prod else "dim")
        if v.get("pr"):
            head.append(f"  PR #{v['pr']}", style="cyan")
        if v.get("reverts"):
            head.append(f"  reverts to {v['reverts']}", style="magenta")
        head.append(f"  {v.get('title') or ''}", style="bold" if prod else "")
        body.append(head)
        pipe = "│ " if i < len(vs) - 1 else "  "
        meta = f"by {v.get('author') or '-'} via {v.get('source') or '-'} · {_ts(v.get('ts'))}"
        if v.get("parent"):
            meta += f" · parent {v['parent']}"
        body.append(Text(pipe, style="grey50") + Text(meta, style="dim"))
        ts = _tests_str(v.get("tests"))
        if ts:
            body.append(Text(pipe, style="grey50") + Text("tests: ", style="dim") + Text(ts, style="green"))
        if v.get("gbrain_slug"):
            body.append(Text(pipe, style="grey50") + Text("gbrain: ", style="dim") + Text(v["gbrain_slug"]))
        ck = v.get("river_checkpoint")
        if ck:
            short = ck if len(ck) < 70 else ck[:22] + "…" + ck[-40:]
            body.append(Text(pipe, style="grey50") + Text("weights: ", style="dim") + Text(short, style="grey70"))
        if i < len(vs) - 1:
            body.append(Text("│", style="grey50"))
    _print(*body)
    return d


# ----------------------------------------------------------------------------- blame
def blame(ctype="usb_a"):
    """Which version / PR taught the production weights what they know about `ctype` openings."""
    try:
        b = _get(f"/api/blame?type={ctype}")
    except Exception as e:  # noqa: BLE001
        _down(e)
    g = Table.grid(padding=(0, 2))
    g.add_column(style="dim", justify="right")
    g.add_column()
    if not b.get("version"):
        g.add_row("knowledge", Text("never seen: no verified change and no merged PR mentions it", style="yellow"))
    else:
        g.add_row("version", Text(b["version"], style="bold yellow"))
        g.add_row("rule", Text(f"{_pretty(ctype)} opening = {b.get('tolerance')} mm per side", style="bold"))
        if b.get("pr"):
            g.add_row("weight PR", Text(f"#{b['pr']}  {b.get('title') or ''}", style="cyan"))
        g.add_row("taught by", f"{b.get('author') or '-'} via {b.get('source') or '-'}")
        if b.get("records"):
            g.add_row("evidence", f"{b['records']} verified engineering changes in GBrain (history/eco-*)")
        if b.get("gbrain_slug"):
            g.add_row("gbrain", b["gbrain_slug"])
        if b.get("ts"):
            g.add_row("when", _ts(b["ts"]))
    _print(_header(f"blame {ctype}", _pretty(ctype)), Text(),
           Panel(g, box=box.ROUNDED, border_style="cyan", padding=(1, 2)),
           Text(b.get("summary") or "", style="dim"))
    return b


# ----------------------------------------------------------------------------- prs
def prs():
    """All weight PRs, newest first."""
    try:
        d = _get("/api/prs")
    except Exception as e:  # noqa: BLE001
        _down(e)
    items = [p for p in (d.get("prs") or []) if p.get("pr")]
    t = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
    t.add_column("#", style="cyan", width=3)
    t.add_column("status", width=8)
    t.add_column("rule", ratio=3)
    t.add_column("author", style="dim", ratio=3)
    t.add_column("result", ratio=4)
    style = {"merged": "bold green", "blocked": "bold red", "closed": "yellow", "open": "bold cyan"}
    for p in reversed(items):
        st = p.get("status") or "open"
        res = p.get("reason") or ("running…" if st == "open" else "")
        if st == "merged":
            res = f"→ {p.get('version')} · {_tests_str(p.get('tests'))}"
        else:
            res = res.split(" -- ")[0]
        t.add_row(str(p["pr"]), Text(st.upper(), style=style.get(st, "")), p.get("title") or p.get("text") or "",
                  f"{p.get('author') or '-'} ({p.get('source') or '-'})", res)
    sub = f"{len(items)} PR(s)" + (" · one running" if d.get("running") else "")
    _print(_header("prs", sub), t if items else Text("  no weight PRs yet", style="dim"))
    return items


# ----------------------------------------------------------------------------- pr (live)
class _PRView:
    def __init__(self, text, author):
        self.text, self.author = text, author
        self.n = None
        self.state = {k: ("pending", "") for k, _ in STAGES}
        self.final = None
        self.t = 0.0

    def feed(self, e):
        st, msg, data = e.get("stage"), e.get("msg") or "", e.get("data") or {}
        self.t = e.get("t") or self.t
        order = [k for k, _ in STAGES]

        def done_before(k):
            for x in order[: order.index(k)]:
                if self.state[x][0] in ("pending", "running"):
                    self.state[x] = ("done", self.state[x][1])

        if st == "parsed":
            self.state["parsed"] = ("done", msg.replace("Rule: ", ""))
        elif st == "gbrain":
            done_before("gbrain")
            self.state["gbrain"] = ("done" if data.get("saved", True) else "warn", data.get("slug") or msg)
        elif st == "conflict_check":
            done_before("conflict_check")
            bad = data.get("verdict") == "conflict"
            self.state["conflict_check"] = ("fail" if bad else "done", msg.replace("Checked ", "", 1))
        elif st == "training":
            done_before("training")
            step, tot, loss = data.get("step"), data.get("total_steps"), data.get("loss")
            det = msg
            if step and tot:
                det = f"step {step}/{tot}" + (f" · loss {loss:.4f}" if isinstance(loss, (int, float)) else "")
            self.state["training"] = ("running", det)
        elif st == "testing":
            done_before("testing")
            self.state["testing"] = ("running", msg)
        elif st == "not_a_rule":
            self.state["parsed"] = ("fail", msg)
        elif st in FINAL:
            rec = data.get("pr_record") or {}
            self.final = rec or {"pr": self.n, "status": data.get("status") or st, "reason": msg}
            status = self.final.get("status")
            for k in order[:-1]:
                if self.state[k][0] == "running":
                    self.state[k] = ("done", self.state[k][1])
                elif self.state[k][0] == "pending":
                    self.state[k] = ("skip", "not run")
            if rec.get("candidate", {}).get("steps"):
                c = rec["candidate"]
                ls = c.get("losses") or []
                self.state["training"] = ("done", f"{c['steps']} steps from production · {c.get('verified')} verified "
                                                  f"+ {c.get('replay')} replay"
                                          + (f" · loss {ls[0]:.3f} → {ls[-1]:.4f}" if ls else ""))
            if rec.get("tests"):
                self.state["testing"] = ("done", _tests_str(rec["tests"]))
            if status == "merged":
                self.state["merge"] = ("done", f"MERGED → {rec.get('version')} (new production weights)")
            elif status == "blocked":
                self.state["merge"] = ("fail", f"BLOCKED: {self.final.get('reason')}")
            else:
                self.state["merge"] = ("skip", f"closed: {self.final.get('reason')}")

    def __rich__(self):
        g = Table.grid(padding=(0, 1))
        g.add_column(width=2)
        g.add_column(width=15, style="bold")
        g.add_column()
        icon = {"done": Text("✓", style="bold green"), "fail": Text("✗", style="bold red"),
                "warn": Text("!", style="bold yellow"), "skip": Text("–", style="dim"),
                "pending": Text("·", style="grey50")}
        running_seen = False
        for k, label in STAGES:
            s, det = self.state[k]
            if s == "running" or (s == "pending" and not running_seen and not self.final and self._next() == k):
                ic = Spinner("dots", style="cyan")
                running_seen = True
            else:
                ic = icon.get(s, icon["pending"])
            g.add_row(ic, label, Text(det, style="red" if s == "fail" else ("dim" if s == "pending" else "")))
        title = f"Weight PR #{self.n}" if self.n else "Weight PR"
        return Panel(Group(Text(f"“{self.text}”", style="italic"), Text(f"— {self.author}", style="dim"), Text(), g),
                     title=f"[bold]{title}[/]", subtitle=f"[dim]{self.t:.1f}s[/]", box=box.ROUNDED,
                     border_style="cyan", padding=(1, 2), width=min(W, console.width))

    def _next(self):
        for k, _ in STAGES:
            if self.state[k][0] == "pending":
                return k
        return None


def _sse(q, stop, ready):
    try:
        with httpx.stream("GET", SERVER + "/api/events", timeout=httpx.Timeout(5.0, read=None)) as r:
            buf = ""
            for chunk in r.iter_text():
                if stop.is_set():
                    return
                ready.set()
                buf += chunk
                while "\n\n" in buf:
                    block, buf = buf.split("\n\n", 1)
                    data = "".join(l[5:].strip() for l in block.splitlines() if l.startswith("data:"))
                    if not data:
                        continue
                    try:
                        ev = json.loads(data)
                    except ValueError:
                        continue
                    if ev.get("type") == "pr":
                        q.put(ev)
    except Exception as ex:  # noqa: BLE001
        q.put({"type": "_err", "error": str(ex)})
    finally:
        ready.set()


def pr(text, author="Senior engineer", replay=False, source="cli", speed=None, timeout=420):
    """Open a weight PR and watch it go through GBrain -> conflict check -> River -> tests -> merge."""
    q, stop, ready = queue.Queue(), threading.Event(), threading.Event()
    threading.Thread(target=_sse, args=(q, stop, ready), daemon=True).start()
    ready.wait(3)
    body = {"text": text, "author": author, "source": source, "replay": bool(replay)}
    if speed:
        body["speed"] = speed
    try:
        r = httpx.post(SERVER + "/api/pr", json=body, timeout=15)
    except Exception as e:  # noqa: BLE001
        stop.set()
        _down(e)
    if r.status_code == 404 and replay:
        _print(Text("no recorded run matches; running it live", style="yellow"))
        body["replay"] = False
        r = httpx.post(SERVER + "/api/pr", json=body, timeout=15)
    if r.status_code not in (200, 202):
        stop.set()
        _print(Text(f"✗  POST /api/pr → HTTP {r.status_code}: {r.text[:300]}", style="bold red"))
        sys.exit(1)
    n = r.json().get("pr")
    view = _PRView(text, author)
    view.n = n
    _print(_header(f"pr #{n}", ("replay of a recorded real run" if body["replay"] else "live on River")
                   + f" · {AGENT}"), Text())
    deadline = time.time() + timeout
    last_poll = 0.0
    with Live(view, console=console, refresh_per_second=12, transient=False):
        while not view.final and time.time() < deadline:
            try:
                e = q.get(timeout=0.5)
                if e.get("type") == "pr" and e.get("pr") == n:
                    view.feed(e)
            except queue.Empty:
                pass
            if time.time() - last_poll > 5:  # safety net if SSE drops
                last_poll = time.time()
                try:
                    rec = next((p for p in _get("/api/prs").get("prs", []) if p.get("pr") == n), None)
                    if rec and rec.get("status") in FINAL:
                        view.feed({"stage": rec["status"], "msg": rec.get("reason"), "t": rec.get("seconds") or view.t,
                                   "data": {"status": rec["status"], "pr_record": rec}})
                except Exception:  # noqa: BLE001
                    pass
    stop.set()
    if not view.final:
        _print(Text(f"still running after {timeout}s; check `python3 -m rev_cli_ci prs`", style="yellow"))
        return None
    st = view.final.get("status")
    style = {"merged": "bold green", "blocked": "bold red"}.get(st, "yellow")
    _print(Text(), Text(summary_line(view.final), style=style))
    cc = view.final.get("conflict") or {}
    if st == "blocked" and cc.get("evidence"):
        _print(Text("evidence: " + ", ".join(ev["slug"] for ev in cc["evidence"]), style="dim"))
    return view.final


def revert(version="v1"):
    try:
        r = httpx.post(SERVER + "/api/ledger/revert", json={"version": version}, timeout=15)
    except Exception as e:  # noqa: BLE001
        _down(e)
    if r.status_code != 200:
        _print(Text(f"✗  revert → HTTP {r.status_code}: {r.text[:200]}", style="bold red"))
        sys.exit(1)
    d = r.json()
    _print(_header("revert", f"production is now {d.get('production')} (reverts to {version})"))
    return log()


# ----------------------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(prog="rev_cli_ci", description="CI for an engineer's weights")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("log")
    sub.add_parser("prs")
    b = sub.add_parser("blame")
    b.add_argument("type", nargs="?", default="usb_a")
    p = sub.add_parser("pr")
    p.add_argument("text")
    p.add_argument("--author", default="Senior engineer")
    p.add_argument("--source", default="cli")
    p.add_argument("--replay", action="store_true", help="re-emit the recorded real run (video re-takes)")
    p.add_argument("--speed", type=float, default=None)
    rv = sub.add_parser("revert")
    rv.add_argument("version")
    a = ap.parse_args(argv)
    if a.cmd == "log":
        log()
    elif a.cmd == "prs":
        prs()
    elif a.cmd == "blame":
        blame(a.type)
    elif a.cmd == "pr":
        res = pr(a.text, author=a.author, replay=a.replay, source=a.source, speed=a.speed)
        sys.exit(0 if res else 1)
    elif a.cmd == "revert":
        revert(a.version)


if __name__ == "__main__":
    main()
