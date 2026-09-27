# Skill transfer demo — The 49th Engineer (49th Labs)

Frozen scope: one company model, two engineer identities (`senior-me` = Senior mechanical engineer,
`junior-me` = Junior mechanical engineer), the existing enclosure workflow for the fictional customer Acme Devices.

Story: reviewed work → personal model update → scoped skill pull request → evaluated company update →
another agent inherits the skill.

Rules for what appears on screen:
- Every score is computed from per-case model outputs graded by the deterministic enclosure checker
  (`rev.eval.grade`). Each `test` run saves raw model text and checker results to
  `rev/data/evals/<checkpoint>-<suite>-<ts>.json` and prints that path.
- Evaluation cases are synthetic Acme Devices history, held out and never trained on. `usb_a` = 12 cases where
  the lesson applies. `heldout` = 30 regular cases with other connector types, where behavior must not change.
- company-v1 = LoRA on Qwen3.5-9B trained on 200 verified ECOs. These ECOs are synthetic company history.
- "Merge" means the approved, checker-verified lesson examples plus replayed history examples are used to
  retrain a company CANDIDATE that continues from the production checkpoint. The candidate goes through tests
  and a gate. This is not a parameter merge.
- company-v2 does not update any personal checkpoint. `49th checkpoints` and `49th agents` show which checkpoint
  each agent uses. An agent moves to a new checkpoint only through `49th inherit`, an explicit step.

All commands run from the repo root through `bin/49th`. The launcher sends
`checkpoints|agents|inherit|reset-agents`, `test --checkpoint …` and `pr --from …` to `rev.transfer_cli`.

## 0. Before each take (reset) — also run after a take; steps 4 and 6 change the ledger and agents.json

```sh
/opt/anaconda3/bin/python3 -m rev.weights_ci reset   # production = company-v1, PR list cleared (recorded replays kept)
bin/49th reset-agents                                # senior-me -> senior-me-v1, junior-me -> company-v1
bin/49th reset                                       # (rev.cli_ext) personal branches back to the pristine snapshot
```

## 1. Baseline: what the company model does today (LIVE)

```sh
bin/49th checkpoints
bin/49th test --checkpoint company-v1 --suite usb_a
```
Output: a lineage tree `qwen3.5-9b → company-v1 → senior-me-v1`, with the River ref, what each checkpoint was
trained from, and which agent uses it. Then a live per-case table (case, change, PASS/FAIL, failed checks, the
USB-A tolerance the model chose, other tolerances). Closing line: `company-v1 · usb_a suite: N/12`. In rehearsal,
company-v1 picked 0.6–1.1 mm for USB-A and failed `openings` on every case (0/3 with `--limit 3`; the personal
branch lesson record shows 0/12 before training).

## 2. Record reviewed work into senior-me's personal branch (LIVE, or pre-trained)

```sh
bin/49th record --as senior-me        # rev.cli_ext: the saved CAD change in workstation/ -> capture -> lesson
```
Output: the reviewed CAD change (for example `opening_J3_usb_a_w 15.34→14.14`) is captured and written to GBrain
as a lesson. A personal LoRA is trained from company-v1 and gives `senior-me-v1`. Training takes about 96 s. For
a short take, use the pre-trained `senior-me-v1` that is already on disk: `49th checkpoints` shows its River ref
and lesson slug.

## 3. Test the personal checkpoint where the lesson applies and where it must not change anything (LIVE)

```sh
bin/49th test --checkpoint senior-me-v1 --suite usb_a      # applies:      expect ~12/12 vs company-v1 0/12
bin/49th test --checkpoint senior-me-v1 --suite heldout    # must not change other connectors
bin/49th test --checkpoint company-v1   --suite heldout    # company reference (29/30 at publish)
```
Rehearsal (16:03 PT, live): senior-me-v1 scored usb_a 12/12 and heldout 30/30, saved to
`rev/data/evals/senior-me-v1-usb_a-20260927-160310.json` and `…-heldout-20260927-160337.json`. Each run prints its saved per-case file. Add `--limit N` for a quicker take and `--cases` to dump each case's
raw model output.

