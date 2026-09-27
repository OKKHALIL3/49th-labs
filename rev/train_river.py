"""River LoRA SFT: teach the base model Acme Devices' house engineering rules from past revisions.

Run from repo root:
    /opt/anaconda3/bin/python3 -m rev.train_river [--epochs 3] [--batch 16] [--lr 2e-4] [--deadline 15:35]
        [--extra N --extra-seed S]   # optionally add N freshly generated tasks (disjoint from test) to train.jsonl

Writes: rev/data/train_log.txt (step/loss/seconds), rev/data/checkpoint.json (river:// checkpoint reference).
Prompts never contain house values; the model only sees Rev A design + Rev B board + change summary, and learns the
values from gold tool calls (the company's revision history).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import time

import river_client as river

from rev import kernel, prompts, tasks
from rev import river_util as ru

LOG_PATH = ru.DATA_DIR / "train_log.txt"


def log(msg: str, fh=None) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    if fh:
        fh.write(line + "\n")
        fh.flush()


def build_rows(extra: int, extra_seed: int) -> list[dict]:
    rows = tasks.load("train")
    if extra:
        test = tasks.load("test")
        excl = {tasks._key(t) for t in rows + test}
        demo = tasks.load("demo")
        excl.add(tasks._key(demo))
        rows = rows + tasks.generate(extra, extra_seed, f"extra{extra_seed}", exclude=excl)
    return rows


def val_rows(n: int = 12, seed: int = 777) -> list[dict]:
    """Small validation set: freshly generated, disjoint from train/test (never the test set)."""
    excl = {tasks._key(t) for t in tasks.load("train") + tasks.load("test")}
    return tasks.generate(n, seed, "val", exclude=excl)


def val_pass(model, rows: list[dict]) -> tuple[int, int, list[str]]:
    ps = [ru.render(prompts.messages(t)) for t in rows]
    groups = model.sample(ps, max_tokens=512, temperature=0.0, stop=ru.STOP)
    ok, fails = 0, []
    for t, g in zip(rows, groups):
        text = g[0].text if g else ""
        calls = prompts.parse_calls(text) or []
        enc, _ = kernel.apply_calls(t["board_b"], t["enclosure_a"], calls)
        if kernel.passes(t["board_b"], enc):
            ok += 1
        elif len(fails) < 2:
            fails.append(text[:300])
    return ok, len(rows), fails


def parse_deadline(s: str | None) -> float | None:
    if not s:
        return None
    hh, mm = map(int, s.split(":"))
    now = dt.datetime.now()
    d = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return d.timestamp()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--rank", type=int, default=32)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--extra", type=int, default=0)
    ap.add_argument("--extra-seed", type=int, default=11)
    ap.add_argument("--deadline", default="15:35", help="local HH:MM wall clock; stop training steps after this")
    ap.add_argument("--name", default="acme-rev")
    ap.add_argument("--val-every-epoch", action="store_true")
    a = ap.parse_args()

    deadline = parse_deadline(a.deadline)
    rows = build_rows(a.extra, a.extra_seed)
    t0 = time.time()
    data = [ru.make_datum(ru.render(prompts.messages(t)), prompts.completion(t)) for t in rows]
    lens = [len(d["input_ids"]) for d in data]
    vrows = val_rows()

    fh = open(LOG_PATH, "a")
    log(f"=== run start base={ru.BASE_MODEL} n={len(data)} epochs={a.epochs} batch={a.batch} lr={a.lr} rank={a.rank} "
        f"tokens/datum mean={sum(lens)/len(lens):.0f} max={max(lens)} (tokenize {time.time()-t0:.1f}s)", fh)

    rng = random.Random(a.seed)
    client = ru.get_client()
    step = 0
    losses = []
    train_start = time.time()
    with client.session(app="rev", run=a.name) as session:
        model = session.create_model(base_model=ru.BASE_MODEL, lora=river.LoraConfig(rank=a.rank, seed=a.seed))
        log(f"model created ({time.time()-train_start:.1f}s)", fh)
        stop = False
        for ep in range(a.epochs):
            idx = list(range(len(data)))
            rng.shuffle(idx)
            batches = [idx[i:i + a.batch] for i in range(0, len(idx), a.batch)]
            for b in batches:
                if deadline and time.time() > deadline:
                    log(f"deadline reached; stopping at epoch {ep} step {step}", fh)
                    stop = True
                    break
                ts = time.time()
                fb, opt = model.train_step([data[i] for i in b], lr=a.lr, loss_fn="cross_entropy", grad_clip_norm=1.0)
                loss = fb.metrics.get("loss_mean", fb.metrics.get("loss"))
                losses.append(loss)
                step += 1
                log(f"step {step} epoch {ep} loss {loss:.4f} grad_norm {opt.metrics.get('grad_norm')} "
                    f"sec {time.time()-ts:.1f}", fh)
            if stop:
                break
            if a.val_every_epoch or ep == a.epochs - 1:
                ts = time.time()
                try:
                    ok, n, fails = val_pass(model, vrows)
                    log(f"epoch {ep} val {ok}/{n} ({time.time()-ts:.1f}s)" + (f" sample_fail={fails[0]!r}" if fails else ""), fh)
                except Exception as e:
                    log(f"val sample failed: {type(e).__name__}: {e}", fh)
        ts = time.time()
        ckpt = model.save_weights(a.name, mode="inference")
        log(f"save_weights {ckpt.path} step {ckpt.step} ({time.time()-ts:.1f}s)", fh)
        rec = ru.save_checkpoint_ref(
            ckpt, name=a.name, train_steps=step, epochs=a.epochs, batch=a.batch, lr=a.lr, rank=a.rank,
            n_train=len(data), final_loss=losses[-1] if losses else None,
            train_seconds=round(time.time() - train_start, 1), saved_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        )
        log(f"wrote {ru.CHECKPOINT_PATH}: {json.dumps(rec)}", fh)
    log(f"=== run done steps={step} final_loss={losses[-1] if losses else None} total {time.time()-train_start:.1f}s", fh)
    fh.close()


if __name__ == "__main__":
    main()
