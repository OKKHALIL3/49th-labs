"""GBrain wrapper: REV's project memory (facts, not skill).

Facts about the project (current design, change records) live in a local,
keyless GBrain brain (PGLite, no embeddings). The company's unwritten house
rules are NOT stored here -- those are learned by the model's weights.

Every public function swallows errors (logs + returns None/[]/False).

CLI used (all via subprocess, 20 s timeout, GBRAIN_HOME=<repo>/brain/.home):
  gbrain init --pglite --non-interactive --no-embedding --skip-embed-check
  gbrain import <staging_dir> --no-embed     (write; `put` does not chunk, so search misses it)
  gbrain get <slug>
  gbrain search <q> --json --limit N
  gbrain list --limit N
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

log = logging.getLogger("rev.gbrain")

REPO = Path(__file__).resolve().parent.parent
BRAIN_DIR = REPO / "brain"                      # seed markdown lives here
GBRAIN_HOME = Path(os.environ.get("REV_GBRAIN_HOME", str(BRAIN_DIR / ".home")))
GBRAIN_BIN = os.environ.get("GBRAIN_BIN", str(Path.home() / ".bun" / "bin" / "gbrain"))
TIMEOUT = 20

PROJECT_SLUG = "sensor-hub"
DESIGN_A_SLUG = "sensor-hub/rev-a-enclosure"

_lock = threading.RLock()   # PGLite is single-writer; serialise all CLI calls
_ready = False
TIMINGS: list[tuple[str, float]] = []   # (command, seconds) for the last calls


# --------------------------------------------------------------------------- low level
def _env() -> dict:
    env = dict(os.environ)
    env["GBRAIN_HOME"] = str(GBRAIN_HOME)
    env.setdefault("NO_COLOR", "1")
    env["PATH"] = str(Path(GBRAIN_BIN).parent) + os.pathsep + env.get("PATH", "")
    return env


def _run(args: list[str], stdin: str | None = None, timeout: int = TIMEOUT):
    """Run gbrain; return (returncode, stdout). Never raises."""
    t0 = time.time()
    try:
        with _lock:
            p = subprocess.run([GBRAIN_BIN, *args], input=stdin, capture_output=True,
                               text=True, timeout=timeout, env=_env(), cwd=str(REPO))
        dt = time.time() - t0
        TIMINGS.append((" ".join(args[:2]), round(dt, 2)))
        del TIMINGS[:-50]
        if p.returncode != 0:
            log.warning("gbrain %s -> rc=%s %s", args[:2], p.returncode, (p.stderr or p.stdout)[-300:])
        return p.returncode, p.stdout or ""
    except Exception as e:  # timeout, missing binary, ...
        log.warning("gbrain %s failed: %s", args[:2], e)
        return -1, ""


def ensure_brain() -> bool:
    """Create the local keyless PGLite brain if it doesn't exist yet."""
    global _ready
    if _ready:
        return True
    try:
        if (GBRAIN_HOME / ".gbrain" / "config.json").exists():
            _ready = True
            return True
        GBRAIN_HOME.mkdir(parents=True, exist_ok=True)
        _run(["init", "--pglite", "--non-interactive", "--no-embedding", "--skip-embed-check"], timeout=120)
        _ready = (GBRAIN_HOME / ".gbrain" / "config.json").exists()
        return _ready
    except Exception as e:
        log.warning("gbrain init failed: %s", e)
        return False


_FM = re.compile(r"\A---\s*\n.*?\n---\s*\n?", re.S)


def _strip_frontmatter(md: str) -> str:
    return _FM.sub("", md or "", count=1).lstrip("\n")


def _frontmatter_title(md: str) -> str | None:
    m = _FM.match(md or "")
    if not m:
        return None
    t = re.search(r"^title:\s*(.+)$", m.group(0), re.M)
    return t.group(1).strip().strip("'\"") if t else None


# --------------------------------------------------------------------------- public API (SPEC 9)
def put_page(slug: str, title: str, markdown: str) -> bool:
    """Create or replace a page. Returns True on success. Never raises."""
    try:
        if not ensure_brain():
            return False
        slug = slug.strip("/")
        body = _strip_frontmatter(markdown)
        safe_title = (title or slug).replace("\n", " ").replace('"', "'")
        doc = f'---\ntitle: "{safe_title}"\ntype: concept\n---\n\n{body.rstrip()}\n'
        stage_root = GBRAIN_HOME / "staging"
        stage_root.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(dir=stage_root))
        try:
            f = stage / f"{slug}.md"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(doc)
            rc, out = _run(["import", str(stage), "--no-embed"])
            # "1 pages imported" or "(1 unchanged ...)" are both success
            return rc == 0 and "Import complete" in out and not re.search(r"\b[1-9]\d* (errors|malformed)", out)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    except Exception as e:
        log.warning("put_page(%s) failed: %s", slug, e)
        return False


