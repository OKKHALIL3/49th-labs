"""49th Labs -- personal AI engineer commands (terminal).

    python3 -m rev.cli_ext engineers
    python3 -m rev.cli_ext record  --as senior-me [--dir workstation]
    python3 -m rev.cli_ext test    --model 9b (--untrained | --custom | --engineer ID | --compare) [--suite usb_a] [--limit N]
    python3 -m rev.cli_ext ask     senior-me refit C [--model 9b]      |  ask --all refit C
    python3 -m rev.cli_ext teach   --as field-me "USB-A openings need 0.5 mm per side"
    python3 -m rev.cli_ext merge   --as senior-me
    python3 -m rev.cli_ext reset

All numbers shown come from rev.engineers return values / emitted events.
"""
from __future__ import annotations

import argparse
import sys
import threading
import time

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text

console = Console(width=min(Console().width or 110, 112), highlight=False)

ACCENT = "bold cyan"
OK = "bold green"
BAD = "bold red"
DIM = "grey50"
SPARK = "▁▂▃▄▅▆▇█"
TEAM = ["senior-me", "field-me", "junior-me"]
MODEL_NAMES = {"9b": "Qwen3.5-9B", "35b": "Qwen3.5-35B", "122b": "Qwen3.5-122B", "397b": "Qwen3.5-397B"}


def E():
    from rev import engineers  # imported lazily so --help is instant
    return engineers


def g(d, *keys, default=None):
    """First present key from a dict (tolerant of small API differences)."""
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def spark(vals):
    vals = [v for v in vals if isinstance(v, (int, float))]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    return "".join(SPARK[min(7, int((v - lo) / rng * 7))] for v in vals)


def mark(ok):
    return Text("✓", style=OK) if ok else Text("✗", style=BAD)


def model_name(m):
    try:
        models = getattr(E(), "MODELS", {}) or {}
        v = models.get(m) if isinstance(models, dict) else None
        if isinstance(v, dict):
            return g(v, "name", "label", "base", default=MODEL_NAMES.get(m, m))
        if isinstance(v, str):
            return v
        if v is not None and getattr(v, "label", None):
            return v.label
    except Exception:
        pass
    return MODEL_NAMES.get(m, m)


def fmt_score(p, n):
    return f"{p}/{n}" if n is not None else str(p)


# ------------------------------------------------------------------ training progress (shared)

class TrainView:
    """Renders 'learn' events: step k/K, loss sparkline, before/after."""

    def __init__(self, who):
        self.who = who
        self.losses = []
        self.step = 0
        self.total = 0
        self.stage = ""
        self.msg = ""

    def feed(self, ev):
        st = ev.get("step")
        tot = ev.get("total_steps") or ev.get("total")
        if isinstance(ev.get("loss"), (int, float)):
            self.losses.append(float(ev["loss"]))
        if ev.get("stage"):
            self.stage = ev["stage"]
        if isinstance(st, int) and self.stage in ("train", "training", "step"):
            self.step, self.total = st, tot or self.total
        elif isinstance(st, int) and isinstance(ev.get("loss"), (int, float)):
            self.step, self.total = st, tot or self.total
        self.msg = ev.get("msg") or self.msg

    def render(self):
        t = Text()
        t.append("  ⟳ ", style=ACCENT)
        t.append(f"{self.who}'s engineer training ", style="bold")
        if self.total:
            done = int(20 * self.step / max(1, self.total))
            t.append("█" * done, style="cyan")
            t.append("░" * (20 - done), style=DIM)
            t.append(f" step {self.step}/{self.total}")
        if self.losses:
            t.append(f"  loss {self.losses[-1]:.3f} ", style="yellow")
            t.append(spark(self.losses), style="yellow")
        if self.msg:
            t.append(f"\n    {self.msg[:96]}", style=DIM)
        return t


