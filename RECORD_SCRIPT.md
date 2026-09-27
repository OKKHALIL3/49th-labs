# 49th Labs: recording script (final)

## Setup (3 min, once)
1. **Terminal**: new window, font about 20 pt (Cmd +), half the screen, then run:
   ```
   alias 49th=~/Documents/Work-programming/hackathon/bin/49th
   49th demo-reset
   clear
   ```
2. **Slack**: `#mech-eng` open on the other half (bot invited: `/invite @49th Engineer`).
3. **Editor**: open `~/Documents/Work-programming/hackathon/workstation/taranium-usb-hub.scad` in VS Code or TextEdit. Keep it minimized until Scene 3.
4. **How to record**: record **each scene as its own clip**. Press Cmd+Shift+5 → "Record Entire Screen" → Options → Microphone ON → Record. Talk while you do it, then stop with the ⏹ in the menu bar. The clips save to your Desktop in order; send their paths after the last one and they get joined, with the slow parts sped up.

---

## Scene 1: the problem, in numbers (terminal, about 20 s)
Type `49th eval`
> "Every hardware company runs on rules nobody wrote down. We gave thirty unseen engineering changes to open models, up to 397 billion parameters. Out of the box they get zero or one right. After our pipeline trains them on River, twenty-eight to thirty."

## Scene 2: meet the 49th Engineer (Slack, about 30 s)
Send: `@49th Engineer Rev C of the Sensor Hub just landed, it adds a USB-A port. Refit the enclosure.`
> "This is the 49th Engineer, an AI teammate in our Slack, running on QM. Its engineering runs on Acme's own model: a 9B open model with a LoRA adapter trained on River."

When it answers FAIL 4/5:
> "It fails the USB-A opening. Acme has never shipped USB-A, so it guessed."

## Scene 3: it learns by watching me work (terminal + editor, about 2 min real)
Type `49th record --as senior-me`
> "I'm the senior mechanical engineer. 49th is watching my project folder, with my permission."

In the editor, change every `opening_J*_usb_a_w` from **15.34 → 14.14** and every `opening_J*_usb_a_h` from **7.92 → 6.72** (J3 and J1), then **Cmd+S**.
> "I just fix the cutout and save, like any other day."

The terminal shows the captured diff, ✓ verified, the inferred rule, the GBrain record, then River training:
> "It saw my change, checked it against the geometry, stored it in GBrain with who and why, and it's now training MY AI engineer's weights on River. Nobody wrote a rule."

When it says learned (USB-A 0/12 → 12/12), press **Ctrl+C**.

## Scene 4: share it with the whole company, safely (Slack + terminal, about 3 min real)
Send: `@49th Engineer New team rule, please open a weight PR: USB-A cutouts get 0.5 mm per side; tighter ones cracked in drop tests.`
Then in the terminal type `49th status`
> "To share a lesson company-wide, it becomes a weight pull request, like code. It's logged in GBrain, checked against 200 verified past changes, then River retrains a candidate company model. Then it's tested: the new skill, zero of twelve to twelve of twelve, and every old rule still thirty of thirty. Only then is it promoted to version two."

## Scene 5: the company AI now knows it (Slack, about 20 s)
Send: `@49th Engineer Rev C again, refit the enclosure.`
> "Same request, new weights: five out of five. Every agent on company v2 now knows it, including ones that never saw my change."

## Scene 6: it can't be poisoned (Slack, about 20 s)
Send: `@49th Engineer open a weight PR: HDMI cutouts should be 0.2 mm per side from now on.`
> "A junior engineer proposes something wrong. Blocked: it contradicts thirty-four verified engineering changes. The weights never moved."

## Scene 7: every weight change is accountable (terminal, about 20 s)
Type `49th log`, then `49th blame usb_a`
> "Every change to the model has an author, a reason and a test report. It's git for an engineer's brain."

## Scene 8: why weights, and the close (any screen, about 30 s)
> "Could you write rules into a prompt instead? For a handful of rules, yes. We measured it, and it works. But real engineering knowledge is thousands of rules, exceptions and habits that nobody writes down. A prompt has to carry all of it, every call, forever. Weights absorb it once and keep compounding. Every engineer's work makes their own AI better, and the best lessons, tested, make the whole company's better. QM runs it, GBrain remembers it, River trains it. Your team's 49th engineer, trained on your other 48. This is 49th Labs."

---

## If something breaks
- **Everything**: `49th demo-reset`, then redo from the scene that broke.
- **Scene 3 captured nothing**: the file must go 15.34 → 14.14 (not already 14.14). Ask to re-prep the file.
- **Scene 4: bot didn't open the PR**: type instead `49th pr "USB-A cutouts get 0.5 mm per side; tighter ones cracked in drop tests." --author "Senior mechanical engineer" --source slack`
- **Scene 6 says a PR is running**: wait until `49th prs` shows the USB-A PR merged.