## 4. Skill pull request → company candidate → tests → gate

```sh
bin/49th pr --from senior-me --approve --replay     # PRERECORDED real run (labelled on screen)
# bin/49th pr --from senior-me --approve            # LIVE: retrains on River, ~165 s; only when nothing else trains
```
Output: a Skill PR card with these fields:
- Proposed lesson
- Source: GBrain lesson slug, the reviewed CAD file and diff, author role, time
- Scope: "USB-A openings; must not change other connector types"
- Permission: "requires approval to share company-wide"
- Plan: 48 checker-verified lesson examples plus 48 replayed history examples; retrain the company candidate
  from company-v1 on River; run the usb_a suite (12) and the held-out suite (30); gate at usb_a ≥ 80% and
  held-out ≥ production − 1

The stages stream after the card: parsed, GBrain decision record, conflict check against the 200 verified ECOs,
training (8 steps, with losses), usb_a before and after, regression 10/20/30 of 30, then the decision. The
recorded run shows candidate `river://a599c7a1…/learn-usb_a-1790549138`, usb_a 0/12 → 12/12, held-out 30/30
(production 29/30), then `gate passed: retrained candidate promoted to company-v2 (not a parameter merge)`.
These PR numbers are the recorded run's aggregates (`rev/data/pr_replays.json`); step 5 re-tests company-v2 live. The closing line is
`company-v2 published · personal checkpoints unchanged (senior-me-v1, junior-me→company-v1)`.
Without `--approve`, the card is printed and nothing is trained.

## 5. Evaluate company-v2 on both suites (LIVE)

Rehearsal (16:05 PT, live): company-v2 usb_a 3/3 (`--limit 3`, all 0.5 mm) and heldout 12/12 (`--limit 12`);
company-v1 usb_a 0/3 (`--limit 3`, 1.1/0.6/0.6 mm). Files in `rev/data/evals/`.

```sh
bin/49th test --checkpoint company-v2 --suite usb_a
bin/49th test --checkpoint company-v2 --suite heldout
bin/49th checkpoints      # company-v1 [superseded] → company-v2 [production]; senior-me-v1 still on its own branch
bin/49th agents           # junior-me is STILL on company-v1: publishing did not move anyone
```

## 6. Another agent inherits the skill (explicit step, LIVE ask)

```sh
bin/49th inherit --agent junior-me --from company-v2        # rebase an existing agent
# bin/49th inherit --agent new-hire-me --from company-v2    # or create a new agent from company-v2
```
Output: `junior-me: company-v1 → company-v2` with the River ref; other agents are listed as untouched. The agent
is then asked to refit the enclosure for Rev C (`rev/data/demo_rev_c.json`), an unseen task with a USB-A port.
The prompt has no retrieval and no lesson text. The output shows the model's tool calls, with the USB-A opening
tolerance highlighted, and the 5 enclosure checks. In rehearsal, junior-me on company-v2 placed J5 (usb_a) at
0.5 mm/side and J2 (hdmi) at 0.6 mm/side, and passed 5/5. The result is saved to
`rev/data/evals/company-v2-ask-rev_c-<ts>.json`.

## Live vs prerecorded

| step | run |
|---|---|
| 1, 3, 5 tests | LIVE model sampling on River plus live checker grading, 12 workers |
| 2 record | LIVE capture; personal training is LIVE (~96 s) or uses the pre-trained senior-me-v1 on disk |
| 4 PR | `--replay` = prerecorded real run (a real River job whose candidate checkpoint still exists; the ledger merge is real). Without `--replay` = LIVE |
| 6 inherit + ask | LIVE |
| company history (200 ECOs), eval cases | synthetic Acme Devices data |