def learned_line(who, res):
    """'✓ senior-me's engineer learned it: USB-A x/12 → y/12 (Ns)'"""
    ok = g(res, "ok", "pass", default=True)
    b = g(res, "before", "before_pass")
    a = g(res, "after", "after_pass")
    n = g(res, "n", "n_test", "total")
    if isinstance(b, dict):
        n = n or g(b, "n", "total")
        b = g(b, "passed", "pass", "n_pass")
    if isinstance(a, dict):
        n = n or g(a, "n", "total")
        a = g(a, "passed", "pass", "n_pass")
    secs = g(res, "seconds", "secs", "t")
    lesson = g(res, "lesson", default={})
    ctype = g(res, "ctype", "connector", default=g(lesson if isinstance(lesson, dict) else {}, "type", default="usb_a"))
    label = {"usb_a": "USB-A", "usb_c": "USB-C", "rj45": "RJ45", "dc_barrel": "DC barrel"}.get(ctype, str(ctype))
    t = Text()
    if not ok:
        t.append("  ✗ ", style=BAD)
        t.append(f"{who}'s engineer did not learn it: {g(res, 'error', 'msg', default='rejected')}", style=BAD)
        return t
    t.append("  ✓ ", style=OK)
    t.append(f"{who}'s engineer learned it", style=OK)
    if b is not None and a is not None:
        t.append(f": {label} ")
        t.append(fmt_score(b, n), style=BAD)
        t.append(" → ")
        t.append(fmt_score(a, n), style=OK)
    if secs is not None:
        t.append(f"  ({float(secs):.0f}s)", style=DIM)
    ck = g(res, "checkpoint", "checkpoint_path", "out_path")
    if isinstance(ck, str):
        t.append(f"\n    personal LoRA branch → {ck}", style=DIM)
    return t


# ------------------------------------------------------------------ 1. engineers

def cmd_engineers(a):
    rows = E().list_engineers()
    tb = Table(box=box.SIMPLE_HEAVY, header_style=ACCENT, expand=False, pad_edge=False)
    for c in ("handle", "role", "base", "lessons", "latest lesson", "personal LoRA"):
        tb.add_column(c, no_wrap=True, min_width={"handle": 9, "lessons": 7}.get(c), justify="right" if c == "lessons" else "left")
    for r in rows:
        lessons = g(r, "lessons", default=[])
        n = g(r, "n_lessons", "lesson_count", default=len(lessons) if isinstance(lessons, list) else lessons)
        latest = g(r, "latest_lesson", "latest")
        if latest is None and isinstance(lessons, list) and lessons:
            latest = lessons[-1]
        if isinstance(latest, dict):
            if latest.get("type") and latest.get("tolerance") is not None:
                latest = f"{latest['type']} {latest['tolerance']} mm · {latest.get('source') or ''}".strip(" ·")
            else:
                latest = g(latest, "text", "summary", "rule", default=str(latest))
        ck = g(r, "checkpoint", "personal_checkpoint", "checkpoint_status", "status")
        steps = g(r, "trained_steps", default=0)
        if isinstance(ck, dict):
            ck = g(ck, "status", "step", "path", default="trained")
        ck_txt = Text(f"● trained · {steps} steps" if steps else "● trained", style=OK) if ck else Text("= company v1", style=DIM)
        tb.add_row(Text(str(g(r, "id", "handle", default="?")), style="bold"),
                   str(g(r, "role", default="")).replace(" mechanical engineer", " ME").replace("mechanical engineer", "ME").replace("Field / ruggedization ME", "Field ME")[:18],
                   "v1 · 9B+LoRA",
                   str(n or 0), Text(str(latest or "—")[:24], style=DIM if not latest else ""), ck_txt)
    console.print(Panel(tb, title="[bold]49th Labs · AI engineers[/]", subtitle=f"[{DIM}]{len(rows)} engineers · company model v1 → personal branches[/]",
                        border_style="cyan", expand=False))


# ------------------------------------------------------------------ 2. record

