"""Train + eval Qwen/Qwen3.6-35B-A3B-FP8 with the exact rev.train_river / rev.eval pipeline.

Isolated: writes ONLY under rev/large/q35b/. Sets RIVER_MODEL for this process before importing river_util,
so ru.BASE_MODEL (tokenizer, renderer, sampling) points at the 35B model without touching shared files.

Run from repo root:
    /opt/anaconda3/bin/python3 -m rev.large.q35b.run --mode base      # base eval -> base_eval.json
    /opt/anaconda3/bin/python3 -m rev.large.q35b.run --mode train     # train, save, tuned eval -> results.json
    /opt/anaconda3/bin/python3 -m rev.large.q35b.run --mode finalize  # merge whatever exists -> results.json
"""
from __future__ import annotations

import os

BASE = "Qwen/Qwen3.6-35B-A3B-FP8"
os.environ["RIVER_MODEL"] = BASE  # this process only

import argparse  # noqa: E402
import datetime as dt  # noqa: E402
import json  # noqa: E402
import random  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
from pathlib import Path  # noqa: E402

import river_client as river  # noqa: E402

from rev import eval as ev  # noqa: E402
from rev import prompts, tasks  # noqa: E402
from rev import river_util as ru  # noqa: E402

assert ru.BASE_MODEL == BASE, ru.BASE_MODEL

OUT = Path(__file__).resolve().parent
LOG_PATH = OUT / "train_log.txt"
CKPT_PATH = OUT / "checkpoint.json"
BASE_EVAL = OUT / "base_eval.json"
TUNED_EVAL = OUT / "tuned_eval.json"
TRAIN_INFO = OUT / "train_info.json"
RESULTS = OUT / "results.json"


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as fh:
        fh.write(line + "\n")


