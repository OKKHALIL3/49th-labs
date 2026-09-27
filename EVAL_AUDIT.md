# Eval audit: The 49th Engineer (adversarial)

Headline: **generic Qwen3.5-9B 0/30, Acme-tuned 9B 29/30** on `rev/data/test.jsonl`. We recounted it from the raw model outputs and got the same numbers. Sections 6-8 cover the larger models, the 48-board wall and learning from one correction.

## 1. House values are never given as rules
- `SYSTEM` + `TOOLS_DOC` contain no house numbers (the only digit in the whole system prompt is `H9` in the format example). The user-prompt template has no numbers either.
- No train or test `change_summary` mentions clearance, tolerance, headroom or standoffs.
- **Caveat judges may raise:** the Rev A enclosure JSON in the prompt was built with the house rules. So clearance (`board_origin` = 1.5) and headroom (cavity height arithmetic) *can be inferred* from context by either model. The base model still gets cavity_fit only 16/30 and headroom 14/30.
- Opening tolerances cannot be inferred: **27/30 test tasks, and the demo (RJ45 J4), need the tolerance for a connector type that is not on the Rev A board.** Base gets openings 0/30 and tuned 29/30, so that value has to come from the weights.

## 2. Train and test are disjoint by content, not just by id
We compared the board geometry (ignoring rev and name) of all 200 train and 30 test tasks:
- (Rev A, Rev B) pairs shared: 0. Test Rev A boards seen anywhere in train: 0. Test Rev B boards seen anywhere in train: 0. Rendered user prompts shared: 0. Change-summary strings shared: 0.
- Rev B signatures (size, max height, connector type/side/pos) shared: 0.
- Test and train reuse the same pool of product names (11/11), and 7 test gold call lists are the same *text* as some train gold (short diffs like `place_standoff(H5)`). The boards in those tasks are different.
- Demo: its board pair, each of its boards, its prompt and its gold calls are all absent from train. 14 train boards happen to share the "Sensor Hub" name.
- The 12-task validation set used during training came from seed 777, excluding train and test. The test set was not used for training decisions.

## 3. Base and tuned are evaluated the same way
`rev/eval.py run_one`: both models get the same `ru.render(prompts.messages(task))` (Qwen chat template, thinking off), temperature 0.0, max_tokens 768 and the same stop strings. Both go through the same `grade()` (parse_calls, then apply_calls on board_b / enclosure_a, then `kernel.check`). The only difference is `sample_base` versus `sample_tuned` with the LoRA checkpoint. The live agent (`rev/agent.py`) also uses one code path for both models.

## 4. Fresh recount matches `eval_results.json`
We re-graded every stored raw output against the current `test.jsonl` (no outputs were truncated):
- base: 0/30. By check: cavity_fit 16, cavity_height 14, standoffs 27, openings 0, orphans 30. Parse errors 0, sample errors 0. **Matches.**
- tuned: 29/30. By check: 30, 30, 30, 29, 30. **Matches.** The one failure, test-013, is `place_opening` for an HDMI connector with no `tolerance` argument (a ToolError).
- Demo in the eval harness: tuned 3/3 and base 0/3. Both recount the same.
- The gold calls pass on all 30 test tasks, and the Rev A enclosure fails on every Rev B board. `test.jsonl` (14:38) was last modified before both evals ran (14:45, 15:03).

## 5. Live demo (server :8000, 15:07)
- Tuned, live: 3 of 3 runs through `/api/agent/run` passed 5/5, and 1 more through `/api/qm/refit` also passed. Model latency was 4.4 to 5.8 s, and a full run with animation took about 8 s.
- Base, live: failed cavity_fit, cavity_height and openings (clearance 1.0, headroom 10, tolerance 0.5 everywhere).
- River is not bit-deterministic at temperature 0. An earlier base run used clearance 2.0 instead of 1.0, and it failed as well.

## 6. Larger generic models, same 30 changes, same grader
Source: `rev/large/{q397b,q122b,q35b}/results.json`. Each model got the same `test.jsonl`, temperature 0, max_tokens 768 and the same `rev.eval.grade`. Every model used its own tokenizer, loaded through `river.load_tokenizer`, with River's renderer (thinking off).

| Base (FP8 on River) | Generic | By check (fit / height / standoffs / openings / orphans) | Acme-tuned | Training |
|---|---|---|---|---|
| Qwen3.5-397B-A17B | **1/30** | 30 / 18 / 29 / 3 / 30 | **30/30** (partial run) | 23 steps (about 1.8 epochs; 4 planned, stopped by a deadline), 1383 s |
| Qwen3.5-122B-A10B | **1/30** | 30 / 17 / 29 / 2 / 30 | **28/30** (openings 28) | 13 steps, 1 epoch, 892 s |
| Qwen3.6-35B-A3B | **0/30** | 29 / 6 / 25 / 3 / 30 | **30/30** | 52 steps, 4 epochs, 980 s |
| Qwen3.5-9B (main) | **0/30** | 16 / 14 / 27 / 0 / 30 | **29/30** | 52 steps, 4 epochs, about 17 min |

