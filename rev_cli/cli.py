"""49th -- terminal front-end for The 49th Engineer (49th Labs).

    49th status                 one-panel health check
    49th eval [--wall]          headline held-out table (real result files only)
    49th teach "<sentence>" [--replay]   teach one rule live, stream the run
    49th refit <B|C>            land a board revision and refit the enclosure
    49th reset                  un-promote learned weights + reset the board to Rev A

Reads only real files / live endpoints; prints only numbers that exist.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path

import httpx
from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "rev" / "data"
LARGE = ROOT / "rev" / "large"
SERVER = os.environ.get("FORTYNINE_SERVER", "http://localhost:8000").rstrip("/")
QM_URL = os.environ.get("FORTYNINE_QM", "http://localhost:9081")
EXPORT = os.environ.get("FORTYNINE_EXPORT")  # append plain-text output here (for CLI_SAMPLES.txt)

COMPANY, AGENT, CUSTOMER = "49th Labs", "The 49th Engineer", "Acme Devices"
ACCENT = "bold cyan"
W = 84  # every command renders at this width (legible in a 1080p recording)

console = Console(highlight=False)


# ----------------------------------------------------------------------------- helpers
def out(*renderables):
    """Print and (optionally) record the plain-text version for the samples file."""
    for r in renderables:
        console.print(r, width=min(W, console.width))
    if EXPORT:
        rec = Console(record=True, file=io.StringIO(), width=W, highlight=False)
        for r in renderables:
            rec.print(r)
        with open(EXPORT, "a") as f:
            f.write(rec.export_text())


def export_header(cmd):
    if EXPORT:
        with open(EXPORT, "a") as f:
            f.write(f"\n$ {cmd}\n")


def load_json(p):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return None


def get(path, timeout=3.0):
    r = httpx.get(SERVER + path, timeout=timeout)
    r.raise_for_status()
    return r.json()


def post(path, body=None, timeout=10.0):
    return httpx.post(SERVER + path, json=body or {}, timeout=timeout)


def ok(s="✓"):
    return Text(s, style="bold green")


def bad(s="✗"):
    return Text(s, style="bold red")


def dim(s):
    return Text(str(s), style="dim")


def header(title, sub=None):
    t = Text()
    t.append(" 49th ", style="bold black on cyan")
    t.append(f"  {title}", style="bold")
    if sub:
        t.append(f"   {sub}", style="dim")
    return t


def server_down():
    out(Text.assemble(bad(), Text(f"  server not reachable at {SERVER}", style="red")))
    sys.exit(2)


def model_name(full):
    s = (full or "").split("/")[-1]
    s = re.sub(r"-FP8$", "", s)
    s = re.sub(r"-A\d+B$", "", s)
    return s


def params_of(full):
    m = re.search(r"-(\d+)B", full or "")
    return int(m.group(1)) if m else 0


def bar(passed, total, width=20, style="green"):
    n = round(width * passed / total) if total else 0
    t = Text("█" * n, style=style)
    t.append("░" * (width - n), style="grey50")
    return t


SPARK = "▁▂▃▄▅▆▇█"


def pretty(ctype):
    return {"usb_a": "USB-A", "usb_c": "USB-C", "hdmi": "HDMI", "rj45": "RJ45", "dc_jack": "DC jack"}.get(ctype, ctype)


def sparkline(vals):
    vals = [v for v in vals if isinstance(v, (int, float))]
    if not vals:
        return ""
    import math
    lv = [math.log10(max(v, 1e-9)) for v in vals]  # loss spans orders of magnitude
    lo, hi = min(lv), max(lv)
    span = (hi - lo) or 1.0
    return "".join(SPARK[int(round((x - lo) / span * (len(SPARK) - 1)))] for x in lv)


# ----------------------------------------------------------------------------- status
def cmd_status(a):
    export_header("49th status")
    try:
        health = get("/api/health", 2)
    except Exception:  # noqa: BLE001
        health = None
    ls = None
    if health:
        try:
            ls = get("/api/learn/status", 2)
        except Exception:  # noqa: BLE001
            pass

    ck = load_json(DATA / "checkpoint.json") or {}
    cur_file = (ls or {}).get("active_checkpoint")
    if cur_file is None and (DATA / "checkpoint_current.json").exists():
        cur_file = "checkpoint_current.json"
    promoted = cur_file not in (None, "checkpoint.json")
    cur = load_json(DATA / "checkpoint_current.json") if promoted else None

    g = Table.grid(padding=(0, 2))
    g.add_column(style="dim", justify="right", no_wrap=True)
    g.add_column()

    g.add_row("customer", Text(CUSTOMER, style="bold"))
    base = ck.get("base_model", "Qwen/Qwen3.5-9B").split("/")[-1]
    m = Text()
    m.append("acme-9b", style=ACCENT)
    m.append(f"  =  {base} + LoRA (rank {ck.get('rank', '?')}) on River", style="")
    g.add_row("model", m)
    mins = ck.get("train_seconds")
    v1 = Text(f"v1  {ck.get('name', '?')}", style="dim" if promoted else "bold")
    v1.append(f"  ·  {ck.get('train_steps', ck.get('step', '?'))} steps"
              + (f"  ·  {mins / 60:.0f} min" if mins else "")
              + (f"  ·  {ck.get('n_train')} company changes" if ck.get("n_train") else ""), style="dim")
    g.add_row("checkpoint", v1)
    if promoted:
        les = (cur or {}).get("lesson") or {}
        v2 = Text("v2  ", style="bold green")
        v2.append((cur or {}).get("path", "?").split("/")[-1], style="bold")
        extra = []
        if cur and cur.get("steps"):
            extra.append(f"+{cur['steps']} steps")
        if les:
            extra.append(f"{pretty(les.get('type'))} {les.get('tolerance')} mm")
        if extra:
            v2.append("  ·  " + "  ·  ".join(extra), style="dim")
        v2.append("  ●", style="green")
        g.add_row("", v2)

    gb, src = None, "server"
    if health:
        try:
            gb = get("/api/gbrain/stats", 8)
        except Exception:  # noqa: BLE001
            gb = None
    if gb is None:
        try:
            sys.path.insert(0, str(ROOT))
            from rev import gbrain_history  # noqa: E402
            gb, src = gbrain_history.stats(), "local"
        except Exception:  # noqa: BLE001
            gb = None
    if gb:
        t = Text()
        t.append(str(gb.get("history_records", 0)), style="bold")
        t.append(" engineering records  +  ")
        t.append(str(gb.get("capture_records", 0)), style="bold")
        t.append(" captures")
        if src == "local":
            t.append("  (read locally)", style="dim")
        g.add_row("GBrain", t)
    else:
        g.add_row("GBrain", bad("unavailable"))

    try:
        httpx.get(QM_URL, timeout=1.0)
        g.add_row("QM agent", Text.assemble(ok("●"), " running (local)"))
    except Exception:  # noqa: BLE001
        g.add_row("QM agent", Text.assemble(dim("○"), dim(" not running")))

    if health:
        st = Text.assemble(ok("●"), f" ok  ", dim(SERVER.replace("http://", "")))
        if health.get("running") or (ls or {}).get("running"):
            st.append("   busy", style="yellow")
        g.add_row("server", st)
    else:
        g.add_row("server", Text.assemble(bad("●"), Text(f" down  {SERVER}", style="red")))

    out(Panel(g, title=Text(f" {AGENT} ", style="bold"), title_align="left",
              subtitle=Text(f" {COMPANY} ", style="dim"), subtitle_align="right",
              border_style="cyan", box=box.ROUNDED, padding=(1, 2), expand=True))


# ----------------------------------------------------------------------------- eval
def _train_note(tr):
    """'LoRA · 4 epochs · 52 steps' from a real training record (None if it has no step count)."""
    steps = tr.get("steps") or tr.get("train_steps")
    if not steps:
        return None
    ep, planned = tr.get("epochs"), tr.get("epochs_planned")
    if isinstance(ep, float) and ep.is_integer():
        ep = int(ep)
    if isinstance(ep, float):
        ep = f"{ep:.1f}"
    if ep and planned and float(ep) < planned:
        return f"LoRA · {ep}/{planned} epochs · {steps} steps"
    if ep:
        return f"LoRA · {ep} epoch{'' if str(ep) == '1' else 's'} · {steps} steps"
    return f"LoRA · {steps} steps"


def _eval_rows():
    """Every row comes from a real results file. Returns list of dicts."""
    rows = []
    ev = load_json(DATA / "eval_results.json") or {}
    b9 = ev.get("base_model", "Qwen/Qwen3.5-9B")
    for r in ev.get("runs", []):
        if r.get("name") == "base":
            rows.append(dict(model=model_name(b9), params=params_of(b9), kind="generic",
                             passed=r["passed"], total=r["total"], partial=False, ours=False))
        elif r.get("name") == "tuned":
            rows.append(dict(model="acme-9b", note=_train_note(load_json(DATA / "checkpoint.json") or {}) or "LoRA",
                             params=params_of(b9),
                             kind="tuned", passed=r["passed"], total=r["total"], partial=False, ours=True))
    for d in sorted(LARGE.glob("*/results.json")):
        res = load_json(d) or {}
        bm = res.get("base_model", "")
        for key, kind in (("base", "generic"), ("tuned", "tuned")):
            r = res.get(key)
            if not isinstance(r, dict) or r.get("passed") is None or not r.get("total"):
                continue
            partial = bool(r.get("partial")) or (kind == "tuned" and bool(res.get("partial")))
            done = r.get("done") or r.get("evaluated")
            if isinstance(done, int) and done < r["total"]:
                partial = True
            name = model_name(bm)
            rows.append(dict(model=name if kind == "generic" else f"acme-{params_of(bm)}b",
                             note=None if kind == "generic" else (_train_note(res.get("train") or {}) or "LoRA"),
                             params=params_of(bm), kind=kind, passed=r["passed"], total=r["total"],
                             partial=partial, ours=False))
    return rows


def cmd_eval(a):
    export_header("49th eval" + (" --wall" if a.wall else ""))
    if a.wall:
        return _eval_wall()
    rows = _eval_rows()
    gen = sorted([r for r in rows if r["kind"] == "generic"], key=lambda r: -r["params"])
    tun = sorted([r for r in rows if r["kind"] == "tuned"], key=lambda r: (r["ours"], r["passed"] / r["total"]))

    t = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, padding=(0, 0, 0, 1),
              header_style="dim", expand=False, min_width=W)
    t.add_column("model", no_wrap=True)
    t.add_column("", no_wrap=True, style="dim")
    t.add_column("passed", justify="right", no_wrap=True)
    t.add_column("", no_wrap=True)

    for r in gen:
        t.add_row(Text(r["model"]), "generic",
                  Text(f"{r['passed']:>2}/{r['total']}", style="red"),
                  bar(r["passed"], r["total"], width=14, style="red"))
    if gen and tun:
        t.add_section()
    for r in tun:
        name = Text(r["model"], style="bold cyan" if r["ours"] else "cyan")
        note = r.get("note") or ""
        score = Text(f"{r['passed']:>2}/{r['total']}", style="bold green" if r["ours"] else "green")
        tail = bar(r["passed"], r["total"], width=14)
        if r["ours"]:
            tail.append("  ◀ live agent", style="bold cyan")
        elif r["partial"]:
            tail.append("  partial", style="yellow")
        t.add_row(name, note, score, tail)

    out(Group(Text(), header("eval", f"{CUSTOMER} · engineering-change refits"), Text(), t,
              Text("30 held-out engineering changes · graded by a deterministic checker", style="dim"),
              Text("same prompt & tools for every model", style="dim")))


def _eval_wall():
    f = load_json(DATA / "fleet_results_real.json") or {}
    w = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, padding=(0, 1), header_style="dim",
              expand=True)
    w.add_column("the wall", no_wrap=True)
    w.add_column("passed", justify="right", no_wrap=True)
    w.add_column("", no_wrap=True, ratio=1)
    w.add_column("time", justify="right", no_wrap=True, style="dim")
    tot = workers = None
    for k in ("base", "tuned", "tuned_v2"):
        r = f.get(k)
        if not r:
            continue
        tot, workers = r.get("total"), r.get("workers") or workers
        full = r["passed"] == r["total"]
        style = "red" if k == "base" else ("bold green" if full else "green")
        secs = r.get("seconds")
        w.add_row(Text(r.get("label") or k, style="bold" if k == "tuned_v2" else ""),
                  Text(f"{r['passed']}/{r['total']}", style=style),
                  bar(r["passed"], r["total"], width=24, style="red" if k == "base" else "green"),
                  f"{secs:.0f} s" if isinstance(secs, (int, float)) else "")
    foot = " · ".join(x for x in (f"{tot} boards" if tot else "", f"{workers} in parallel" if workers else "",
                                   "same checker") if x)
    out(Group(Text(), header("the wall", f"{CUSTOMER} · one refit per board"), Text(), w, Text(foot, style="dim")))


# ----------------------------------------------------------------------------- teach
STEPS = [("rule", "Rule understood"), ("gbrain", "Saved to GBrain"), ("curriculum", "Curriculum"),
         ("replay", "Replay"), ("train", "River training"), ("eval", "Eval"), ("deploy", "Deployed")]


class TeachView:
    def __init__(self, sentence, cached):
        self.sentence, self.cached = sentence, cached
        self.t0 = time.time()
        self.state = {k: None for k, _ in STEPS}  # None pending | "run" | "ok" | "fail"
        self.detail = {k: Text() for k, _ in STEPS}
        self.losses, self.step, self.total = [], 0, None
        self.final, self.error, self.seen_gbrain = None, None, False
        self.spinner = Spinner("dots", style="cyan")
        self.ev_t, self.rec_total = None, None
        if cached:  # the server replays rev/data/learn_last.json; its real duration is in the recorded result
            rec = load_json(DATA / "learn_last.json") or {}
            fin = [e for e in rec.get("events", []) if e.get("type") == "learned"]
            self.rec_total = fin[-1].get("seconds") if fin else None

    # ---- event ingestion (numbers only from events)
    def feed(self, e):
        typ, stage, msg = e.get("type"), e.get("stage"), e.get("msg") or ""
        if isinstance(e.get("t"), (int, float)):
            self.ev_t = e["t"]
        if typ == "learned":
            self.final = e
            self._finish(e)
            return
        if typ != "learn":
            return
        if stage == "verify":
            if msg.startswith("Correction understood"):
                m = re.search(r":\s*(\S+) opening tolerance = ([\d.]+) mm", msg)
                self.state["rule"] = "ok"
                self.detail["rule"] = (Text.assemble(Text(pretty(m.group(1)), style="bold"),
                                                     f" openings  ·  {m.group(2)} mm per side") if m else Text(msg))
                self._start("curriculum")
            elif "verified" in msg:
                m = re.search(r"verified (\d+)/(\d+)", msg)
                v, g = (m.group(1), m.group(2)) if m else (e.get("step"), e.get("total_steps"))
                self.state["curriculum"] = "ok"
                self.detail["curriculum"] = Text.assemble(Text(str(g), style="bold"), " practice changes  ·  ",
                                                          Text(f"{v}/{g}", style="bold green"), " verified by the checker")
            elif "replay" in msg:
                m = re.search(r"(\d+) replay", msg)
                self.state["replay"] = "ok"
                self.detail["replay"] = Text.assemble(Text(m.group(1) if m else "?", style="bold"),
                                                      " past changes mixed in  ", dim("(anti-forgetting)"))
                self._start("train")
            elif msg.startswith("Checker rejected") or msg.startswith("Could not"):
                self.error = msg
            else:
                self.detail["curriculum"] = Text(msg, style="dim")
        elif stage == "gbrain":
            self.seen_gbrain = True
            slug = e.get("slug") or e.get("gbrain_slug")
            self.state["gbrain"] = "ok" if (slug or "saved" in msg.lower() or e.get("ok")) else "run"
            self.detail["gbrain"] = Text(slug, style="bold") if slug else Text(msg, style="dim")
        elif stage == "train":
            self._start("train")
            self.total = e.get("total_steps") or self.total
            if isinstance(e.get("loss"), (int, float)):
                self.losses.append(e["loss"])
                self.step = e.get("step") or self.step
            if msg.startswith("Saving"):
                self.state["train"] = "ok"
            self._train_detail(msg)
        elif stage == "eval":
            if self.state["train"] == "run":
                self.state["train"] = "ok"
                self._train_detail()
            if msg.startswith("Promoted"):
                self.state["deploy"] = "ok"
                self.detail["deploy"] = Text("promoted", style="bold green").append(f"  ·  {AGENT} now runs v2")
                return
            m = re.search(r"(\S+) held-out: before (\d+)/(\d+) -> after (\d+)/(\d+)", msg)
            if m:
                self.state["eval"] = "ok"
                self.detail["eval"] = self._eval_text(m.group(1), m.group(2), m.group(3), m.group(4), m.group(5))
                self._start("deploy")
            elif msg.startswith("regular held-out"):
                self.detail["eval"].append(f"   regular held-out {msg.split(':')[-1].strip()}", style="")
            else:
                self._start("eval")
                self.detail["eval"] = Text(msg, style="dim")

    def _eval_text(self, ctype, b, bt, a, at):
        name = pretty(ctype)
        return Text.assemble(f"{name} held-out  ", Text(f"{b}/{bt}", style="red"), "  →  ",
                             Text(f"{a}/{at}", style="bold green"))

    def _train_detail(self, msg=""):
        t = Text()
        if self.total:
            t.append(f"step {self.step}/{self.total}", style="bold")
        if self.losses:
            fmt = lambda v: f"{v:.3f}" if v >= 0.01 else f"{v:.2g}"  # noqa: E731
            t.append(f"  ·  loss {fmt(self.losses[0])}")
            if len(self.losses) > 1:
                t.append(f" → {fmt(self.losses[-1])}")
            t.append("  ")
            t.append(sparkline(self.losses), style="cyan")
            t.append(" (log)", style="dim")
        elif msg and not msg.startswith("step"):
            t.append(("  ·  " if len(t) else "") + msg, style="dim")
        self.detail["train"] = t

    def _start(self, k):
        if self.state[k] is None:
            self.state[k] = "run"

    def _finish(self, e):
        if not e.get("ok"):
            self.error = e.get("error") or self.error or "learn run failed"
            for k, _ in STEPS:
                if self.state[k] == "run":
                    self.state[k] = "fail"
            return
        les = e.get("lesson") or {}
        if self.state["rule"] != "ok" and les:
            self.state["rule"] = "ok"
            self.detail["rule"] = Text.assemble(Text(pretty(les.get("type", "")), style="bold"),
                                                f" openings  ·  {les.get('tolerance')} mm per side")
        if e.get("verified") is not None and self.state["curriculum"] != "ok":
            self.state["curriculum"] = "ok"
            self.detail["curriculum"] = Text.assemble(Text(str(e["generated"]), style="bold"),
                                                      " practice changes  ·  ",
                                                      Text(f"{e['verified']}/{e['generated']}", style="bold green"),
                                                      " verified by the checker")
        if e.get("replay") is not None and self.state["replay"] != "ok":
            self.state["replay"] = "ok"
            self.detail["replay"] = Text.assemble(Text(str(e["replay"]), style="bold"), " past changes mixed in  ",
                                                  dim("(anti-forgetting)"))
        if e.get("losses"):
            self.losses = [x for x in e["losses"] if isinstance(x, (int, float))]
            self.step = e.get("steps") or len(self.losses)
            self.total = e.get("steps") or self.total
        self.state["train"] = "ok"
        self._train_detail()
        b, af = e.get("before") or {}, e.get("after") or {}
        if b and af:
            self.state["eval"] = "ok"
            self.detail["eval"] = self._eval_text(les.get("type", ""), b.get("passed"), b.get("total"),
                                                  af.get("passed"), af.get("total"))
            h = e.get("heldout_after")
            if h:
                self.detail["eval"].append(f"   ·   regular held-out {h['passed']}/{h['total']}")
        if e.get("gbrain_slug") and not self.seen_gbrain:
            self.seen_gbrain = True
            self.state["gbrain"] = "ok"
            self.detail["gbrain"] = Text(e["gbrain_slug"], style="bold")
        if e.get("promoted"):
            self.state["deploy"] = "ok"
            self.detail["deploy"] = Text("promoted", style="bold green").append(f"  ·  {AGENT} now runs v2")
        else:
            self.state["deploy"] = None
            self.detail["deploy"] = dim("not promoted")

    # ---- render
    def __rich__(self):
        g = Table.grid(padding=(0, 1))
        g.add_column(width=2, no_wrap=True)
        g.add_column(style="bold", no_wrap=True, min_width=16)
        g.add_column()
        for k, label in STEPS:
            s = self.state[k]
            if k == "gbrain" and not self.seen_gbrain:
                continue  # only shown if the server emits it
            if k == "deploy" and s is None and self.final:
                icon = dim("–")
            elif s == "ok":
                icon = ok()
            elif s == "run":
                icon = self.spinner
            elif s == "fail":
                icon = bad()
            else:
                icon = dim("·")
            g.add_row(icon, Text(label, style="bold" if s else "dim"), self.detail[k])
        el = (self.final or {}).get("seconds") if self.final else None
        if self.cached:  # show the recorded run's own clock, not the replay's wall clock
            tot = el if el is not None else self.rec_total
            now = el if el is not None else (self.ev_t or 0.0)
        else:
            el = el if el is not None else round(time.time() - self.t0, 1)
        top = Text()
        top.append(f"“{self.sentence}”", style="italic")
        parts = [top, Text(), g]
        if self.error:
            parts += [Text(), Text(f"✗ {self.error}", style="bold red")]
        if self.cached:
            sub = f" recorded real run · {now:.0f} s" + (f" / {tot:.0f} s" if tot else "") + " · fast-forward "
        else:
            sub = f" {el:.0f} s "
        return Panel(Group(*parts), title=Text(f" {AGENT} is learning ", style="bold"), title_align="left",
                     subtitle=Text(sub, style="dim"), subtitle_align="right",
                     border_style="green" if (self.final and self.final.get("ok")) else ("red" if self.error else "cyan"),
                     box=box.ROUNDED, padding=(1, 2), expand=True, width=min(console.width, W))


def _sse_reader(q, stop, ready):
    try:
        with httpx.stream("GET", SERVER + "/api/events", timeout=httpx.Timeout(5.0, read=None)) as r:
            buf = ""
            for chunk in r.iter_text():
                if stop.is_set():
                    return
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
                    ready.set()
                    if ev.get("type") in ("learn", "learned"):
                        q.put(ev)
    except Exception as ex:  # noqa: BLE001
        q.put({"type": "_sse_error", "error": str(ex)})
    finally:
        ready.set()


def cmd_teach(a):
    sentence = " ".join(a.sentence).strip()
    export_header(f'49th teach "{sentence}"' + (" --replay" if a.replay else ""))
    try:
        st = get("/api/learn/status", 3)
    except Exception:  # noqa: BLE001
        server_down()
    if st.get("running"):
        out(Text.assemble(bad(), Text("  busy: a learn run is already in progress (409)", style="red")))
        sys.exit(1)

    q, stop, ready = queue.Queue(), threading.Event(), threading.Event()
    threading.Thread(target=_sse_reader, args=(q, stop, ready), daemon=True).start()
    ready.wait(5)

    body = {"text": sentence, "promote": True}
    if a.replay:
        body["cached"] = True
    r = post("/api/learn", body)
    if r.status_code == 409:
        out(Text.assemble(bad(), Text("  busy: a learn run is already in progress (409)", style="red")))
        sys.exit(1)
    if r.status_code >= 400:
        try:
            det = r.json().get("detail")
        except ValueError:
            det = r.text
        out(Text.assemble(bad(), Text(f"  {det}", style="red")))
        sys.exit(1)

    view = TeachView(sentence, a.replay)
    last_ev, sse_ok, last_tick = time.time(), True, 0.0
    HOLD = 0.6  # pacing only: verify/eval ticks that arrive in the same instant appear one by one
    with Live(view, console=console, refresh_per_second=12, transient=False) as live:
        while not view.final:
            try:
                ev = q.get(timeout=0.25)
            except queue.Empty:
                ev = None
            if ev is not None:
                if ev.get("type") == "_sse_error":
                    sse_ok = False
                else:
                    if ev.get("stage") in ("verify", "eval") or ev.get("type") == "learned":
                        wait = last_tick + HOLD - time.time()
                        if wait > 0:
                            time.sleep(wait)  # Live keeps animating on its own refresh thread
                        last_tick = time.time()
                    view.feed(ev)
                    last_ev = time.time()
            # safety net: SSE dropped or quiet for long -> read the run log from /api/learn/status
            if (not sse_ok or time.time() - last_ev > 20) and not view.final:
                try:
                    s = get("/api/learn/status", 3)
                    if not s.get("running") and s.get("result"):
                        fresh = TeachView(sentence, a.replay)
                        fresh.t0 = view.t0
                        for e in s.get("events", []):
                            fresh.feed(e)
                        view.__dict__.update(fresh.__dict__)
                        live.update(view)
                except Exception:  # noqa: BLE001
                    pass
                last_ev = time.time()
                if not sse_ok:
                    time.sleep(1)
            live.refresh()
    stop.set()
    if EXPORT:
        rec = Console(record=True, file=io.StringIO(), width=W, highlight=False)
        rec.print(view)
        with open(EXPORT, "a") as f:
            f.write(rec.export_text())
    sys.exit(0 if view.final.get("ok") else 1)


# ----------------------------------------------------------------------------- refit
def cmd_refit(a):
    rev = a.rev.upper()
    export_header(f"49th refit {rev}")
    try:
        st = post("/api/revision", {"rev": rev}).json()
    except Exception:  # noqa: BLE001
        server_down()
    summary = st.get("change_summary") or "(no change summary)"
    failing = [c for c in st.get("checks") or [] if not c.get("pass")]
    ctypes = {c.get("id"): c.get("type") for c in (st.get("board") or {}).get("connectors") or []}

    head = [Text(), header(f"refit · Rev {rev}", f"{CUSTOMER} · Sensor Hub enclosure"), Text()]
    ch = Table.grid(padding=(0, 2))
    ch.add_column(style="dim", justify="right", no_wrap=True)
    ch.add_column()
    ch.add_row("change", Text(summary, style="bold"))
    ch.add_row("old enclosure", Text(f"{len(failing)}/{len(st.get('checks') or [])} checks failing",
                                     style="red" if failing else "green"))
    head.append(ch)
    console.print(Group(*head), width=min(W, console.width))

    t0 = time.time()
    with console.status(f"[cyan]{AGENT} (acme-9b) is refitting…", spinner="dots"):
        try:
            res = post("/api/qm/refit", {"model": "tuned"}, timeout=240).json()
        except Exception as ex:  # noqa: BLE001
            out(Text.assemble(bad(), Text(f"  refit failed: {ex}", style="red")))
            sys.exit(1)
    wall = time.time() - t0

    # connectors a failing check names (e.g. "J5 (usb_a) opening ... 1.10 mm") -> their tolerance in red;
    # the connector type the agent was taught (active promoted lesson) -> its tolerance in green when it passes
    bad_ids = set()
    for c in res.get("checks") or []:
        if not c.get("pass"):
            bad_ids |= set(re.findall(r"\b([A-Z]+\d+)\b", str(c.get("detail") or "")))
    taught = None
    try:
        ls = get("/api/learn/status", 2)
        taught = (ls.get("lesson") or {}).get("type")
        if not taught and ls.get("active_checkpoint") not in (None, "checkpoint.json"):
            taught = ((load_json(DATA / "checkpoint_current.json") or {}).get("lesson") or {}).get("type")
    except Exception:  # noqa: BLE001
        pass

    calls = Table.grid(padding=(0, 1))
    calls.add_column(style="dim", justify="right")
    calls.add_column(no_wrap=True)
    for i, c in enumerate(res.get("calls") or [], 1):
        args = c.get("args") or {}
        t = Text(c.get("tool", "?"), style="cyan")
        if isinstance(args, dict):
            conn = args.get("connector")
            tol_style = ("bold red" if conn in bad_ids else
                         "bold green" if (res.get("pass") and taught and ctypes.get(conn) == taught) else "")
            for k, v in args.items():
                if k in ("connector", "hole"):
                    t.append(f" {v}", style="bold")
                    if k == "connector" and ctypes.get(v):
                        t.append(f" {ctypes[v]}")
                else:
                    t.append(f"  {k} ", style="dim")
                    if k == "tolerance":
                        t.append(f"{v:.2f}" if isinstance(v, (int, float)) else str(v), style=tol_style)
                    else:
                        t.append(str(v))
        else:
            t.append(f" {args}")
        calls.add_row(f"{i}", t)

    checks = Table.grid(padding=(0, 2))
    checks.add_column(width=1)
    checks.add_column(no_wrap=True)
    checks.add_column()
    for c in res.get("checks") or []:
        passed = c.get("pass")
        checks.add_row(ok() if passed else bad(), Text(c.get("label", c.get("id", "?")), style="" if passed else "bold"),
                       Text("") if passed else Text(str(c.get("detail") or ""), style="red", overflow="fold"))

    body = [Text("tool calls", style="dim"), calls, Text(), Text("checks", style="dim"), checks, Text()]
    npass = sum(1 for c in res.get("checks") or [] if c.get("pass"))
    ntot = len(res.get("checks") or [])
    verdict = Text()
    verdict.append(" PASS " if res.get("pass") else " FAIL ",
                   style="bold black on green" if res.get("pass") else "bold white on red")
    verdict.append(f"  {npass}/{ntot} checks", style="bold")
    lat = res.get("latency_s")
    verdict.append(f"   ·   model latency {lat:.1f} s" if isinstance(lat, (int, float)) else f"   ·   {wall:.1f} s",
                   style="dim")
    if res.get("cached"):
        verdict.append("   ·   cached replay", style="yellow")
    body.append(verdict)
    if res.get("error"):
        body.append(Text(f"error: {res['error']}", style="red"))
    if res.get("gbrain_slug"):
        body.append(Text.assemble(dim("change record → GBrain  "), Text(res["gbrain_slug"])))
    body.append(Text.assemble(dim("3D viewer  "), Text(res.get("viewer_url") or SERVER + "/", style="underline cyan")))

    console.print(Group(Text(), *body), width=min(W, console.width))
    if EXPORT:
        rec = Console(record=True, file=io.StringIO(), width=W, highlight=False)
        rec.print(Group(*head, Text(), *body))
        with open(EXPORT, "a") as f:
            f.write(rec.export_text())


# ----------------------------------------------------------------------------- reset
def cmd_reset(a):
    export_header("49th reset")
    try:
        r = post("/api/learn/reset")
    except Exception:  # noqa: BLE001
        server_down()
    g = Table.grid(padding=(0, 2))
    g.add_column(width=1)
    g.add_column(no_wrap=True)
    if r.status_code == 409:
        out(Text.assemble(bad(), Text("  busy: a learn run is in progress (409), nothing reset", style="red")))
        sys.exit(1)
    lr = r.json()
    active = lr.get("active_checkpoint")
    if lr.get("unpromoted"):
        g.add_row(ok(), Text.assemble("learned weights un-promoted  ", dim(f"(acme-9b back to v1 · {active})")))
    else:
        g.add_row(ok(), Text.assemble("acme-9b already on v1  ", dim(f"({active})")))
    g.add_row(ok(), Text.assemble("learn run log cleared  ", dim("(the USB-A rule is unknown again)")))
    try:
        s = post("/api/reset").json()
        brd = (s.get("board") or {}).get("rev")
        g.add_row(ok(), Text.assemble("board reset  ", dim(f"(Rev {brd or 'A'} + matching enclosure)")))
    except Exception:  # noqa: BLE001
        g.add_row(bad(), Text("board reset failed", style="red"))
    out(Group(Text(), header("reset", "ready for the next take"), Text(), g))


# ----------------------------------------------------------------------------- main
def main(argv=None):
    p = argparse.ArgumentParser(prog="49th", description=f"{AGENT} · {COMPANY}")
    sp = p.add_subparsers(dest="cmd", required=True)
    sp.add_parser("status", help="one-panel health check").set_defaults(fn=cmd_status)
    e = sp.add_parser("eval", help="held-out results table")
    e.add_argument("--wall", action="store_true", help="also show the 48-board wall waves")
    e.set_defaults(fn=cmd_eval)
    t = sp.add_parser("teach", help='teach one rule: 49th teach "Our USB-A cutouts always get 0.5 mm per side."')
    t.add_argument("sentence", nargs="+")
    t.add_argument("--replay", action="store_true", help="replay a recorded real run (for re-takes)")
    t.set_defaults(fn=cmd_teach)
    r = sp.add_parser("refit", help="land a board revision and refit the enclosure")
    r.add_argument("rev", choices=["B", "C", "b", "c"])
    r.set_defaults(fn=cmd_refit)
    sp.add_parser("reset", help="un-promote learned weights and reset the board").set_defaults(fn=cmd_reset)
    a = p.parse_args(argv)
    try:
        a.fn(a)
    except KeyboardInterrupt:
        console.print(dim("\ninterrupted"))
        sys.exit(130)


if __name__ == "__main__":
    main()