def capture_line(ev, who):
    t = Text()
    ok = g(ev, "ok", "verified_ok", default=True)
    t.append("✓ " if ok else "✗ ", style=OK if ok else BAD)
    t.append(str(g(ev, "time", "ts", default=time.strftime("%H:%M:%S")))[-8:] + " ", style=DIM)
    t.append("saved ", style="bold")
    t.append(str(g(ev, "file", "filename", "path", default="?")).split("/")[-1], style="bold white")
    change = g(ev, "change", "diff", "summary")
    if not change and isinstance(ev.get("calls"), list):
        parts = []
        for c in ev["calls"][:3]:
            a = c.get("args", {}) if isinstance(c, dict) else {}
            parts.append(" ".join(str(x) for x in (c.get("tool") if isinstance(c, dict) else c, a.get("connector") or a.get("id") or "",
                                                  (f"{a['tolerance']} mm/side" if a.get("tolerance") is not None else "")) if x))
        change = ", ".join(parts)
    if change:
        t.append(f" · {change}")
    ver = g(ev, "verified", "checks")
    if isinstance(ver, (list, tuple)):
        ver = f"{sum(1 for x in ver if x)}/{len(ver)}"
    if isinstance(ver, dict):
        ver = f"{sum(1 for x in ver.values() if x)}/{len(ver)}"
    if ver is not None:
        t.append(" · verified ")
        t.append(str(ver), style=OK if ok else BAD)
    rule = g(ev, "rule", "lesson", "inferred_rule")
    if isinstance(rule, dict):
        rule = f"{g(rule, 'type', default='')} {g(rule, 'tolerance', default='')} mm"
    if rule:
        t.append(" · rule ")
        t.append(str(rule), style="yellow")
    slug = g(ev, "slug", "gbrain_slug", "gbrain")
    if slug:
        t.append(f" · → GBrain {slug}", style=DIM)
    return t


def cmd_record(a):
    who = a.who
    console.print(Panel(Text.assemble(("● ", BAD), (f"Recording {who}'s work", "bold"), ("  →  ", DIM),
                                      (f"GBrain engineers/{who}/", "cyan"), ("  →  ", DIM), ("River", "cyan"),
                                      (f"\n  watching {a.dir.rstrip('/').split('/')[-1]}/  ·  every save is checked, recorded, and learned  ·  Ctrl-C to stop", DIM)),
                        border_style="red", expand=False))
    stats = {"captures": 0, "verified": 0, "learned": 0, "t0": time.time()}
    tv = {"v": None}
    live = Live(Text(""), console=console, refresh_per_second=8, transient=True)
    lock = threading.Lock()

    def emit(ev):
        with lock:
            typ = ev.get("type")
            if typ == "learn":
                if tv["v"] is None:
                    tv["v"] = TrainView(who)
                    live.start()
                tv["v"].feed(ev)
                live.update(tv["v"].render())
                return
            if typ in ("learned", "learn_done", "trained"):
                if tv["v"] is not None:
                    live.stop()
                    if tv["v"].losses:
                        console.print(Text(f"    loss {tv['v'].losses[0]:.3f} → {tv['v'].losses[-1]:.3f}  {spark(tv['v'].losses)}", style="yellow"))
                    tv["v"] = None
                    live.__init__(Text(""), console=console, refresh_per_second=8, transient=True)
                console.print(learned_line(who, ev))
                if g(ev, "ok", default=True):
                    stats["learned"] += 1
                return
            if typ == "rejected":
                stats["captures"] += 1
                console.print(Text.assemble(("✗ ", BAD), (time.strftime("%H:%M:%S") + " ", DIM), ("saved ", "bold"),
                                            (str(ev.get("file", "?")).split("/")[-1], "bold white"),
                                            (" · checker rejected: ", BAD), (str(ev.get("reason", ""))[:70], ""),
                                            (" · not learned", DIM)))
                return
            if typ == "capture_gbrain":
                console.print(Text.assemble(("    → GBrain ", DIM), (str(ev.get("slug")), "cyan"),
                                            ("  ✓" if ev.get("ok") else "  (write failed)", OK if ev.get("ok") else BAD)))
                return
            if typ in ("recording", "stopped"):
                return
            if typ in ("capture", "save", "captured") or "file" in ev or "filename" in ev:
                stats["captures"] += 1
                if g(ev, "ok", "verified_ok", default=True):
                    stats["verified"] += 1
                console.print(capture_line(ev, who))
                return
            msg = ev.get("msg") or ev.get("message")
            if msg:
                console.print(Text(f"  · {msg}", style=DIM))

    try:
        E().record(who, a.dir, emit=emit, auto_teach=not a.no_teach)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            live.stop()
        except Exception:
            pass
    s = Table.grid(padding=(0, 2))
    s.add_row(Text("■ stopped", style="bold"), Text(f"{stats['captures']} saves recorded", style="bold"),
              Text(f"{stats['verified']} verified", style=OK), Text(f"{stats['learned']} lessons learned", style=OK),
              Text(f"{time.time() - stats['t0']:.0f}s", style=DIM))
    console.print(Panel(s, border_style="grey50", expand=False, title=f"[bold]{who}[/] session"))


