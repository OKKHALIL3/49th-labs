"""GBrain -> training data. GBrain is the system of record; the model is distilled from what is in it.

  build_dataset_from_gbrain(kind="captures"|"history") -> verified records pulled back OUT of GBrain
  derive_lessons(captures)        -> [{"type","tolerance","evidence":[capture ids],"n"}]  (consistent only)
  derive_lessons_report(captures) -> {"lessons": [...], "conflicts": [...], "unverified": [ids], "observations": int}
  learn_from_gbrain(emit)         -> pulls verified captures, derives the lesson, runs rev.learn.learn()

Capture page (written by the CAD watcher; `capture_markdown()` / `write_capture()` build one):
  slug "captures/<id>", body contains a fenced ```json block with the record:
  {"kind":"capture","id":"cap-...","by":"senior-engineer","file":"...","product":"...","calls":[...],
   "observations":[{"connector":"J5","type":"usb_a","tolerance":0.5}],"verified":true,"checks":[...]}
  Tolerances may instead come from top-level {"type","tolerance"}, a "lesson"/"rule" dict, or place_opening
  calls + a "board" with connectors (id -> type).

CLI:  python3 -m rev.gbrain_dataset captures|history|lessons   ·   python3 -m rev.gbrain_dataset learn [--cached]
"""
from __future__ import annotations

import json
import re
import shutil
import statistics
import tempfile
import time
from pathlib import Path

from rev import gbrain_io as g

PREFIX = {"captures": "captures/", "history": "history/"}
AGREE_MM = 0.05          # captures agree if all tolerances for a type are within this band
ROLE = "senior-engineer"
_JSON_BLOCK = re.compile(r"```json\s*\n(.*?)\n```", re.S)


# --------------------------------------------------------------------------- page <-> record
def parse_record(md: str, slug: str | None = None) -> dict | None:
    """Pull the machine-readable record out of a GBrain page (last fenced json dict wins; 'kind' preferred)."""
    best = None
    for block in _JSON_BLOCK.findall(md or ""):
        try:
            v = json.loads(block)
        except ValueError:
            continue
        if isinstance(v, dict) and (best is None or "kind" in v or "verified" in v):
            best = v
    if best is None:
        return None
    rec = dict(best)
    if slug:
        rec.setdefault("slug", slug)
        rec.setdefault("id", slug.rsplit("/", 1)[-1])
    if "verified" not in rec:
        rec["verified"] = bool(re.search(r"^\s*verified:\s*(yes|true)\b", md or "", re.I | re.M))
    elif isinstance(rec["verified"], str):
        rec["verified"] = rec["verified"].strip().lower() in ("yes", "true", "1", "pass")
    return rec


def capture_markdown(rec: dict) -> str:
    """Markdown page for one captured engineering change (helper for the CAD watcher)."""
    rec = dict(rec, kind="capture", by=rec.get("by") or ROLE)
    obs = observations(rec)
    rule = "; ".join(f"{o['type']} openings get {o['tolerance']:g} mm per side" for o in obs) or "no opening rule inferred"
    title = rec.get("title") or f"Captured change — {rec.get('product') or rec.get('file') or rec.get('id')}"
    L = [f"# {title}", "",
         "Engineering change captured from a CAD save on an engineer's workstation.", "",
         "| Field | Value |", "|---|---|",
         f"| Capture | {rec.get('id')} |",
         f"| Changed by | {rec['by']} |",
         f"| Design file | {rec.get('file', '-')} |",
         f"| Inferred rule | {rule} |",
         f"| Verified by checker | {'yes' if rec.get('verified') else 'no'} |", "",
         "## What changed", "", rec.get("summary") or rec.get("change_summary") or "-", "",
         "## Engineering calls", "", "```json", json.dumps(rec.get("calls") or []), "```", "",
         "## Record", "", "```json", json.dumps(rec), "```", "",
         f"verified: {'yes' if rec.get('verified') else 'no'}", ""]
    return "\n".join(L)


