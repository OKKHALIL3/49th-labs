# `49th` — The 49th Engineer from the terminal

A small CLI over the running REV server (`http://localhost:8000`). Every number it prints comes from a real result
file or a live endpoint; nothing is hard-coded.

## Setup (once)

```sh
# needs rich + httpx in /opt/anaconda3 (both already installed)
alias 49th='./bin/49th'
# make it permanent:
echo "alias 49th='./bin/49th'" >> ~/.zshrc && source ~/.zshrc
```

`bin/49th` cd's into the repo and runs `python3 -m rev_cli` with `/opt/anaconda3/bin/python3`
(override with `FORTYNINE_PYTHON`; point at another server with `FORTYNINE_SERVER`).

## Commands

| command | what it does |
|---|---|
| `49th status` | one panel: customer, `acme-9b` checkpoint (v1, plus v2 once a lesson is promoted), GBrain record counts, QM agent (localhost:9081), server health |
| `49th eval` | held-out table from `rev/data/eval_results.json` + `rev/large/*/results.json`: generic base models vs. Acme-tuned ones (tuned rows with unfinished runs are marked `partial`) |
| `49th eval --wall` | adds the three wall waves from `rev/data/fleet_results_real.json` |
| `49th teach "Our USB-A cutouts always get 0.5 mm per side."` | real live learning: `POST /api/learn {"text","promote":true}`, streams `/api/events`. Retrains on River (~110 s) and promotes v2 |
| `49th teach --replay "..."` | same view, replays the last recorded real run (`"cached": true`, ~20 s) and promotes its checkpoint. Use it for re-takes |
| `49th refit C` / `49th refit B` | lands the board revision (`POST /api/revision`) and refits with the tuned model (`POST /api/qm/refit`): change, tool calls, 5 checks, latency, 3D viewer URL |
| `49th reset` | `POST /api/learn/reset` + `POST /api/reset`: the model goes back to v1 (USB-A rule unknown) and the board goes back to Rev A |

## Recording flow

```sh
49th reset          # clean take
49th status
49th eval --wall
49th refit C        # v1 gets the new USB-A opening wrong (4/5 checks)
49th teach "Our USB-A cutouts always get 0.5 mm per side."      # or: 49th teach --replay "..."
49th refit C        # v2: 5/5
49th reset          # before the next take
```

If a learn run is already in progress, `teach` and `reset` report the server's 409 and do nothing.
Sample output: `CLI_SAMPLES.txt`.