def wjson(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    os.replace(tmp, path)


def rjson(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def deadline_ts(s: str) -> float:
    hh, mm = map(int, s.split(":"))
    return dt.datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp()


def tokenizer_desc() -> str:
    tok = ru.get_tokenizer()
    return f"{getattr(tok, 'name_or_path', '?')} (river.load_tokenizer for {BASE}); renderer {type(ru.get_renderer()).__name__}"


def eval_summary(which: str, results: list[dict], ckpt_path: str | None = None) -> dict:
    s = ev.summarize(which, results)
    s["checkpoint"] = ckpt_path if which == "tuned" else None
    s["label"] = f"{'Base' if which == 'base' else 'Acme-tuned'} {ev.short_model(BASE)}"
    return s


def mode_base(workers: int) -> None:
    rows = tasks.load("test")
    t0 = time.time()
    try:
        res = ev.run_eval("base", rows, workers)
        s = eval_summary("base", res)
        s["elapsed_s"] = round(time.time() - t0, 1)
        wjson(BASE_EVAL, {"summary": s, "details": res})
        print(json.dumps(s, indent=1))
    except Exception as e:
        wjson(BASE_EVAL, {"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-2000:]})
        raise
    finalize()


def mode_train(a) -> None:
    deadline = deadline_ts(a.deadline)
    rows = tasks.load("train")
    t0 = time.time()
    data = [ru.make_datum(ru.render(prompts.messages(t)), prompts.completion(t)) for t in rows]
    lens = [len(d["input_ids"]) for d in data]
    log(f"=== run start base={ru.BASE_MODEL} n={len(data)} max_epochs={a.epochs} batch={a.batch} lr={a.lr} "
        f"rank={a.rank} deadline={a.deadline} tokens/datum mean={sum(lens)/len(lens):.0f} max={max(lens)} "
        f"(tokenize {time.time()-t0:.1f}s) tokenizer={tokenizer_desc()}")
    rng = random.Random(a.seed)
    client = ru.get_client()
    step, losses, step_secs = 0, [], []
    epochs_done, partial, err = 0, False, None
    train_start = time.time()
    info = {"steps": 0, "epochs": 0, "seconds": None, "final_loss": None, "sec_per_step": None}
    ckpt = None
    with client.session(app="rev", run=a.name) as session:
        try:
            model = session.create_model(base_model=ru.BASE_MODEL, lora=river.LoraConfig(rank=a.rank, seed=a.seed))
            log(f"model created LoRA rank {a.rank} ({time.time()-train_start:.1f}s)")
            n_batches = (len(data) + a.batch - 1) // a.batch
            for ep in range(a.epochs):
                if ep >= 2 and step_secs:  # after >=2 full epochs, only start another if it fits
                    est = n_batches * (sum(step_secs) / len(step_secs))
                    if time.time() + est > deadline:
                        log(f"skip epoch {ep}: est {est:.0f}s > {deadline - time.time():.0f}s left")
                        break
                idx = list(range(len(data)))
                rng.shuffle(idx)
                batches = [idx[i:i + a.batch] for i in range(0, len(idx), a.batch)]
                stop = False
                for bi, b in enumerate(batches):
                    if time.time() > deadline:
                        log(f"deadline reached; stopping at epoch {ep} batch {bi} step {step}")
                        partial = True
                        stop = True
                        break
                    ts = time.time()
                    fb, opt = model.train_step([data[i] for i in b], lr=a.lr, loss_fn="cross_entropy",
                                               grad_clip_norm=1.0)
                    loss = fb.metrics.get("loss_mean", fb.metrics.get("loss"))
                    losses.append(loss)
                    step += 1
                    step_secs.append(time.time() - ts)
                    log(f"step {step} epoch {ep} loss {loss:.4f} grad_norm {opt.metrics.get('grad_norm')} "
                        f"sec {step_secs[-1]:.1f}")
                    if step == 2:
                        sps = sum(step_secs) / 2
                        log(f"first 2 steps mean {sps:.1f}s/step -> est epoch {n_batches*sps:.0f}s, "
                            f"{deadline - time.time():.0f}s left to deadline")
                if stop:
                    break
                epochs_done = ep + 1
        except Exception as e:
            err = f"train: {type(e).__name__}: {e}"
            log(f"TRAIN ERROR {err}")
            partial = True
        if step > 0:
            try:
                ts = time.time()
                ckpt = model.save_weights(a.name, mode="inference")
                log(f"save_weights {ckpt.path} step {ckpt.step} ({time.time()-ts:.1f}s)")
            except Exception as e:
                err = (err + "; " if err else "") + f"save_weights: {type(e).__name__}: {e}"
                log(f"SAVE ERROR {err}")
    secs = round(time.time() - train_start, 1)
    info = {"steps": step, "epochs": epochs_done if not partial else round(step / ((len(data) + a.batch - 1) // a.batch), 2),
            "full_epochs_completed": epochs_done, "seconds": secs,
            "final_loss": losses[-1] if losses else None,
            "sec_per_step": round(sum(step_secs) / len(step_secs), 1) if step_secs else None,
            "batch": a.batch, "lr": a.lr, "rank": a.rank, "n_train": len(data), "partial": partial, "error": err,
            "losses": [round(x, 4) for x in losses]}
    wjson(TRAIN_INFO, info)
    if ckpt is not None:
        rec = ru.save_checkpoint_ref(ckpt, path=CKPT_PATH, name=a.name, train_steps=step, epochs=info["epochs"],
                                     batch=a.batch, lr=a.lr, rank=a.rank, n_train=len(data),
                                     final_loss=info["final_loss"], train_seconds=secs, partial=partial,
                                     saved_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        log(f"wrote {CKPT_PATH}: {json.dumps(rec)}")
    log(f"=== run done steps={step} final_loss={info['final_loss']} total {secs}s")
    finalize()
    if ckpt is not None:
        eval_tuned(a.workers)


def eval_tuned(workers: int) -> None:
    ckpt = ru.load_checkpoint_ref(CKPT_PATH)
    rows = tasks.load("test")
    t0 = time.time()
    try:
        res = ev.run_eval("tuned", rows, workers, checkpoint=ckpt)
        s = eval_summary("tuned", res, ckpt.path)
        s["elapsed_s"] = round(time.time() - t0, 1)
        wjson(TUNED_EVAL, {"summary": s, "details": res})
        log(f"tuned eval {s['passed']}/{s['total']} by_check {s['by_check']} ({s['elapsed_s']}s)")
    except Exception as e:
        wjson(TUNED_EVAL, {"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-2000:]})
        log(f"TUNED EVAL ERROR {type(e).__name__}: {e}")
    finalize()


def _pick(ev_json):
    if not ev_json:
        return None
    if "summary" not in ev_json:
        return {"error": ev_json.get("error")}
    s = ev_json["summary"]
    return {k: s[k] for k in ("passed", "total", "by_check", "parse_failures", "sample_errors", "mean_latency_s",
                              "checkpoint", "evaluated_at") if k in s}


def finalize() -> None:
    b, t, info, ck = rjson(BASE_EVAL), rjson(TUNED_EVAL), rjson(TRAIN_INFO), rjson(CKPT_PATH)
    notes = []
    if info and info.get("error"):
        notes.append(info["error"])
    if info and info.get("partial"):
        notes.append("training stopped early at deadline/error; eval uses the partial checkpoint")
    if b is None:
        notes.append("base eval not finished")
    if t is None:
        notes.append("tuned eval not finished")
    out = {
        "base_model": BASE,
        "base": _pick(b),
        "tuned": _pick(t),
        "train": {k: info.get(k) for k in ("steps", "epochs", "full_epochs_completed", "seconds", "final_loss",
                                            "sec_per_step", "batch", "lr", "rank", "n_train")} if info else None,
        "checkpoint": ck,
        "partial": bool(info and info.get("partial")),
        "tokenizer": tokenizer_desc(),
        "test_set": "rev/data/test.jsonl (30 held-out tasks); grader = rev.eval.grade (kernel checker), temp 0, "
                    f"max_tokens {ev.MAX_TOKENS}",
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "notes": "; ".join(notes) if notes else "",
    }
    wjson(RESULTS, out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["base", "train", "tuned", "finalize"], required=True)
    ap.add_argument("--epochs", type=int, default=4)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--rank", type=int, default=32)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--deadline", default="15:44")
    ap.add_argument("--name", default="acme-rev-q35b")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    if a.mode == "base":
        mode_base(a.workers)
    elif a.mode == "train":
        mode_train(a)
    elif a.mode == "tuned":
        eval_tuned(a.workers)
    else:
        finalize()


if __name__ == "__main__":
    main()
