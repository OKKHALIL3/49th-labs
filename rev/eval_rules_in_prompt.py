"""Baseline: the same untrained model, with EVERY house rule written into its prompt.

Answers "why not just give the model the rules?" on the same 30 held-out tasks, same grader.
Usage: python3 -m rev.eval_rules_in_prompt [--checkpoint company]  -> rev/data/eval_rules_in_prompt.json
"""
import json, sys, time
from concurrent.futures import ThreadPoolExecutor
from rev import prompts, river_util as ru, eval as ev, kernel

RULES = (
    "\n\nACME HOUSE RULES (follow exactly):\n"
    f"- Board-to-wall clearance: {kernel.HOUSE['clearance']} mm on every side (fit_cavity clearance).\n"
    f"- Headroom above the tallest component: {kernel.HOUSE['headroom']} mm (fit_cavity headroom).\n"
    "- One standoff on every mounting hole; remove standoffs for removed holes.\n"
    "- Connector opening tolerance per side, by connector type: "
    + ", ".join(f"{t} {v} mm" for t, v in kernel.HOUSE["tolerance"].items() if t != "usb_a") + ".\n"
    "- Remove openings for removed connectors.\n")


def run(task):
    msgs = prompts.messages(task)
    msgs = [dict(m) for m in msgs]
    msgs[0]["content"] = msgs[0]["content"] + RULES
    t0 = time.time()
    text = ru.sample_base(ru.render(msgs), temperature=0.0, max_tokens=768)
    g = ev.grade(task, text)
    return {"id": task["id"], "pass": bool(g.get("pass")), "failed": g.get("failed"), "latency_s": round(time.time() - t0, 1), "raw": text[:1500]}


if __name__ == "__main__":
    rows = [json.loads(l) for l in open("rev/data/test.jsonl")]
    ru.get_tokenizer()
    t0 = time.time()
    with ThreadPoolExecutor(12) as ex:
        res = list(ex.map(run, rows))
    n = sum(r["pass"] for r in res)
    rule_tokens = len(ru.get_tokenizer().encode(RULES, add_special_tokens=False))
    out = {"label": "Untrained Qwen3.5-9B + all house rules written in the prompt", "passed": n, "total": len(res),
           "rule_tokens": rule_tokens, "seconds": round(time.time() - t0, 1), "cases": res,
           "note": "usb_a has no written rule (it is new knowledge); every other rule is spelled out in the system prompt"}
    json.dump(out, open("rev/data/eval_rules_in_prompt.json", "w"), indent=1)
    print(out["label"], f"{n}/{len(res)}", "| rule tokens:", rule_tokens, "|", out["seconds"], "s")
    from collections import Counter
    print("failed checks:", Counter(c for r in res if not r["pass"] for c in (r["failed"] or [])))