def get_page(slug: str) -> str | None:
    """Return the page markdown (frontmatter stripped) or None."""
    try:
        if not ensure_brain():
            return None
        rc, out = _run(["get", slug.strip("/")])
        if rc != 0 or not out.strip():
            return None
        return _strip_frontmatter(out)
    except Exception as e:
        log.warning("get_page(%s) failed: %s", slug, e)
        return None


_STOP = {"the", "a", "an", "is", "are", "was", "why", "what", "where", "when", "how", "who", "here", "there",
         "this", "that", "it", "its", "of", "to", "in", "on", "for", "and", "or", "do", "does", "did", "we",
         "our", "be", "at", "by", "with", "from", "as", "so", "put", "placed"}


def _snippet(text: str, q: str, n: int = 240) -> str:
    """Pick the line(s) of the chunk that mention a query term, else the start of the chunk."""
    terms = [t.lower() for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-_.]*", q) if t.lower() not in _STOP and len(t) > 1]
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip() and not set(ln.strip()) <= set("|-: ")]
    best = max(lines, key=lambda ln: sum(t in ln.lower() for t in terms), default="") if terms else ""
    if best and any(t in best.lower() for t in terms):
        return re.sub(r"\s+", " ", best)[:n]
    return re.sub(r"\s+", " ", text or "").strip()[:n]


def _search_raw(q: str, limit: int) -> list[dict]:
    rc, out = _run(["search", q, "--json", "--limit", str(limit)])
    if rc != 0:
        return []
    i = out.find("[")
    if i < 0:
        return []
    try:
        rows = json.loads(out[i:])
    except Exception:
        return []
    res, seen = [], set()
    for r in rows if isinstance(rows, list) else []:
        slug = r.get("slug")
        if not slug or slug in seen:
            continue
        seen.add(slug)
        text = (r.get("chunk_text") or r.get("snippet") or "").strip()
        res.append({"slug": slug, "title": r.get("title") or slug, "snippet": _snippet(text, q)})
    return res


def search(q: str, limit: int = 8) -> list[dict]:
    """Keyword search -> [{"slug","title","snippet"}]. Falls back to OR-of-terms. Never raises."""
    try:
        if not q or not q.strip() or not ensure_brain():
            return []
        res = _search_raw(q, limit)
        if res:
            return res
        terms = [t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-_.]*", q) if t.lower() not in _STOP and len(t) > 1]
        if len(terms) > 1:
            return _search_raw(" or ".join(terms), limit)
        return []
    except Exception as e:
        log.warning("search(%r) failed: %s", q, e)
        return []


def list_pages(limit: int = 100) -> list[dict]:
    """-> [{"slug","title","type","updated"}]. Never raises."""
    try:
        if not ensure_brain():
            return []
        rc, out = _run(["list", "--limit", str(limit)])
        if rc != 0:
            return []
        pages = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 4:
                pages.append({"slug": parts[0], "type": parts[1], "updated": parts[2], "title": parts[3]})
        return sorted(pages, key=lambda p: p["slug"])
    except Exception as e:
        log.warning("list_pages failed: %s", e)
        return []


# --------------------------------------------------------------------------- design + change records
def _fmt(v) -> str:
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def design_markdown(board: dict, enc: dict) -> str:
    """Markdown for a design page: board summary + enclosure parameters (facts only, no house rules)."""
    rev = board.get("rev", "?")
    L = [f"# {board.get('name', 'Sensor Hub')} Rev {rev} enclosure",
         "",
         f"Part of [[{PROJECT_SLUG}]]. Current released enclosure design for board Rev {rev}.",
         "",
         "## Board",
         "",
         "| Parameter | Value |", "|---|---|",
         f"| Revision | {rev} |",
         f"| Size (w x d x t) | {_fmt(board.get('w'))} x {_fmt(board.get('d'))} x {_fmt(board.get('t'))} mm |",
         f"| Tallest component | {_fmt(board.get('max_component_h'))} mm |",
         "",
         "| Hole | x | y | dia |", "|---|---|---|---|"]
    L += [f"| {h['id']} | {_fmt(h['x'])} | {_fmt(h['y'])} | {_fmt(h['dia'])} |" for h in board.get("holes", [])]
    L += ["", "| Connector | Type | Side | Pos | z | w x h |", "|---|---|---|---|---|---|"]
    L += [f"| {c['id']} | {c['type']} | {c['side']} | {_fmt(c['pos'])} | {_fmt(c.get('z'))} | "
          f"{_fmt(c.get('w'))} x {_fmt(c.get('h'))} |" for c in board.get("connectors", [])]
    cav = enc.get("cavity", {})
    org = enc.get("board_origin", {})
    L += ["", "## Enclosure", "",
          "| Parameter | Value |", "|---|---|",
          f"| Wall thickness | {_fmt(enc.get('wall'))} mm |",
          f"| Cavity (w x d x h) | {_fmt(cav.get('w'))} x {_fmt(cav.get('d'))} x {_fmt(cav.get('h'))} mm |",
          f"| Board origin in cavity | ({_fmt(org.get('x'))}, {_fmt(org.get('y'))}) mm |",
          f"| Standoff height | {_fmt(enc.get('standoff_h'))} mm |",
          "| Lid | open top |",
          "", "| Standoff | Hole | x | y | dia | bore |", "|---|---|---|---|---|---|"]
    L += [f"| S{i + 1} | {s['hole']} | {_fmt(s['x'])} | {_fmt(s['y'])} | {_fmt(s['dia'])} | {_fmt(s['bore'])} |"
          for i, s in enumerate(enc.get("standoffs", []))]
    L += ["", "| Opening | Connector | Wall | Pos | z | w x h |", "|---|---|---|---|---|---|"]
    L += [f"| O{i + 1} | {o['connector']} | {o['side']} | {_fmt(o['pos'])} | {_fmt(o['z'])} | "
          f"{_fmt(o['w'])} x {_fmt(o['h'])} |" for i, o in enumerate(enc.get("openings", []))]
    L += ["", "Coordinates in mm. Cavity origin = interior floor bottom-left; board origin = board bottom-left."]
    return "\n".join(L) + "\n"


