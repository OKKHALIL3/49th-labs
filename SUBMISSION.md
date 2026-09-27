# Submission: The 49th Engineer (49th Labs)

Paste-ready fields for the Google Form. Before submitting, fill in `[GITHUB_URL]`, `[VIDEO_URL]`, `[SLACK_STATUS]` and `[TEAM_NAME]`. Every other number comes from a result file in the repo (listed at the bottom).

---

## Project name

The 49th Engineer, by 49th Labs

## One-liner

Your team's 49th engineer. Trained on your other 48: an AI teammate that refits the enclosure when a circuit board changes, using a model fine-tuned on the company's own engineering-change history, with a deterministic checker grading every result.

## Short description (80 words max)

For mechanical engineers at hardware startups who redo the enclosure after every board revision. The 49th Engineer refits it with an open model fine-tuned on River from the company's own change history, and a deterministic checker grades every result. On 30 unseen changes for Acme Devices, a fictional company, generic Qwen3.5-9B passed 0/30 and the Acme-tuned 9B passed 29/30. One typed correction taught it a new rule in under two minutes, without forgetting the old ones.

## Long description (250 words max)

Mechanical engineers at hardware startups redo the enclosure after every board revision. Their rules (per-side clearance, each cutout's tolerance, "3 mm above the tallest part") live in past revisions, not in any spec, and a generic model has never seen them.

The 49th Engineer turns that history into a model the company owns. A LoRA is fine-tuned on River on top of an open-weight Qwen base. GBrain holds the project facts and the engineering-change records, and the agent runs in a self-hosted QM. The model emits tool calls to a parametric enclosure kernel, and a deterministic checker (which holds the house values as grading ground truth) passes or fails every result. No LLM judge.

Acme Devices is fictional, and we generated its history so the ground truth is known. On 30 held-out changes, generic Qwen3.5-9B passed 0/30. After 17 minutes of training, the Acme-tuned 9B passed 29/30. Generic 397B and 122B models each passed only 1/30. On a wall of 48 real open-source KiCad boards, the generic model passed 16/48 and Acme v1 passed 42/48. Five of the six misses were USB-A, which Acme had never shipped. One typed sentence became 48 checker-verified practice changes, mixed with replayed history for 8 River steps. USB-A went from 0/12 to 12/12 and the wall reached 48/48. A repeat run scored 30/30 on the regular held-out set.

QM and GBrain run locally. Training and sampling run on River's hosted API. The company owns the open-weight base, its LoRA checkpoints and its GBrain records.

---

## Side quests

**QM.** The 49th Engineer lives in QM, self-hosted on local Docker (`qm init`, `qm up`; the portal web UI runs on localhost:9081).
- **Skill:** we added a deployment-layer skill, `hardware-enclosure-engineer`.
- **Tools:** sandbox tools with `tool.json` descriptors run on the agent's sandbox computer and call our server through a cloudflared tunnel. `rev-refit` runs an engineering change, and `rev-learn` hands an engineer's exact correction to the learning pipeline and reports the held-out before/after score.
- **Branding:** the bot is "49th Engineer" and the org is "Acme Devices" (`botName` / `orgName` in the QM config).
- **Result:** in QM's web UI, "Rev B landed — refit the enclosure" ran `rev-refit` and returned PASS 5/5, with every check and a 3D viewer link.
- **Slack:** the Socket Mode setup steps are documented in `qm/LOCAL_RUN.md`. Status: [SLACK_STATUS].
- **What the agent doesn't do:** the QM agent never designs geometry. It triggers the company's model and relays the checker's verdict.

**River.** River trains and serves the company's own weights. We use the Python `river_client` directly.
- **Training v1:** `client.session(app=..., run=...)`, then `session.create_model(base_model="Qwen/Qwen3.5-9B", lora=river.LoraConfig(rank=32))`, then `model.train_step(batch, lr, loss_fn="cross_entropy", grad_clip_norm=1.0)`. That is 52 steps over 4 epochs of 200 ECOs, about 17 minutes.
- **One chat template:** River's own renderer (`get_renderer`, `build_prompt_str`, `get_stop_strings`) is used for both training and sampling, and the loss applies only to the tool-call completion.
- **Saving and sampling:** `model.save_weights(name, mode="inference")` returns a `river://` checkpoint. The 48-board wall runs 48 refit jobs against that one checkpoint as concurrent `session.sample(..., checkpoint=ckpt)` calls (16 at a time), about 30 s per wave. The generic baseline uses `client.sample`.
- **Continued training:** `create_model(..., checkpoint=<v1>)` continues the same LoRA from one correction. The new checkpoint records its parent.
- **Larger bases:** the same pipeline on bigger open models: Qwen3.5-397B-A17B went from 1/30 to 30/30 (a partial, deadline-stopped run of 23 steps), Qwen3.5-122B-A10B from 1/30 to 28/30 (13 steps), and Qwen3.6-35B-A3B from 0/30 to 30/30 (52 steps).

**GBrain.** GBrain holds the facts and the engineering-change records, never skill. It is a local, keyless brain (`gbrain init --pglite --no-embedding`).
- **History:** the 200 historical ECOs are bulk-loaded with `gbrain import --no-embed` as `history/eco-N` pages, each with its summary, engineering calls and `verified: yes`.
- **Change records:** each single-product refit (engineer view, CLI or QM) writes a change-record page with `[[wikilinks]]` to the previous design, so `gbrain search` can answer "why is the opening there?"
- **Captures:** each save of a senior engineer's OpenSCAD file becomes a `captures/` page with who made it, which file, the diff and the checks.
- **Lessons:** the learner reads verified captures back with `gbrain export --slug-prefix captures/` and flags captures that disagree. It then writes a `lessons/` page that links the evidence and the `river://` checkpoint that learned it.

**Memorable (positioning, not integrated).** GBrain remembers what's true. Memorable remembers how it's done. 49th Labs turns it into weights your company owns. Memorable's agent traces are our next capture source. Nothing in this build calls Memorable.

---

## What's real and what's synthetic

- **Real:** every River training and sampling run, every checkpoint and every number above, each graded by the deterministic checker in `rev/kernel.py`. The replay modes only replay earlier real runs and are labeled as replays.
- **Real board geometry:** the wall's 48 boards come from 185 real, licensed open-source KiCad designs in 173 GitHub repos. Attribution is in `REAL_DATA.md`.
- **Synthetic:** Acme Devices and its house rules, the 200 training and 30 held-out ECOs, the 12 held-out USB-A changes, the practice changes generated from a correction, and the Rev B change applied to each real board.
- **The checker holds the ground truth for grading.** The model never sees the house values in its prompt. The generic model's 0/30 is a control that shows those values can't be guessed. The result that matters is that training recovered them.
- **Not air-gapped:** QM and GBrain run locally, but River is a hosted API and QM's agent uses a hosted LLM. The pitch is ownership (an open-weight base, the company's own LoRA checkpoints, its GBrain records), not isolation.
- **Honest failure along the way:** our first one-correction run overgeneralized. It put 0.5 mm on every connector and regressed on held-out changes it used to pass. Replaying history in every batch, adding contrast examples, and continuing from v1 instead of starting fresh fixed it (USB-A 12/12; regular held-out 30/30 in a repeat run of the same recipe).