def write_capture(rec: dict) -> str | None:
    """Write a capture record to GBrain as captures/<id>. Returns slug or None. Never raises."""
    try:
        cid = re.sub(r"[^a-z0-9\-]+", "-", str(rec.get("id") or f"cap-{int(time.time())}").lower()).strip("-")
        rec = dict(rec, id=cid)
        slug = PREFIX["captures"] + cid
        title = rec.get("title") or f"Captured change {cid}"
        return slug if g.put_page(slug, title, capture_markdown(rec)) else None
    except Exception as e:  # noqa: BLE001
        g.log.warning("write_capture failed: %s", e)
        return None


# --------------------------------------------------------------------------- pull records out of GBrain
def _export(prefix: str) -> list[tuple[str, str]]:
    """[(slug, markdown)] for every page under `prefix` via one `gbrain export --slug-prefix` call."""
    if not g.ensure_brain():
        return []
    root = g.GBRAIN_HOME / "staging"
    root.mkdir(parents=True, exist_ok=True)
    out = Path(tempfile.mkdtemp(dir=root, prefix="export-"))
    try:
        rc, _ = g._run(["export", "--dir", str(out), "--slug-prefix", prefix], timeout=120)
        if rc != 0:
            return []
        pages = []
        for f in sorted(out.rglob("*.md")):
            slug = str(f.relative_to(out))[:-3]
            pages.append((slug, f.read_text()))
        return pages
    finally:
        shutil.rmtree(out, ignore_errors=True)


def _pages_via_get(prefix: str) -> list[tuple[str, str]]:
    """Fallback: list + get each page (slower, ~0.7 s/page)."""
    rc, out = g._run(["list", "--limit", "5000", "--sort", "slug"], timeout=60)
    slugs = [ln.split("\t")[0] for ln in out.splitlines() if "\t" in ln and ln.startswith(prefix)] if rc == 0 else []
    return [(s, md) for s in slugs if (md := g.get_page(s))]


def fetch_pages(kind: str = "captures") -> list[tuple[str, str]]:
    prefix = PREFIX.get(kind, kind)
    try:
        pages = _export(prefix)
        return pages if pages else _pages_via_get(prefix)
    except Exception as e:  # noqa: BLE001
        g.log.warning("fetch_pages(%s) failed: %s", kind, e)
        return []


def build_dataset_from_gbrain(kind: str = "captures", include_unverified: bool = False) -> list[dict]:
    """Records pulled back out of GBrain, parsed from each page's fenced json. Verified only by default."""
    recs = []
    for slug, md in fetch_pages(kind):
        r = parse_record(md, slug)
        if r is not None and (include_unverified or r.get("verified")):
            recs.append(r)
    return recs


# --------------------------------------------------------------------------- lessons
def _num(v):
    try:
        return round(float(v), 3)
    except (TypeError, ValueError):
        return None


def observations(rec: dict) -> list[dict]:
    """[{"type","tolerance","connector"?}] the capture demonstrates."""
    out = []
    for o in rec.get("observations") or []:
        if isinstance(o, dict) and o.get("type") and _num(o.get("tolerance")) is not None:
            out.append({"type": o["type"], "tolerance": _num(o["tolerance"]), "connector": o.get("connector")})
    if out:
        return out
    for key in ("lesson", "rule", "inferred_rule"):
        v = rec.get(key)
        if isinstance(v, dict) and v.get("type") and _num(v.get("tolerance")) is not None:
            return [{"type": v["type"], "tolerance": _num(v["tolerance"]), "connector": v.get("connector")}]
    if rec.get("type") and _num(rec.get("tolerance")) is not None:
        return [{"type": rec["type"], "tolerance": _num(rec["tolerance"]), "connector": rec.get("connector")}]
    board = rec.get("board") or rec.get("board_b") or {}
    types = {c.get("id"): c.get("type") for c in board.get("connectors", []) if isinstance(c, dict)}
    for c in rec.get("calls") or []:
        if not isinstance(c, dict) or c.get("tool") != "place_opening":
            continue
        a = c.get("args") or {}
        t = a.get("type") or types.get(a.get("connector"))
        if t and _num(a.get("tolerance")) is not None:
            out.append({"type": t, "tolerance": _num(a["tolerance"]), "connector": a.get("connector")})
    return out


