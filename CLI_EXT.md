# 49th personal AI engineers -- terminal commands (`rev/cli_ext.py`)

Every engineer gets an AI engineer: company model v1 (Qwen3.5-9B + Acme LoRA) plus a personal
branch that keeps learning from their own work (GBrain `engineers/<id>/`, LoRA under
`rev/data/engineers/<id>/`). Run with `/opt/anaconda3/bin/python3 -m rev.cli_ext <cmd>`
(the lead wires these into `49th <cmd>`). All logic lives in `rev/engineers.py`; this file only renders.

| command | what it shows |
|---|---|
| `engineers` | handle, role, base, personal lessons (count + latest), personal LoRA status |
| `record --as senior-me [--dir workstation] [--no-teach]` | live capture lines (file, change, verified n/n, rule, GBrain slug), training step/loss sparkline, "learned it: USB-A x/12 → y/12"; Ctrl-C prints summary |
| `test --model 9b\|35b\|122b\|397b --untrained\|--custom\|--engineer ID [--suite heldout\|usb_a] [--limit N] [--workers 8]` | live progress bar + pass/fail dots, big "Qwen3.5-9B Acme custom: 30/30", by-check breakdown, seconds |
| `test --compare --model 9b [--engineer ID]` | untrained vs custom (vs engineer) in one table |
| `ask <senior-me\|field-me\|junior-me\|company\|untrained> refit C [--model 9b]` | tool calls, USB-A opening chosen, 5 checks, pass/fail |
| `ask --all refit C` | company vs senior-me vs field-me vs junior-me: opening chosen, company-checker result |
| `teach --as ID "<sentence>" [--source slack]` | training progress, before → after |
| `merge --as ID [--open]` | default: "PR prepared" (title, author, source checkpoint; no training, production untouched). `--open` runs rev/weights_ci.open_pr, which retrains and merges into production if the gate passes |
| `reset` | `reset_all()` -- restores personal branches to pristine (senior-me 0.5, field-me 0.8, junior-me = v1); never touches production |

Quick smoke: `python3 -m rev.cli_ext test --model 9b --custom --suite usb_a --limit 4`