# ------------------------------------------------------------------ 3. test

def variant_label(model, variant, eng):
    mn = model_name(model)
    if variant == "untrained":
        return f"{mn} untrained"
    if variant == "engineer":
        return f"{mn} + {eng}'s engineer"
    return f"{mn} Acme custom"


def run_test(a, variant, eng=None):
    label = variant_label(a.model, variant, eng)
    dots = Text()
    results = []
    prog = Progress(SpinnerColumn(style="cyan"), TextColumn("[bold]{task.description}"), BarColumn(bar_width=34, complete_style="green", finished_style="green"),
                    MofNCompleteColumn(), TextColumn("[green]{task.fields[p]} pass[/] [red]{task.fields[f]} fail[/]"), TimeElapsedColumn(),
                    console=console, transient=True)
    try:
        n_suite = len(E()._load_suite(a.suite))
        n_suite = min(n_suite, a.limit) if a.limit else n_suite
    except Exception:
        n_suite = a.limit or None
    tid = prog.add_task(label, total=n_suite, p=0, f=0)
    t0 = time.time()
    lock = threading.Lock()

    def emit(ev):
        with lock:
            if "pass" not in ev or ev.get("type") in ("summary", "done", "result_summary"):
                n = g(ev, "n", "total")
                if isinstance(n, int) and n > 0:
                    prog.update(tid, total=n)
                return
            results.append(ev)
            ok = bool(ev["pass"])
            dots.append("●" if ok else "●", style="green" if ok else "red")
            n = g(ev, "n", "total")
            if isinstance(n, int) and n > 0:
                prog.update(tid, total=n)
            p = sum(1 for r in results if r["pass"])
            prog.update(tid, completed=len(results), p=p, f=len(results) - p)
            live.update(Group(prog, Text("  ") + dots))

    with Live(Group(prog, Text("")), console=console, refresh_per_second=10, transient=True) as live:
        res = E().test(a.model, variant, eng, a.suite, a.limit, a.workers, emit)
    secs = g(res, "seconds", "secs", default=round(time.time() - t0, 1))
    passed = g(res, "passed", "n_pass", "pass_count")
    total = g(res, "n", "total", "n_tasks")
    rows = g(res, "results", "rows", default=results) or results
    if passed is None:
        passed = sum(1 for r in rows if r.get("pass"))
    if total is None:
        total = len(rows)
    by = g(res, "by_check", "checks")
    if not isinstance(by, dict):
        by = {}
        for r in rows:
            for k, v in (r.get("checks") or {}).items():
                by.setdefault(k, [0, 0])
                by[k][0] += bool(v)
                by[k][1] += 1
    return {"label": g(res, "label", default=label), "passed": passed, "total": total, "by": by, "secs": secs, "dots": dots,
            "suite": g(res, "suite", default=a.suite)}


def _by_str(v, total):
    if isinstance(v, (list, tuple)) and len(v) == 2:
        return v[0], v[1]
    if isinstance(v, dict):
        return g(v, "passed", "pass", default=0), g(v, "n", "total", default=total)
    if isinstance(v, float) and v <= 1.0:
        return round(v * total), total
    return v, total


def print_result(r):
    ok = r["passed"] == r["total"] and r["total"]
    big = Text()
    big.append(f"{r['label']}: ", style="bold")
    big.append(f"{r['passed']}/{r['total']}", style=OK if ok else (BAD if r["passed"] < r["total"] / 2 else "bold yellow"))
    big.append(f"   suite {r['suite']} · {float(r['secs']):.0f}s", style=DIM)
    parts = [big, Text("  ") + r["dots"]]
    if r["by"]:
        bt = Table.grid(padding=(0, 3))
        cells = []
        for k, v in r["by"].items():
            p, n = _by_str(v, r["total"])
            cells.append(Text.assemble((f"{k} ", DIM), (f"{p}/{n}", OK if p == n else BAD)))
        bt.add_row(*cells)
        parts.append(bt)
    console.print(Panel(Group(*parts), border_style="green" if ok else "red", expand=False))