def derive_lessons_report(captures: list[dict]) -> dict:
    groups: dict[str, list[tuple[str, float]]] = {}
    unverified, n_obs = [], 0
    for rec in captures:
        cid = str(rec.get("id") or rec.get("slug") or "?")
        if not rec.get("verified"):
            unverified.append(cid)
            continue
        for o in observations(rec):
            groups.setdefault(o["type"], []).append((cid, o["tolerance"]))
            n_obs += 1
    lessons, conflicts = [], []
    for ctype, obs in sorted(groups.items()):
        tols = [t for _, t in obs]
        ids = list(dict.fromkeys(cid for cid, _ in obs))
        if max(tols) - min(tols) <= AGREE_MM + 1e-9:
            lessons.append({"type": ctype, "tolerance": round(statistics.median(tols), 2), "evidence": ids, "n": len(ids)})
        else:
            conflicts.append({"type": ctype, "tolerances": sorted(set(tols)), "evidence": ids, "n": len(ids),
                              "reason": f"captures disagree ({min(tols):g}-{max(tols):g} mm, > {AGREE_MM} mm apart); not learned"})
    return {"lessons": lessons, "conflicts": conflicts, "unverified": unverified, "observations": n_obs}


def derive_lessons(captures: list[dict]) -> list[dict]:
    """Consistent lessons only; conflicting types are flagged in derive_lessons_report() and never learned."""
    return derive_lessons_report(captures)["lessons"]


def pick_lesson(lessons: list[dict], ctype: str | None = None) -> dict | None:
    """Prefer a connector type the company history never covered (new knowledge), then most evidence."""
    if ctype:
        return next((l for l in lessons if l["type"] == ctype), None)
    try:
        from rev import tasks
        known = set(tasks.TYPES)
    except Exception:  # noqa: BLE001
        known = set()
    return max(lessons, key=lambda l: (l["type"] not in known, l["n"]), default=None)


# --------------------------------------------------------------------------- learn
def _data_dir() -> Path:
    return Path(__file__).resolve().parent / "data"


LAST_CAPTURE_RUN = _data_dir() / "capture_learn_last.json"


def _write_lesson_page(lesson: dict, result: dict):
    try:
        slug = f"lessons/{lesson['type']}-opening-tolerance"
        md = "\n".join([
            f"# Lesson: {lesson['type']} opening tolerance {lesson['tolerance']:g} mm per side", "",
            "Learned from verified engineering changes captured on engineers' workstations (not typed as a rule).", "",
            "| Field | Value |", "|---|---|",
            f"| Connector type | {lesson['type']} |",
            f"| Tolerance per side | {lesson['tolerance']:g} mm |",
            f"| Evidence | {', '.join(f'[[captures/{e}]]' for e in lesson['evidence'])} |",
            f"| Held-out before -> after | {(result.get('before') or {}).get('passed')}/{(result.get('before') or {}).get('total')}"
            f" -> {(result.get('after') or {}).get('passed')}/{(result.get('after') or {}).get('total')} |",
            f"| Checkpoint | {result.get('checkpoint') or '-'} |",
            f"| Learned at | {time.strftime('%Y-%m-%d %H:%M')} |", "",
            "```json", json.dumps({"kind": "lesson", **lesson, "verified": True}), "```", ""])
        g.put_page(slug, f"Lesson: {lesson['type']} opening tolerance", md)
    except Exception as e:  # noqa: BLE001
        g.log.warning("lesson page failed: %s", e)