The large generic models infer clearance from the Rev A enclosure more often than the 9B does (see section 1). They still get opening tolerances wrong, because those tolerances can't be inferred. The 122B training was deadline-bounded, and its epoch planner chose 1 epoch (13 steps).

## 7. The wall: 48 real KiCad boards
Source: `rev/data/fleet_results_real.json`, 16 workers, all runs on Qwen3.5-9B. The boards come from `rev/data/real_fleet.json`: real outlines, holes and connectors, with a synthetic Rev B change. See `REAL_DATA.md`.

| Wave | Passed | Wall-clock |
|---|---|---|
| Generic Qwen3.5-9B | **16/48** | 26 s |
| Acme v1 (`acme-rev`, 52 steps) | **42/48** | 29 s |
| v2, after one correction | **48/48** | 36 s |

- **v1's 6 failures:** all 6 fail `openings`.
  - 5 are USB-A boards (real-fleet-07, 23, 33, 38 and 43). On each one, v1 used 0.6 mm on the USB-A opening.
  - The sixth (real-fleet-08) is an SD-card board where v1 emitted `remove_opening` for the SD connector instead of placing it.
- **USB-A boards that passed:** the real fleet has 9 USB-A boards. The other 4 passed under v1 because their change did not require a new USB-A opening.
- **v2's checkpoint:** its `parent` field is the v1 checkpoint.
- **Default fleet:** the server loads this real fleet unless you set `FLEET_SOURCE=synthetic`.
- **Synthetic fleet results** (`rev/data/fleet_results.json`): generic 2/48, v1 33/48 (0/12 USB-A), and 48/48 after a correction. We keep them for reference, but the headline wall numbers are the real-board ones above.

## 8. Learning from one correction
Source: `rev/data/learn_last.json` (the promoted run) and `rev/learn.py`.

**The promoted run:**
- **Input:** "Our USB-A cutouts always get 0.5 mm per side.", parsed to `{"type": "usb_a", "tolerance": 0.5}`.
- **Practice changes:** 48 generated, 48 verified by `kernel.passes` (lesson seed 7001), plus 48 replayed history rows that place openings for other connector types.
- **Batches:** each batch is 16 examples, 8 lesson and 8 replay. 70% of lesson rows also re-place another type's opening at its own house tolerance (`CONTRAST_P`).
- **Training:** 8 River `train_step`s at lr 1e-4, continuing from the v1 checkpoint (`create_model(checkpoint=...)`). Total time was 108.6 s.
- **Held-out USB-A** (`rev/data/usb_a_test.jsonl`, 12 rows, seed 7002, disjoint from the lesson seed): **0/12 before and 12/12 after**. In the "before" outputs, v1 guessed 0.6, 1.1 or 1.6 mm per side for USB-A.

**The regression check.** `python3 -m rev.learn --text "..." --heldout` also grades the 30 regular held-out changes after learning.
- A run with the identical recipe (same seeds, same 8+8 batches, 8 steps, lr 1e-4, continuing from the same v1 checkpoint) logged `regular held-out after learning: 30/30` and USB-A 0/12 → 12/12, in 88 s total. Its checkpoint (`learn-usb_a-1790546782`) is the one recorded as `tuned_v2` in the synthetic `fleet_results.json` (48/48).
- The 6-step variant logged 29/30.
- **Caveat:** those logs were written to a session scratch directory and are not committed. Re-run the command above to reproduce them.
- The promoted checkpoint itself (`learn-usb_a-1790547261`) was not run with `--heldout`. Its no-forgetting evidence is the real wall: 48/48, including the 39 boards that have no USB-A.

**What didn't work:**
- A fresh LoRA trained on history plus the lesson (no v1 checkpoint, 6 steps) reached only 4/12 on USB-A.
- Our first naive continued-training run overgeneralized: it applied 0.5 mm to every connector type and regressed on the regular set. That run's numbers were not saved, so we don't quote them. Replay, contrast examples and continuing from v1 are the fixes.

**Not gated:** `learn()` promotes whenever `promote=True`. It reports before and after, but it does not refuse to promote on a regression.

## Honest limits
- The data is synthetic, and the test set comes from the same generator distribution as the training set (in-distribution generalization to unseen boards). The wall uses real board geometry, but its Rev B changes are synthetic.
- There is one training run per base model, and the held-out sets are small (n=30, n=12).
- River sampling is not bit-deterministic at temperature 0. A live re-run can differ by a board or two from the recorded files.