def cmd_test(a):
    if a.compare:
        out = [run_test(a, "untrained")]
        print_result(out[0])
        out.append(run_test(a, "custom"))
        print_result(out[1])
        if a.engineer:
            out.append(run_test(a, "engineer", a.engineer))
            print_result(out[2])
        tb = Table(box=box.SIMPLE_HEAVY, header_style=ACCENT, title=f"[bold]{model_name(a.model)} · untrained vs Acme custom[/]")
        tb.add_column("model")
        tb.add_column("pass", justify="right")
        checks = list(out[1]["by"].keys() or out[0]["by"].keys())
        for c in checks:
            tb.add_column(c, justify="right")
        tb.add_column("time", justify="right")
        for r in out:
            ok = r["passed"] == r["total"]
            cells = []
            for c in checks:
                p, n = _by_str(r["by"].get(c, (0, r["total"])), r["total"])
                cells.append(Text(f"{p}/{n}", style="green" if p == n else "red"))
            tb.add_row(Text(r["label"], style="bold"), Text(f"{r['passed']}/{r['total']}", style=OK if ok else BAD), *cells,
                       Text(f"{float(r['secs']):.0f}s", style=DIM))
        console.print(tb)
        return
    variant = "untrained" if a.untrained else ("engineer" if a.engineer else "custom")
    print_result(run_test(a, variant, a.engineer))


# ------------------------------------------------------------------ 4. ask

def _opening(res):
    osum = g(res, "opening_summary")
    if isinstance(osum, str):
        segs = [x.strip() for x in osum.split(";")]
        usb = [x for x in segs if "(usb_a)" in x]
        return (usb[0].replace(" (usb_a)", "") if usb else osum)[:40]
    o = g(res, "opening", "usb_a_opening", "tolerance", "tol")
    if isinstance(o, dict):
        w, h = g(o, "w"), g(o, "h")
        tol = g(o, "tol", "tolerance")
        s = f"{w}×{h} mm" if w is not None else ""
        if tol is not None:
            s += f"  (+{tol} mm/side)"
        return s.strip() or str(o)
    if isinstance(o, (int, float)):
        return f"+{o} mm/side"
    return str(o) if o is not None else "—"


def _checks(res):
    c = g(res, "checks", default={})
    if isinstance(c, list):
        return {g(x, "id", default=str(i)): bool(g(x, "pass", default=False)) for i, x in enumerate(c)}
    return c or {}


def cmd_ask(a):
    if a.all:
        whos = ["company"] + TEAM
        rows = []
        with console.status(f"[cyan]asking {len(whos)} AI engineers to {a.verb} rev {a.rev}…", spinner="dots"):
            from concurrent.futures import ThreadPoolExecutor

            def one(w):
                t0 = time.time()
                r = E().refit_as(w, a.rev, a.model)
                r.setdefault("_secs", round(time.time() - t0, 1))
                return w, r
            with ThreadPoolExecutor(max_workers=len(whos)) as ex:
                rows = list(ex.map(one, whos))
        tb = Table(box=box.SIMPLE_HEAVY, header_style=ACCENT,
                   title=f"[bold]Refit rev {a.rev} · how each AI engineer would do it[/]  [{DIM}]{model_name(a.model)}[/]")
        tb.add_column("engineer", style="bold")
        tb.add_column("USB-A opening chosen")
        tb.add_column("tool calls", justify="right")
        tb.add_column("company checker", justify="center")
        tb.add_column("own rule", justify="center")
        tb.add_column("time", justify="right", style=DIM)
        for w, r in rows:
            chk = _checks(r)
            ok = g(r, "pass", "ok", default=all(chk.values()) if chk else False)
            n_calls = g(r, "n_calls", default=len(g(r, "calls", default=[]) or []))
            tb.add_row(w, _opening(r), str(n_calls),
                       Text(f"✓ {sum(chk.values())}/{len(chk)}" if ok else f"✗ {sum(chk.values())}/{len(chk)}", style=OK if ok else BAD),
                       (Text("✓ pass", style=OK) if r.get("personal_pass") else Text("✗ fail", style=BAD)) if "personal_pass" in r
                       else Text("= company", style=DIM),
                       f"{float(g(r, 'seconds', 'latency_s', '_secs', default=0)):.0f}s")
        console.print(tb)
        return
    who = a.who
    with console.status(f"[cyan]asking {who}'s AI engineer to {a.verb} rev {a.rev}…", spinner="dots"):
        t0 = time.time()
        r = E().refit_as(who, a.rev, a.model)
    chk = _checks(r)
    ok = g(r, "pass", "ok", default=all(chk.values()) if chk else False)
    body = []
    head = Text()
    head.append(f"{who}", style="bold cyan")
    head.append(f"  ·  {g(r, 'label', 'model_label', default=model_name(a.model))}", style=DIM)
    body.append(head)
    calls = g(r, "calls", default=[]) or []
    ct = Table(box=None, show_header=False, pad_edge=False)
    ct.add_column(style="yellow")
    ct.add_column(style=DIM)
    for c in calls[:10]:
        if isinstance(c, dict):
            name = g(c, "tool", "name", default="call")
            args = g(c, "args", "arguments", default={k: v for k, v in c.items() if k not in ("tool", "name")})
            argtxt = ", ".join(f"{k}={v}" for k, v in args.items()) if isinstance(args, dict) else str(args)
            ct.add_row(f"  → {name}", argtxt[:84])
        else:
            ct.add_row("  →", str(c)[:90])
    if len(calls) > 10:
        ct.add_row("  …", f"{len(calls) - 10} more")
    body.append(ct)
    body.append(Text.assemble(("  USB-A opening: ", "bold"), (_opening(r), "yellow")))
    cg = Table.grid(padding=(0, 3))
    cg.add_row(*[Text.assemble(("✓ " if v else "✗ ", OK if v else BAD), (k, "")) for k, v in chk.items()])
    body.append(cg)
    if "personal_pass" in r:
        body.append(Text.assemble(("  own rule profile ", DIM), (str(r.get("personal_profile")), "yellow"), ("  → ", DIM),
                                  ("pass" if r["personal_pass"] else "fail", OK if r["personal_pass"] else BAD)))
    body.append(Text(("  PASS — company checker " if ok else "  FAIL — company checker ") + f"{sum(chk.values())}/{len(chk)}", style=OK if ok else BAD))
    console.print(Panel(Group(*body), title=f"[bold]{a.verb} rev {a.rev}[/]", subtitle=f"[{DIM}]{g(r, 'seconds', 'latency_s', default=round(time.time() - t0, 1))}s[/]",
                        border_style="green" if ok else "red", expand=False))


