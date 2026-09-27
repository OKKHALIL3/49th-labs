# Pitch: The 49th Engineer (49th Labs)

**Your team's 49th engineer. Trained on your other 48.**

This is the spoken pitch and the judge Q&A. The recorded video is planned in [`VIDEO_SCRIPT.md`](VIDEO_SCRIPT.md), and the form text is in [`SUBMISSION.md`](SUBMISSION.md).

**Honesty rules:**
- Quote only numbers that are in a result file:
  - `rev/data/eval_results.json`
  - `rev/large/*/results.json`
  - `rev/data/fleet_results_real.json`
  - `rev/data/learn_last.json` (the promoted correction run)
  - `rev/data/pr_replays.json` (a repeat of the same correction recipe, with the 30/30 regression check)
- Invent no customers, revenue or market figures.
- Acme Devices is fictional.

---

## 20-second version

Mechanical engineers at hardware startups redo the enclosure every time the board changes, and the rules they follow were never written down. The 49th Engineer learns those rules from the company's own change history with a LoRA on River, keeps the facts and records in GBrain, and works as a teammate in QM. A deterministic checker grades every result. On 30 unseen changes, generic Qwen3.5-9B passed 0/30 and the tuned model passed 29/30.

## 60-second version

Every board revision means redoing the enclosure: fit the cavity, move the standoffs, re-cut the openings. Every hardware team has rules for this, like the clearance per side, the tolerance on each cutout, and "3 mm above the tallest part." Nobody wrote them down. They live in hundreds of past revisions.

The 49th Engineer turns those revisions into a model the company owns. On 30 changes it had never seen, a generic 9B model passed zero and a 397B model passed one. Our 9B, trained on Acme's history for 17 minutes, passed 29. (Acme is our fictional demo company.)

On 48 real open-source boards, the tuned model passed 42. Five of its six misses were USB-A, a connector Acme had never shipped. An engineer typed one sentence: "Our USB-A cutouts always get 0.5 mm per side." That sentence became 48 practice changes, every one verified by the checker, mixed half and half with past work so nothing would be forgotten. After 8 River steps, the model scored 12/12 on USB-A and passed all 48 boards on the wall. A repeat of the same run scored 30/30 on the regular held-out set (v1 scored 29/30), so the old rules held.

GBrain remembers what's true. Memorable remembers how it's done. 49th Labs turns it into weights your company owns.

---

## Judge Q&A

**"Why not just prompt a frontier model?"**
- There is nothing to put in the prompt. Nobody has a document that says "0.4 mm per side on USB-C, 0.8 on barrel jacks." The rules only show up as a pattern across past revisions, and learning a pattern from examples is what fine-tuning does.
- Size doesn't substitute for the history. On the same held-out set, generic Qwen3.5-397B-A17B passed 1/30 and Qwen3.5-122B-A10B passed 1/30. The same history fixes them: after training, the 397B passed 30/30 (in a partial run) and the 122B passed 28/30.
- Some teams also want a model they own rather than a prompt on someone else's model. (That is ownership, not isolation: see the privacy question below.)

**"If the rules were never written down, how does your checker know them?"**
In this benchmark, the checker holds Acme's house values as hidden ground truth. That is what makes the grading objective. The model never sees those values in its prompt. The generic 0/30 is a control that shows the values can't be guessed. The result that matters is that 17 minutes of training on 200 past changes recovered them. With a real customer, the geometry checks carry over unchanged, and the house values are the ones their senior engineers sign off on.

**"Isn't the data synthetic?"**
- The company history is synthetic, and we say so. We generated it so the ground truth is known.
- The wall's board geometry is real: 48 open-source KiCad designs, drawn from 185 boards in 173 licensed GitHub repos. The Rev B change applied to each of those boards is synthetic.
- With a real customer, their ECO history replaces our generator.

**"Doesn't teaching it one rule break the others?"**
- It did the first time. Our naive attempt overgeneralized and put 0.5 mm on every connector.
- The fix: replay past company changes in every batch, add contrast examples where other connector types keep their own tolerance, and continue the v1 adapter instead of starting over.
- Result: USB-A went from 0/12 to 12/12, and the wall's 39 boards without USB-A all still passed. A repeat run of the same recipe scored 30/30 on the regular held-out set.

**"Is this private? Does data leave the building?"**
- It isn't air-gapped, and we don't claim it is. QM and GBrain run locally. Training and sampling run on River's hosted API.
- The claim is ownership. The company owns an open-weight base, its own LoRA checkpoints (each one recording its parent) and its GBrain records.

**"Why fine-tune instead of putting the rules in GBrain?"**
- Facts go in GBrain and skill goes in the weights.
- Facts change every revision: the Rev B dimensions, where the USB-C port is, why it moved. They belong in memory the team can read and edit.
- House rules are implicit in hundreds of past decisions, and training is how you extract an unwritten pattern.
- GBrain still matters for learning. It holds the 200 historical ECO records, the CAD captures a lesson is derived from, and the lesson pages that link each rule to the `river://` checkpoint that learned it.

**"Where does Memorable fit?"**
Memorable captures procedural memory from agent tool-call traces, and GBrain stores facts. Neither one fine-tunes a model. We are the step that turns verified records into weights. Memorable's traces would be our next capture source. We have not integrated it yet.

**"How does this become a company?"**
- **Wedge:** enclosure changes after board revisions at hardware startups. They happen often, they're tedious, and a machine can check the result.
- **Expand:** every engineering change that has to follow house rules, mechanical first and then electrical.
- **Moat:** every verified change and every correction becomes a training example only that customer has. The longer a team uses it, the more the model engineers the way that team does.

**"What doesn't it do yet?"**
- It has one capability: refitting an open-top box when the board changes.
- It has been trained on synthetic history only.
- It has no lids, fasteners, thermal analysis or electrical changes.
- Promotion after a correction reports before/after scores but does not block a regression automatically.
