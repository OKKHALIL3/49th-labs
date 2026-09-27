"""Import the company's engineering history into GBrain as ECO (engineering change order) records.

GBrain is the system of record the company model is distilled from: one page per past revision in
rev/data/train.jsonl, slug "history/eco-<n>", with the change summary, the engineering calls that were
made (fenced json) and "verified: yes" (every historical change passes the deterministic checker).

    python3 -m rev.gbrain_history            # import all 200 (skips ones already in GBrain)
    python3 -m rev.gbrain_history --limit 50 # subset
    python3 -m rev.gbrain_history --count    # just count history/ + captures/ pages

Bulk path: write markdown files into one staging dir, then ONE `gbrain import <dir> --no-embed`.
Idempotent: slugs already present (gbrain list) are skipped; import itself also skips unchanged files.
"""
from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import time
from pathlib import Path

from rev import gbrain_io as g

REPO = Path(__file__).resolve().parent.parent
TRAIN_PATH = REPO / "rev" / "data" / "train.jsonl"
HISTORY_PREFIX = "history/"
CAPTURE_PREFIX = "captures/"
IMPORT_TIMEOUT = 300
ROLE = "senior-engineer"


def eco_slug(task_id: str) -> str:
    return f"{HISTORY_PREFIX}eco-{str(task_id).replace('train-', '')}"


def _board_label(t: dict) -> str:
    a, b = t.get("board_a", {}), t.get("board_b", {})
    return f"{b.get('name') or a.get('name') or 'Board'} Rev {a.get('rev', 'A')}→{b.get('rev', 'B')}"


def eco_title(t: dict) -> str:
    return f"{_board_label(t)} — {t.get('change_summary', '').strip()}"


def eco_record(t: dict) -> dict:
    """The machine-readable part of an ECO page (round-trips through GBrain as a fenced json block)."""
    return {"kind": "eco", "id": t["id"], "product": t.get("board_b", {}).get("name"),
            "rev_from": t.get("board_a", {}).get("rev", "A"), "rev_to": t.get("board_b", {}).get("rev", "B"),
            "by": ROLE, "change_summary": t.get("change_summary", ""), "calls": t.get("gold_calls", []),
            "verified": True}


def eco_markdown(t: dict) -> str:
    title = eco_title(t).replace('"', "'")
    rec = eco_record(t)
    calls = t.get("gold_calls", [])
    L = ["---", f'title: "{title}"', "type: concept", "tags: [eco, history]", "---", "",
         f"# {title}", "",
         "Engineering change order from the Acme Devices revision history.", "",
         "| Field | Value |", "|---|---|",
         f"| ECO | {t['id']} |",
         f"| Product | {rec['product']} |",
         f"| Board revision | {rec['rev_from']} -> {rec['rev_to']} |",
         f"| Changed by | {ROLE} |",
         f"| Engineering actions | {len(calls)} |",
         "| Verified by checker | yes |", "",
         "## Change summary", "", t.get("change_summary", ""), "",
         "## Engineering calls", "",
         "```json", json.dumps(calls), "```", "",
         "## Record", "",
         "```json", json.dumps(rec), "```", "",
         "verified: yes", ""]
    return "\n".join(L)


def existing_slugs(prefix: str = "", limit: int = 5000) -> set[str]:
    """Slugs currently in GBrain (optionally only those under `prefix`). Never raises."""
    try:
        if not g.ensure_brain():
            return set()
        rc, out = g._run(["list", "--limit", str(limit), "--sort", "slug"], timeout=60)
        if rc != 0:
            return set()
        return {ln.split("\t")[0] for ln in out.splitlines() if "\t" in ln and ln.split("\t")[0].startswith(prefix)}
    except Exception as e:  # noqa: BLE001
        g.log.warning("existing_slugs failed: %s", e)
        return set()


def load_history(limit: int | None = None) -> list[dict]:
    rows = [json.loads(ln) for ln in TRAIN_PATH.read_text().splitlines() if ln.strip()]
    return rows[:limit] if limit else rows


def bulk_import(pages: list[tuple[str, str]], timeout: int = IMPORT_TIMEOUT) -> tuple[bool, str]:
    """pages = [(slug, full markdown incl. frontmatter)] -> one `gbrain import` call. Returns (ok, output tail)."""
    if not pages:
        return True, "nothing to import"
    if not g.ensure_brain():
        return False, "gbrain unavailable"
    stage_root = g.GBRAIN_HOME / "staging"
    stage_root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(dir=stage_root))
    try:
        for slug, md in pages:
            f = stage / f"{slug}.md"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(md)
        rc, out = g._run(["import", str(stage), "--no-embed"], timeout=timeout)
        return rc == 0 and "Import complete" in out, out.strip()[-400:]
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def import_history(limit: int | None = None, force: bool = False) -> dict:
    """Import ECO pages for the company history. Returns {"imported","skipped","total","seconds","ok"}."""
    t0 = time.time()
    rows = load_history(limit)
    have = set() if force else existing_slugs(HISTORY_PREFIX)
    todo = [(eco_slug(t["id"]), eco_markdown(t)) for t in rows if force or eco_slug(t["id"]) not in have]
    ok, tail = bulk_import(todo)
    after = existing_slugs(HISTORY_PREFIX)
    return {"ok": ok, "imported": len(todo), "skipped": len(rows) - len(todo), "total": len(rows),
            "history_records": len(after), "seconds": round(time.time() - t0, 1), "gbrain": tail}


def stats() -> dict:
    slugs = existing_slugs()
    return {"history_records": sum(s.startswith(HISTORY_PREFIX) for s in slugs),
            "capture_records": sum(s.startswith(CAPTURE_PREFIX) for s in slugs)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true", help="re-import even if the slug exists")
    ap.add_argument("--count", action="store_true")
    a = ap.parse_args()
    if a.count:
        print(json.dumps(stats()))
        return
    print(json.dumps(import_history(a.limit, a.force), indent=1))


if __name__ == "__main__":
    main()
