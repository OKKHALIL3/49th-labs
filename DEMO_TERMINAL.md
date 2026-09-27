# Demo terminal script: personal AI engineers

Before recording: `export COLUMNS=110`, `cd` to the repo, and alias `49th="/opt/anaconda3/bin/python3 -m rev.cli_ext"`
(or use `bin/49th` if the lead has wired these commands in). Customer "Acme Devices" is fictional. Engineers are
role handles: `senior-me` (you), `field-me` and `junior-me`.

Start clean with `49th reset`. This restores every personal branch to its pristine pre-trained state:
senior-me = 0.5 mm, field-me = 0.8 mm, junior-me = company v1. It never touches the production checkpoint.

| # | command | what it shows (real output from rehearsal) | time |
|---|---|---|---|
| 1 | `49th engineers` | Roster panel: senior-me, field-me, junior-me · base v1 · 9B+LoRA · lesson counts (usb_a 0.5 / 0.8 / none) · personal LoRA "trained · 8 steps" or "= company v1" | 2 s |
| 2 | `49th test --model 9b --untrained --limit 10` | Qwen3.5-9B untrained: **0/10**. By check: cavity_fit 3/10, cavity_height 3/10, openings 0/10 | ~12 s |
| 3 | `49th test --model 9b --custom --limit 10` | Qwen3.5-9B Acme custom: **10/10**, every check 10/10 | ~15 s |
| 4 | `49th test --model 397b --untrained --limit 6` | Qwen3.5-397B-A17B untrained: **0/6** (openings 1/6). A frontier-size base model still can't do Acme's refits | ~12 s |
| 5 | `49th ask --all refit C` | How each AI engineer would refit rev C: company 1.1 mm ✗ 4/5 · **senior-me 0.5 mm ✓ 5/5** · field-me 0.8 mm ✗ company / ✓ its own rule · junior-me 1.1 mm ✗ 4/5 | ~10 s |
| 6 | `49th record --as senior-me` → in a second pane, save the rev C .scad with the USB-A openings at 0.5 mm/side | Live line: `✓ saved taranium-usb-hub.scad · place_opening J3/J1/J2 0.5 mm/side · rule usb_a 0.5 mm` → `GBrain engineers/senior-me/captures/cap-…  ✓`, then personal LoRA training on River and "learned it" before → after. Ctrl-C prints a summary | capture <1 s, training 86 s |
| 7 | `49th merge --as senior-me` | Weight PR panel: title "USB-A cutout tolerance 0.5 mm", status **PR prepared**, author, source River checkpoint, next = `--open` | 2 s |
| 8 | (optional) `49th merge --as senior-me --open` | Real weight CI: retrains the company model, runs the lesson and regression gate, and **merges into production** if it passes. Only run this on camera if you want production changed | minutes |
| 9 | `49th reset` | Every personal branch back to its pristine state | 2 s |

Setting up step 6 (checkout before recording): `/opt/anaconda3/bin/python3 -c "from rev import capture_api; print(capture_api.checkout(source='real_fleet'))"`
creates `workstation/taranium-usb-hub.scad`. The real_fleet checkout **already comes out at 0.5 mm/side**, so saving it
without changes records nothing. Set the three `opening_J*_usb_a_w/_h` values to body + 2.2 (1.1 mm/side, the company
default) **before** starting `record`, and make the sidecar `workstation/.rev/taranium-usb-hub.scad.json` `params`
match. Then start `record` and save with body + 1.0 (0.5 mm/side). This prep
one-liner does the checkout and the 1.1 mm baseline:

```
/opt/anaconda3/bin/python3 -c "
import re,json; from rev import capture_api,scad,watcher as W; from pathlib import Path
p=Path(capture_api.checkout(source='real_fleet')['path']); s=p.read_text()
for j,bw,bh in re.findall(r'cut-out for (J\d+) usb_a \(connector body ([\d.]+) x ([\d.]+)\)',s):
    s=re.sub(rf'opening_{j}_usb_a_w = [\d.]+;',f'opening_{j}_usb_a_w = {float(bw)+2.2:.2f};',s); s=re.sub(rf'opening_{j}_usb_a_h = [\d.]+;',f'opening_{j}_usb_a_h = {float(bh)+2.2:.2f};',s)
p.write_text(s); sc=W.sidecar_path(p); d=json.loads(sc.read_text()); d['params']=scad.parse_params(s); sc.write_text(json.dumps(d,indent=1)); print(p)"
```
On camera, edit each USB-A `_w`/`_h` to body + 1.00 (for example 13.14 becomes 14.14 and 5.72 becomes 6.72) and save.

Rehearsal result for step 6: the capture and GBrain lines appeared within 1 s of the save, then the loss
sparkline and `✓ senior-me's engineer learned it: USB-A 12/12 → 12/12 (86s)` with a new personal River checkpoint.
It shows 12/12 → 12/12 because the pristine senior-me branch already has the 0.5 mm lesson. For a visible
before → after jump, run `record --as junior-me` instead (not rehearsed; junior-me starts from company v1 at 0/12).
Run `49th reset` again after step 6.

Other useful commands:
- `49th test --compare --model 9b` shows untrained vs custom in one table.
- `49th test --model 9b --engineer senior-me --suite usb_a` scores the personal branch on the 12 USB-A tasks (12/12).
- `49th teach --as field-me "USB-A cutouts get 0.5 mm per side" --source slack` teaches from a chat message.
