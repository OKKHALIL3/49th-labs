import os, sys, json, time
os.environ["RIVER_MODEL"] = "Qwen/Qwen3.5-122B-A10B-FP8"  # this process only
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, ROOT)
from pathlib import Path
from rev import river_util as ru
assert ru.BASE_MODEL == "Qwen/Qwen3.5-122B-A10B-FP8", ru.BASE_MODEL
OUT = Path(ROOT) / "rev/large/q122b"
CHECK_IDS = ["cavity_fit", "cavity_height", "standoffs", "openings", "orphans"]

def summ(results):
    return {"passed": sum(r["pass"] for r in results), "total": len(results),
            "by_check": {c: sum(r["checks"].get(c, False) for r in results) for c in CHECK_IDS},
            "parse_failures": sum(1 for r in results if r["parse_error"]),
            "sample_errors": sum(1 for r in results if r["sample_error"]),
            "mean_latency_s": round(sum(r["latency_s"] for r in results) / max(1, len(results)), 2)}

def write_json(p, d):
    p = Path(p); tmp = p.with_suffix(".tmp"); tmp.write_text(json.dumps(d, indent=1)); os.replace(tmp, p)

def finalize(notes_extra=None):
    def ld(n):
        try: return json.loads((OUT / n).read_text())
        except Exception: return None
    b, t, tr = ld("eval_base.json"), ld("eval_tuned.json"), ld("train_summary.json")
    notes = []
    if tr and tr.get("error"): notes.append("train error: " + tr["error"])
    if b and b.get("error"): notes.append("base eval error: " + b["error"])
    if t and t.get("error"): notes.append("tuned eval error: " + t["error"])
    if notes_extra: notes.append(notes_extra)
    res = {"base_model": ru.BASE_MODEL,
           "base": b.get("summary") if b else None,
           "tuned": t.get("summary") if t else None,
           "train": {k: tr.get(k) for k in ("steps", "epochs", "planned_epochs", "seconds", "final_loss", "sec_per_step", "lr", "batch", "rank", "n_train", "checkpoint")} if tr else None,
           "partial": bool(tr.get("partial")) if tr else True,
           "tokenizer": "HF tokenizer via river.load_tokenizer(base_model='Qwen/Qwen3.5-122B-A10B-FP8') (downloaded fresh, not the 9B one); river renderer thinking=False",
           "test_set": "rev/data/test.jsonl (30 held-out), temperature 0, max_tokens 768, grader = rev.eval.grade (kernel checks)",
           "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "notes": "; ".join(notes) if notes else ""}
    write_json(OUT / "results.json", res)
    return res