# ------------------------------------------------------------------ 5. teach / merge / reset

def cmd_teach(a):
    who = a.who
    console.print(Text.assemble(("● ", ACCENT), (f"{who}", "bold"), (" teaches their engineer: ", ""), (f"“{a.text}”", "italic yellow")))
    tv = TrainView(who)
    out = {}
    with Live(tv.render(), console=console, refresh_per_second=8, transient=True) as live:
        def emit(ev):
            if ev.get("type") == "learn":
                tv.feed(ev)
                live.update(tv.render())
            elif ev.get("type") in ("learned", "trained"):
                out.update(ev)
            elif ev.get("slug"):
                console.print(Text(f"  · recorded → GBrain {ev['slug']}", style=DIM))
        res = E().teach(who, a.text, a.source, emit) or out
    if tv.losses:
        console.print(Text(f"    loss {tv.losses[0]:.3f} → {tv.losses[-1]:.3f}  {spark(tv.losses)}", style="yellow"))
    console.print(learned_line(who, res if isinstance(res, dict) else out))


def cmd_merge(a):
    who = a.who
    console.print(Text.assemble(("⇪ ", ACCENT), (f"merging {who}'s lessons into the company model", "bold"), ("  (weight PR)", DIM)))
    msgs = []

    def emit(ev):
        m = ev.get("msg")
        if m:
            st = ev.get("stage", "")
            console.print(Text.assemble(("  · ", DIM), (f"{st:10s} ", "cyan"), (str(m)[:92], "")))
            msgs.append(ev)

    with console.status("[cyan]running weight CI…", spinner="dots"):
        res = E().merge(who, emit, open_pr=getattr(a, "open", False)) or {}
    status = str(g(res, "status", "state", default="prepared"))
    if status == "nothing_to_merge":
        console.print(Text.assemble(("  · ", DIM), (f"{who} has no personal lessons yet -- nothing to merge. ", "yellow"),
                                    (f"Try: teach --as {who} \"USB-A cutouts get 0.5 mm per side\"", DIM)))
        return
    n = g(res, "pr", "number", "pr_number")
    ok = status not in ("blocked", "rejected", "failed", "nothing_to_merge", "closed")
    if status == "pr_prepared":
        status = "PR prepared"
    t = Table.grid(padding=(0, 2))
    t.add_row(Text("PR", style=DIM), Text(f"#{n}" if n else "PR prepared", style="bold"))
    if g(res, "title"):
        t.add_row(Text("title", style=DIM), Text(str(res["title"])))
    t.add_row(Text("status", style=DIM), Text(status, style=OK if ok else BAD))
    if not g(res, "title") and g(res, "text"):
        t.add_row(Text("lesson", style=DIM), Text(f"“{res['text']}”", style="yellow"))
    for k in ("author", "from_checkpoint", "next", "lesson_before", "lesson_after", "regression", "decision", "reason", "version", "seconds"):
        v = g(res, k)
        if v is not None and not isinstance(v, (dict, list)):
            t.add_row(Text(k.replace("_", " "), style=DIM), Text(str(v)))
    console.print(Panel(t, title=f"[bold]weight PR · {who} → company[/]", border_style="green" if ok else "red", expand=False))