## Tech stack

- **Teammate:** QM (self-hosted, local Docker), a custom skill, the `rev-refit` / `rev-learn` sandbox tools, and Slack Socket Mode (documented)
- **Weights:** River (`river-client`: LoRA SFT, continued training, batched sampling) on open Qwen3.5-9B, with 35B, 122B and 397B runs
- **Records:** GBrain CLI, local PGLite brain, markdown pages
- **Engine:** Python, a parametric enclosure kernel and deterministic checker, an OpenSCAD diff watcher, a KiCad `.kicad_pcb` parser
- **Interfaces:** FastAPI + SSE, a three.js engineer view, the fleet wall, and the `49th` terminal CLI (rich)

## Team

Solo: **[TEAM_NAME]**, who built the product, the code and the demo, working with a team of AI coding agents.

## Links

- GitHub: [GITHUB_URL]
- Video: [VIDEO_URL]
- Eval audit: `EVAL_AUDIT.md`
- Real-board attribution: `REAL_DATA.md`

---

*Number sources (for reviewers):*
- *Held-out eval: `rev/data/eval_results.json`*
- *Larger models: `rev/large/{q397b,q122b,q35b}/results.json`*
- *Wall: `rev/data/fleet_results_real.json`*
- *One correction: `rev/data/learn_last.json` (the promoted run, USB-A 0/12 → 12/12, 108.6 s). The regression check (regular held-out 30/30, v1 29/30) comes from a repeat run of the same 8-step recipe from the same v1 checkpoint, recorded in `rev/data/pr_replays.json` (checkpoint in `rev/data/checkpoint_pr2.json`). The promoted checkpoint itself was not re-run on the regular set.*
