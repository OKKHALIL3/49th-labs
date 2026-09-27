"""Isolated River LoRA SFT + held-out eval for Qwen/Qwen3.5-397B-A17B-FP8.

Same pipeline as rev/train_river.py + rev/eval.py (same prompts, render, make_datum, grader), but every output
goes under rev/large/q397b/. BASE_MODEL is selected via RIVER_MODEL env in THIS process only.

    RIVER_MODEL=Qwen/Qwen3.5-397B-A17B-FP8 python3 -m rev.large.q397b.run train --epochs 4 --deadline 15:44
    RIVER_MODEL=Qwen/Qwen3.5-397B-A17B-FP8 python3 -m rev.large.q397b.run evalbase
"""
from __future__ import annotations

import os

os.environ["RIVER_MODEL"] = "Qwen/Qwen3.5-397B-A17B-FP8"

import argparse  # noqa: E402
import datetime as dt  # noqa: E402
import json  # noqa: E402
import random  # noqa: E402
import statistics  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import river_client as river  # noqa: E402

from rev import prompts, tasks  # noqa: E402
from rev import river_util as ru  # noqa: E402
from rev import eval as ev  # noqa: E402

OUT = Path(__file__).resolve().parent
# Safety: redirect any default-path writes in this process away from rev/data.
ru.CHECKPOINT_PATH = OUT / "checkpoint.json"
ru.CURRENT_CHECKPOINT_PATH = OUT / "checkpoint_current_unused.json"
ev.RESULTS_PATH = OUT / "eval_results_unused.json"

BASE = ru.BASE_MODEL
assert BASE == "Qwen/Qwen3.5-397B-A17B-FP8", BASE
LOG = OUT / "train_log.txt"
RESULTS = OUT / "results.json"
_lock = threading.Lock()


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with _lock, open(LOG, "a") as fh:
        fh.write(line + "\n")