def _diff_boards(a: dict, b: dict) -> list[str]:
    out = []
    for k, label in (("w", "width"), ("d", "depth"), ("t", "thickness"), ("max_component_h", "tallest component")):
        if a.get(k) != b.get(k):
            delta = (b.get(k) or 0) - (a.get(k) or 0)
            out.append(f"Board {label} {_fmt(a.get(k))} -> {_fmt(b.get(k))} mm ({delta:+g} mm).")
    ha = {h["id"]: h for h in a.get("holes", [])}
    hb = {h["id"]: h for h in b.get("holes", [])}
    for i in sorted(set(ha) | set(hb)):
        if i not in hb:
            out.append(f"Mounting hole {i} removed.")
        elif i not in ha:
            out.append(f"Mounting hole {i} added at ({_fmt(hb[i]['x'])}, {_fmt(hb[i]['y'])}).")
        elif (ha[i]["x"], ha[i]["y"], ha[i]["dia"]) != (hb[i]["x"], hb[i]["y"], hb[i]["dia"]):
            out.append(f"Mounting hole {i} moved ({_fmt(ha[i]['x'])}, {_fmt(ha[i]['y'])}) -> "
                       f"({_fmt(hb[i]['x'])}, {_fmt(hb[i]['y'])}).")
    ca = {c["id"]: c for c in a.get("connectors", [])}
    cb = {c["id"]: c for c in b.get("connectors", [])}
    for i in sorted(set(ca) | set(cb)):
        if i not in cb:
            out.append(f"Connector {i} ({ca[i]['type']}) removed from the {ca[i]['side']} edge.")
        elif i not in ca:
            out.append(f"Connector {i} ({cb[i]['type']}) added on the {cb[i]['side']} edge at {_fmt(cb[i]['pos'])} mm.")
        elif (ca[i]["side"], ca[i]["pos"]) != (cb[i]["side"], cb[i]["pos"]):
            d = cb[i]["pos"] - ca[i]["pos"] if ca[i]["side"] == cb[i]["side"] else None
            extra = f" ({d:+g} mm along the edge)" if d is not None else ""
            out.append(f"Connector {i} ({cb[i]['type']}) moved from {ca[i]['side']} {_fmt(ca[i]['pos'])} mm to "
                       f"{cb[i]['side']} {_fmt(cb[i]['pos'])} mm{extra}.")
    return out or ["No board geometry change detected."]


def _call_str(c: dict) -> str:
    args = ", ".join(f"{k}={_fmt(v)}" for k, v in (c.get("args") or {}).items())
    return f"{c.get('tool', '?')}({args})"