def cmd_reset(a):
    res = E().reset_all()
    console.print(Text.assemble(("✓ ", OK), ("reset: personal branches restored to pristine (senior-me 0.5, field-me 0.8, junior-me = company v1); production untouched", "bold")))
    if isinstance(res, dict) and res:
        console.print(Text("  " + ", ".join(f"{k}={v}" for k, v in res.items() if not isinstance(v, (dict, list))), style=DIM))


# ------------------------------------------------------------------ main

def main(argv=None):
    p = argparse.ArgumentParser(prog="49th", description="49th Labs -- every engineer gets their own AI engineer")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("engineers", help="list the company's AI engineers")

    r = sub.add_parser("record", help="record your work into your AI engineer")
    r.add_argument("--as", dest="who", default="senior-me")
    r.add_argument("--dir", default="workstation")
    r.add_argument("--no-teach", action="store_true")

    t = sub.add_parser("test", help="eval a model: untrained vs Acme custom vs an engineer's branch")
    t.add_argument("--model", default="9b", choices=list(MODEL_NAMES))
    grp = t.add_mutually_exclusive_group()
    grp.add_argument("--untrained", action="store_true")
    grp.add_argument("--custom", action="store_true")
    grp.add_argument("--compare", action="store_true")
    t.add_argument("--engineer", default=None)
    t.add_argument("--suite", default="heldout", choices=["heldout", "usb_a"])
    t.add_argument("--limit", type=int, default=None)
    t.add_argument("--workers", type=int, default=8)

    k = sub.add_parser("ask", help="ask an AI engineer how they'd do a job")
    k.add_argument("who", nargs="?", default="company")
    k.add_argument("verb", nargs="?", default="refit")
    k.add_argument("rev", nargs="?", default="C")
    k.add_argument("--all", action="store_true")
    k.add_argument("--model", default="9b", choices=list(MODEL_NAMES))

    te = sub.add_parser("teach", help="teach your AI engineer a rule")
    te.add_argument("--as", dest="who", default="senior-me")
    te.add_argument("--source", default="slack")
    te.add_argument("text")

    m = sub.add_parser("merge", help="open a weight PR from your engineer into the company model")
    m.add_argument("--as", dest="who", default="senior-me")
    m.add_argument("--open", action="store_true", help="run the real weight CI (retrains; merges into production if the gate passes)")

    sub.add_parser("reset", help="reset every personal branch")

    argv = list(sys.argv[1:] if argv is None else argv)
    # `ask --all refit C` -> positional shuffle
    if argv[:1] == ["ask"] and "--all" in argv:
        rest = [x for x in argv[1:] if x != "--all"]
        pos = [x for x in rest if not x.startswith("--")]
        opt = [x for i, x in enumerate(rest) if x.startswith("--") or (i > 0 and rest[i - 1] == "--model")]
        pos = [x for x in pos if x not in opt]
        argv = ["ask", "company"] + pos[-2:] + opt + ["--all"]
    a = p.parse_args(argv)
    try:
        {"engineers": cmd_engineers, "record": cmd_record, "test": cmd_test, "ask": cmd_ask, "teach": cmd_teach,
         "merge": cmd_merge, "reset": cmd_reset}[a.cmd](a)
    except KeyboardInterrupt:
        console.print(Text("\n■ stopped", style=DIM))


if __name__ == "__main__":
    main()