def update_results(**kw) -> None:
    with _lock:
        try:
            data = json.loads(RESULTS.read_text())
        except Exception:
            data = {"base_model": BASE,
                    "tokenizer": "HF Qwen/Qwen3.5-397B-A17B-FP8 via river.load_tokenizer; renderer Qwen35VLRenderer thinking=False"}
        data.update(kw)
        data["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        tmp = RESULTS.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, RESULTS)


def summ(results: list[dict]) -> dict:
    return {
        "passed": sum(r["pass"] for r in results),
        "total": len(results),
        "by_check": {cid: sum(r["checks"].get(cid, False) for r in results) for cid in ev.CHECK_IDS},
        "parse_failures": sum(1 for r in results if r["parse_error"]),
        "sample_errors": sum(1 for r in results if r["sample_error"]),
        "mean_latency_s": round(sum(r["latency_s"] for r in results) / max(1, len(results)), 2),
    }


def do_eval(which: str, checkpoint=None) -> dict:
    rows = tasks.load("test")
    t0 = time.time()
    res = ev.run_eval(which, rows, workers=8, checkpoint=checkpoint)
    s = summ(res)
    s["elapsed_s"] = round(time.time() - t0, 1)
    (OUT / f"details_{which}.json").write_text(json.dumps(res, indent=1))
    log(f"eval {which}: {s['passed']}/{s['total']} by_check={s['by_check']} ({s['elapsed_s']}s)")
    return s


def parse_deadline(s: str) -> float:
    hh, mm = map(int, s.split(":"))
    return dt.datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp()


def train(a) -> None:
    deadline = parse_deadline(a.deadline)
    rows = tasks.load("train")
    t0 = time.time()
    data = [ru.make_datum(ru.render(prompts.messages(t)), prompts.completion(t)) for t in rows]
    lens = [len(d["input_ids"]) for d in data]
    log(f"=== run start base={BASE} n={len(data)} epochs={a.epochs} batch={a.batch} lr={a.lr} rank={a.rank} "
        f"tokens/datum mean={sum(lens)/len(lens):.0f} max={max(lens)} (tokenize {time.time()-t0:.1f}s) deadline={a.deadline}")
    rng = random.Random(a.seed)
    client = ru.get_client()
    step, losses, times = 0, [], []
    partial = False
    train_start = time.time()
    ep_done = 0
    with client.session(app="rev", run=a.name) as session:
        model = session.create_model(base_model=BASE, lora=river.LoraConfig(rank=a.rank, seed=a.seed))
        log(f"model created ({time.time()-train_start:.1f}s)")
        stop = False
        for ep in range(a.epochs):
            idx = list(range(len(data)))
            rng.shuffle(idx)
            batches = [idx[i:i + a.batch] for i in range(0, len(idx), a.batch)]
            for b in batches:
                est = statistics.median(times) if times else 0
                if time.time() + est > deadline:
                    log(f"deadline: stopping at epoch {ep} step {step} (median step {est:.1f}s)")
                    stop = partial = True
                    break
                ts = time.time()
                fb, opt = model.train_step([data[i] for i in b], lr=a.lr, loss_fn="cross_entropy", grad_clip_norm=1.0)
                loss = fb.metrics.get("loss_mean", fb.metrics.get("loss"))
                losses.append(loss)
                times.append(time.time() - ts)
                step += 1
                log(f"step {step} epoch {ep} loss {loss:.4f} grad_norm {opt.metrics.get('grad_norm')} sec {times[-1]:.1f}")
            if stop:
                break
            ep_done = ep + 1
        train_secs = round(time.time() - train_start, 1)
        ts = time.time()
        ckpt = model.save_weights(a.name, mode="inference")
        log(f"save_weights {ckpt.path} step {ckpt.step} ({time.time()-ts:.1f}s)")
        rec = ru.save_checkpoint_ref(
            ckpt, path=OUT / "checkpoint.json", name=a.name, train_steps=step, epochs_completed=ep_done,
            epochs_planned=a.epochs, batch=a.batch, lr=a.lr, rank=a.rank, n_train=len(data),
            final_loss=losses[-1] if losses else None, train_seconds=train_secs, partial=partial,
            saved_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        log(f"wrote {OUT/'checkpoint.json'}: {json.dumps(rec)}")
    epochs_frac = round(step * a.batch / len(data), 2)
    update_results(train={"steps": step, "epochs": epochs_frac, "epochs_completed": ep_done, "epochs_planned": a.epochs,
                          "seconds": train_secs, "final_loss": losses[-1] if losses else None,
                          "sec_per_step": round(sum(times) / len(times), 1) if times else None,
                          "median_sec_per_step": round(statistics.median(times), 1) if times else None,
                          "lr": a.lr, "rank": a.rank, "batch": a.batch, "checkpoint": ckpt.path},
                   partial=partial)
    ck = river.Checkpoint(path=ckpt.path, step=int(ckpt.step), checkpoint_type=ckpt.checkpoint_type)
    try:
        s = do_eval("tuned", ck)
        update_results(tuned=s)
    except Exception as e:
        log(f"tuned eval failed: {type(e).__name__}: {e}")
        update_results(tuned_error=f"{type(e).__name__}: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["train", "evalbase", "evaltuned"])
    ap.add_argument("--epochs", type=int, default=4)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--rank", type=int, default=32)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--deadline", default="15:44")
    ap.add_argument("--name", default="acme-rev-q397b")
    a = ap.parse_args()
    if a.mode == "evalbase":
        try:
            update_results(base=do_eval("base"))
        except Exception as e:
            log(f"base eval failed: {type(e).__name__}: {e}")
            update_results(base_error=f"{type(e).__name__}: {e}")
    elif a.mode == "evaltuned":
        ck = ru.load_checkpoint_ref(OUT / "checkpoint.json")
        update_results(tuned=do_eval("tuned", ck))
    else:
        train(a)


if __name__ == "__main__":
    sys.exit(main())
