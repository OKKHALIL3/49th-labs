"""LoRA SFT of Qwen3.5-122B-A10B-FP8 with the exact rev.train_river pipeline; deadline-bounded."""
import sys, time, json, random, math, datetime as dt, traceback
from common import ru, OUT, write_json, finalize
import river_client as river
from rev import prompts, tasks
LOG = OUT / "train_log.txt"
fh = open(LOG, "a")
def log(m):
    line = f"{time.strftime('%H:%M:%S')} {m}"; print(line, flush=True); fh.write(line + "\n"); fh.flush()
def at(hhmm):
    h, m = map(int, hhmm.split(":")); return dt.datetime.now().replace(hour=h, minute=m, second=0, microsecond=0).timestamp()
DEADLINE = at(sys.argv[1] if len(sys.argv) > 1 else "15:43")
LR, BATCH, RANK, SEED, MAXEP = 2e-4, 16, 32, 0, 4
rows = tasks.load("train")
data = [ru.make_datum(ru.render(prompts.messages(t)), prompts.completion(t)) for t in rows]
lens = [len(d["input_ids"]) for d in data]
log(f"=== run start base={ru.BASE_MODEL} n={len(data)} batch={BATCH} lr={LR} rank={RANK} tok mean={sum(lens)/len(lens):.0f} max={max(lens)} deadline={time.strftime('%H:%M', time.localtime(DEADLINE))}")
summary = {"lr": LR, "batch": BATCH, "rank": RANK, "n_train": len(data), "steps": 0, "epochs": 0, "partial": True}
rng = random.Random(SEED)
step, losses, durs = 0, [], []
t_start = time.time()
planned = MAXEP
steps_per_ep = math.ceil(len(data) / BATCH)
try:
    client = ru.get_client()
    with client.session(app="rev", run="q122b-acme-rev") as session:
        model = session.create_model(base_model=ru.BASE_MODEL, lora=river.LoraConfig(rank=RANK, seed=SEED))
        log(f"model created ({time.time()-t_start:.1f}s)")
        stop = False; ep_done = 0.0
        for ep in range(MAXEP):
            if ep >= planned: break
            idx = list(range(len(data))); rng.shuffle(idx)
            batches = [idx[i:i + BATCH] for i in range(0, len(idx), BATCH)]
            for bi, b in enumerate(batches):
                est = (sorted(durs)[len(durs)//2] if durs else 0)
                if time.time() + est > DEADLINE:
                    log(f"deadline: stopping at epoch {ep} batch {bi} step {step}"); stop = True; break
                ts = time.time()
                fb, opt = model.train_step([data[i] for i in b], lr=LR, loss_fn="cross_entropy", grad_clip_norm=1.0)
                d = time.time() - ts; durs.append(d)
                loss = fb.metrics.get("loss_mean", fb.metrics.get("loss")); losses.append(loss); step += 1
                ep_done = ep + (bi + 1) / len(batches)
                log(f"step {step} epoch {ep} loss {loss:.4f} grad_norm {opt.metrics.get('grad_norm')} sec {d:.1f}")
                if step == 2:
                    sps = sum(durs) / 2
                    avail = (DEADLINE - time.time()) / sps + 2
                    planned = max(1, min(MAXEP, int(avail // steps_per_ep)))
                    log(f"timing: {sps:.1f}s/step first2; ~{avail:.0f} steps fit; planned_epochs={planned}")
                summary.update(steps=step, epochs=round(ep_done, 2), planned_epochs=planned, final_loss=losses[-1],
                               sec_per_step=round(sum(durs)/len(durs), 1), seconds=round(time.time()-t_start, 1))
                write_json(OUT / "train_summary.json", summary)
            if stop: break
        summary["partial"] = stop or ep_done < planned
        ts = time.time()
        ckpt = model.save_weights("q122b-acme-rev", mode="inference")
        log(f"save_weights {ckpt.path} step {ckpt.step} ({time.time()-ts:.1f}s)")
        rec = ru.save_checkpoint_ref(ckpt, path=OUT / "checkpoint.json", name="q122b-acme-rev", train_steps=step,
                                     epochs=summary["epochs"], batch=BATCH, lr=LR, rank=RANK, n_train=len(data),
                                     final_loss=losses[-1] if losses else None, partial=summary["partial"],
                                     train_seconds=round(time.time()-t_start, 1), saved_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        log(f"wrote {OUT/'checkpoint.json'}: {json.dumps(rec)}")
        summary.update(checkpoint=ckpt.path, seconds=round(time.time()-t_start, 1))
except Exception as e:
    log(f"ERROR {type(e).__name__}: {e}"); traceback.print_exc()
    summary["error"] = f"{type(e).__name__}: {e}"
write_json(OUT / "train_summary.json", summary)
log(f"=== train done steps={step} final_loss={losses[-1] if losses else None} total {time.time()-t_start:.1f}s")
finalize()