def _replay(emit, path: Path, promote: bool, speed: float = 4.0) -> dict:
    """Stage fallback: replay a recorded real learn run with compressed timing."""
    from rev import learn
    rec = json.loads(path.read_text())
    prev, last = 0.0, {}
    for e in rec.get("events", []):
        if e.get("stage") == "gbrain":
            continue  # the gbrain event of this run was already emitted fresh
        t = float(e.get("t", prev) or prev)
        time.sleep(max(0.0, min(3.0, (t - prev) / speed)))
        prev = t
        e = dict(e, replay=True)
        if e.get("type") == "learned" and promote and e.get("ok"):
            learn.promote_checkpoint(learn.CKPT_V2_PATH)
            e["promoted"] = True
        emit(e)
        last = e
    return last


def learn_from_gbrain(emit=None, promote: bool = True, steps: int | None = None, ctype: str | None = None,
                      cached: bool = False, dry_run: bool = False) -> dict:
    """Pull verified captures from GBrain -> derive lesson -> rev.learn.learn(). Returns the 'learned' event."""
    events = []

    def out(e):
        events.append(e)
        (emit or (lambda ev: print(json.dumps(ev), flush=True)))(e)

    t0 = time.time()

    def ev(msg, **extra):
        out({"type": "learn", "stage": "gbrain", "step": 0, "total_steps": 0, "loss": None, "msg": msg,
             "t": round(time.time() - t0, 1), **extra})

    captures = build_dataset_from_gbrain("captures")
    rep = derive_lessons_report(captures)
    lesson = pick_lesson(rep["lessons"], ctype)
    if not lesson:
        ev(f"pulled {len(captures)} verified captures from GBrain → no consistent lesson to learn",
           conflicts=rep["conflicts"])
        res = {"type": "learned", "ok": False, "error": "no consistent verified captures in GBrain",
               "seconds": round(time.time() - t0, 1)}
        out(res)
        return res
    ev(f"pulled {len(captures)} verified captures from GBrain → lesson {lesson['type']} {lesson['tolerance']:g} mm",
       lesson=lesson, captures=len(captures), evidence=lesson["evidence"], conflicts=rep["conflicts"])
    for c in rep["conflicts"]:
        ev(f"flagged: {c['type']} captures disagree ({', '.join(f'{t:g}' for t in c['tolerances'])} mm) — not learned",
           conflict=c)
    if dry_run:
        return {"type": "learned", "ok": True, "dry_run": True, "lesson": lesson, "report": rep}

    from rev import learn
    replay_src = LAST_CAPTURE_RUN if LAST_CAPTURE_RUN.exists() else learn.DATA_DIR / "learn_last.json"
    if cached and replay_src.exists():
        res = _replay(out, replay_src, promote)
    else:
        res = learn.learn({"type": lesson["type"], "tolerance": lesson["tolerance"]}, emit=out,
                          promote=promote, steps=steps)
        if res.get("ok"):
            try:
                LAST_CAPTURE_RUN.write_text(json.dumps({"lesson": lesson, "events": events}, indent=1, default=str))
            except OSError:
                pass
    if res.get("ok"):
        _write_lesson_page(lesson, res)
    return res


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "lessons"
    if cmd in ("captures", "history"):
        recs = build_dataset_from_gbrain(cmd)
        print(f"{len(recs)} verified {cmd} records")
        print(json.dumps(recs[:2], indent=1)[:2000])
    elif cmd == "lessons":
        print(json.dumps(derive_lessons_report(build_dataset_from_gbrain("captures")), indent=1))
    elif cmd == "learn":
        r = learn_from_gbrain(cached="--cached" in sys.argv, dry_run="--dry-run" in sys.argv)
        print(json.dumps({k: v for k, v in r.items() if k != "events"}, indent=1, default=str))