def write_change_record(board_a: dict, board_b: dict, calls: list, checks: list, model_name: str) -> str | None:
    """Write 'sensor-hub/rev-<b>-enclosure-change' page. Returns slug (None on failure). Never raises."""
    try:
        ra, rb = board_a.get("rev", "A"), board_b.get("rev", "B")
        slug = f"{PROJECT_SLUG}/rev-{str(rb).lower()}-enclosure-change"
        title = f"Sensor Hub Rev {rb} enclosure change"
        passed = bool(checks) and all(c.get("pass") for c in checks)
        enc_b = None
        try:  # optional: recompute resulting enclosure so openings/standoffs are searchable facts
            from rev import kernel
            enc_b, _ = kernel.apply_calls(board_b, kernel.enclosure_for(board_a), calls)
        except Exception:
            enc_b = None
        cb = {c["id"]: c for c in board_b.get("connectors", [])}
        L = [f"# {title}", "",
             f"Engineering change for [[{PROJECT_SLUG}]]: enclosure refit from board Rev {ra} to Rev {rb}. "
             f"Previous design: [[{PROJECT_SLUG}/rev-{str(ra).lower()}-enclosure]].", "",
             "| Field | Value |", "|---|---|",
             f"| Date | {time.strftime('%Y-%m-%d %H:%M')} |",
             f"| Performed by | REV agent (model: {model_name}) |",
             f"| Result | {'ALL CHECKS PASS' if passed else 'CHECKS FAILING'} |", "",
             "## What changed on the board", ""]
        L += [f"- {s}" for s in _diff_boards(board_a, board_b)]
        L += ["", "## Tool calls (in order)", ""]
        L += [f"{i + 1}. `{_call_str(c)}`" for i, c in enumerate(calls or [])] or ["(none)"]
        if enc_b:
            L += ["", "## Why each opening is where it is", ""]
            for o in enc_b.get("openings", []):
                c = cb.get(o["connector"], {})
                kind = "USB-C" if c.get("type") == "usb_c" else str(c.get("type", "?")).upper()
                L.append(f"- The {kind} opening for {o['connector']} is on the {o['side']} wall at {_fmt(o['pos'])} mm "
                         f"along the wall, {_fmt(o['z'])} mm above the floor, {_fmt(o['w'])} x {_fmt(o['h'])} mm, "
                         f"because connector {o['connector']} sits at {_fmt(c.get('pos'))} mm on the "
                         f"{c.get('side', '?')} edge of the Rev {rb} board.")
            cav = enc_b.get("cavity", {})
            L += [f"- Cavity is now {_fmt(cav.get('w'))} x {_fmt(cav.get('d'))} x {_fmt(cav.get('h'))} mm "
                  f"for the {_fmt(board_b.get('w'))} x {_fmt(board_b.get('d'))} mm Rev {rb} board.",
                  f"- Standoffs: " + ", ".join(f"{s['hole']} at ({_fmt(s['x'])}, {_fmt(s['y'])})"
                                               for s in enc_b.get("standoffs", [])) + "."]
        L += ["", "## Checks", "", "| Check | Result | Detail |", "|---|---|---|"]
        L += [f"| {c.get('label', c.get('id'))} | {'pass' if c.get('pass') else 'FAIL'} | "
              f"{str(c.get('detail', '')).replace('|', '/')} |" for c in checks or []]
        md = "\n".join(L) + "\n"
        if not put_page(slug, title, md):
            return None
        if enc_b is not None and passed:  # record the new current design as its own page
            put_page(f"{PROJECT_SLUG}/rev-{str(rb).lower()}-enclosure",
                     f"Sensor Hub Rev {rb} enclosure", design_markdown(board_b, enc_b))
        return slug
    except Exception as e:
        log.warning("write_change_record failed: %s", e)
        return None


# --------------------------------------------------------------------------- seeding
def seed() -> list[str]:
    """Import brain/*.md seed pages (project + Rev A design). Regenerates the design page from
    rev/data/demo.json + kernel when available so the numbers match the demo. Returns seeded slugs."""
    try:
        design_file = BRAIN_DIR / "sensor-hub" / "rev-a-enclosure.md"
        try:
            demo = json.loads((REPO / "rev" / "data" / "demo.json").read_text())
            board_a = demo.get("board_a") or demo.get("board")
            enc_a = demo.get("enclosure_a")
            if enc_a is None:
                from rev import kernel
                enc_a = kernel.enclosure_for(board_a)
            if board_a and enc_a:
                design_file.write_text(f'---\ntitle: "Sensor Hub Rev A enclosure"\ntype: concept\n---\n\n'
                                       + design_markdown(board_a, enc_a))
        except Exception as e:
            log.info("seed: using static design page (%s)", e)
        done = []
        for slug in (PROJECT_SLUG, DESIGN_A_SLUG):
            p = BRAIN_DIR / f"{slug}.md"
            md = p.read_text()
            if put_page(slug, _frontmatter_title(md) or slug, md):
                done.append(slug)
        return done
    except Exception as e:
        log.warning("seed failed: %s", e)
        return []


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "seed"
    if cmd == "seed":
        print("seeded:", seed())
    elif cmd == "list":
        print(json.dumps(list_pages(), indent=1))
    elif cmd == "get":
        print(get_page(sys.argv[2]))
    elif cmd == "search":
        print(json.dumps(search(" ".join(sys.argv[2:])), indent=1))
    print("timings:", TIMINGS)
