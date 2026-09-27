"""River smoke test: base sample, tiny LoRA train, in-session sample, save, cross-process sample.

Run from repo root:  /opt/anaconda3/bin/python3 -m rev.river_smoke [--skip-train]
Writes rev/data/river_config.json and rev/data/smoke_checkpoint.json.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

import river_client as river

from rev import river_util as ru

SMOKE_CKPT = ru.DATA_DIR / "smoke_checkpoint.json"


def log(*a):
    print(*a, flush=True)


def pick_base(models: list[str]) -> str:
    if "Qwen/Qwen3.5-9B" in models:
        return "Qwen/Qwen3.5-9B"
    cands = [m for m in models if any(k in m.lower() for k in ("instruct", "chat", "qwen"))] or models

    def size(m: str) -> float:
        import re
        hits = re.findall(r"(\d+(?:\.\d+)?)B", m)
        return float(hits[0]) if hits else 1e9

    return sorted(cands, key=size)[0]


def toy_messages(i: int) -> list[dict]:
    q = ["Refit for a 90 mm board.", "USB-C moved left.", "Hole H3 removed.", "Board got taller parts."][i]
    return [
        {"role": "system", "content": "You are an enclosure engineer. Output ONLY a JSON array of tool calls."},
        {"role": "user", "content": q},
    ]


TOY_COMPLETIONS = [
    '[{"tool":"fit_cavity","args":{"clearance":1.5,"headroom":3.0}}]',
    '[{"tool":"place_opening","args":{"connector":"J1","tolerance":0.4}}]',
    '[{"tool":"remove_standoff","args":{"hole":"H3"}}]',
    '[{"tool":"fit_cavity","args":{"clearance":1.5,"headroom":3.0}}]',
]


def fresh_process_sample(prompt: str) -> tuple[str, float]:
    """Sample through the persisted checkpoint reference in a brand-new Python process."""
    code = (
        "import json,sys,time;from rev import river_util as ru;"
        f"t=time.time();out=ru.sample_tuned(sys.argv[1],checkpoint_path={str(SMOKE_CKPT)!r},max_tokens=64);"
        "print(json.dumps({'text':out,'s':time.time()-t}))"
    )
    r = subprocess.run([sys.executable, "-c", code, prompt], cwd=str(ru.ROOT), capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError(f"fresh-process sample failed:\n{r.stderr[-3000:]}")
    last = [l for l in r.stdout.splitlines() if l.startswith("{")][-1]
    d = json.loads(last)
    return d["text"], d["s"]


def main() -> None:
    skip_train = "--skip-train" in sys.argv
    client = ru.get_client()
    log("health_check:", client.health_check())
    caps = client.get_capabilities()
    log("capabilities:", caps)
    base = pick_base(caps)
    if base != ru.BASE_MODEL:
        log(f"switching BASE_MODEL {ru.BASE_MODEL} -> {base}")
        ru.BASE_MODEL = base
    ru.CONFIG_PATH.write_text(json.dumps({"base_model": base, "capabilities": caps}, indent=2))
    log("wrote", ru.CONFIG_PATH)

    prompt = ru.render(toy_messages(0))
    t = time.time()
    out = ru.sample_base(prompt, max_tokens=64)
    log(f"base sample ({time.time() - t:.1f}s): {out!r}")

    if not skip_train:
        data = [ru.make_datum(ru.render(toy_messages(i)), TOY_COMPLETIONS[i]) for i in range(4)]
        t = time.time()
        with client.session(app="rev", run="smoke") as session:
            log(f"session open ({time.time() - t:.1f}s)")
            t = time.time()
            model = session.create_model(base_model=base, lora=river.LoraConfig(rank=32))
            log(f"create_model ({time.time() - t:.1f}s)")
            for step in range(2):
                t = time.time()
                fb = model.forward_backward(data, loss_fn="cross_entropy")
                t_fb = time.time() - t
                opt = model.optim_step(lr=2e-4, grad_clip_norm=1.0)
                log(f"step {step}: fwd_bwd {t_fb:.1f}s total {time.time() - t:.1f}s "
                    f"loss={fb.metrics.get('loss_mean', fb.metrics.get('loss'))} grad_norm={opt.metrics.get('grad_norm')}")
            t = time.time()
            fb, opt = model.train_step(data, lr=2e-4, loss_fn="cross_entropy", grad_clip_norm=1.0)
            log(f"train_step (pipelined) {time.time() - t:.1f}s loss={fb.metrics.get('loss_mean')}")
            t = time.time()
            groups = model.sample(prompt, max_tokens=64, temperature=0.0, stop=ru.STOP)
            log(f"in-session LoRA sample ({time.time() - t:.1f}s): {groups[0][0].text!r}")
            t = time.time()
            ckpt = model.save_weights("smoke", mode="inference")
            log(f"save_weights ({time.time() - t:.1f}s): {ckpt}")
            ru.save_checkpoint_ref(ckpt, SMOKE_CKPT)
            log("wrote", SMOKE_CKPT)
            t = time.time()
            same = session.sample(prompt, base_model=base, checkpoint=ckpt, max_tokens=64, temperature=0.0, stop=ru.STOP)
            log(f"same-session checkpoint sample ({time.time() - t:.1f}s): {same[0][0].text!r}")

    text, secs = fresh_process_sample(prompt)
    log(f"fresh-process sample_tuned ({secs:.1f}s incl. session open): {text!r}")
    log("SMOKE OK")


if __name__ == "__main__":
    main()
