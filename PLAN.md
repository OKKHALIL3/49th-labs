# The 49th Engineer — build plan (historical)

_This is the plan written at the start of the hackathon, kept for the record. The product is now The 49th Engineer by 49th Labs. The current state is in README.md and SUBMISSION.md. Some items below changed: QM runs locally in Docker rather than on Fly, the wall uses 48 real KiCad boards, and learning from one correction was added._

**Pitch (original draft):** Hardware companies can't send their designs to frontier AI (IP, NDAs, export control).
And the knowledge that matters isn't written down — tolerances, clearances, "we always do it this way"
live in hundreds of past revisions. **The 49th Engineer turns a company's revision history into its own
engineering model**, gives it the project's memory, and runs it as a teammate that makes
engineering changes and proves they're correct.

Theme fit — *Own Your Intelligence*:

| Layer | Sponsor | Job |
|---|---|---|
| **Skill** — how *this company* does engineering | **River** (LoRA SFT on open model) | weights we own |
| **Memory** — what is true about the project now, and why | **GBrain** | markdown pages we own |
| **Agent** — who does the work, where the team talks to it | **QM** (self-hosted) | the teammate |
| **Tools + proof** | ours | parametric enclosure CAD + deterministic checker |

Rule: facts go in GBrain, skill goes in weights. Never train facts.

## The 90-second demo
1. In QM (Slack/web): "Rev B of the Sensor Hub board just landed — refit the enclosure."
2. 3D view: Rev B board dropped into the Rev A enclosure → checks go **red**
   (board 6 mm wider doesn't fit, USB-C opening 14 mm off, standoffs miss holes).
3. Agent pulls the current design from GBrain, calls the River-trained model → tool calls stream →
   geometry regenerates → checks go **green**.
4. Same change, base model, same prompt, same tools → wrong tolerances → **red**.
   *"Same prompt. Same tools. Different weights."*
5. Change record written to GBrain. Ask "why is the USB-C opening where it is?" → answered from GBrain.
6. Chart: 30 held-out engineering changes, graded by the checker — base X/30 → trained Y/30 (real numbers).

## Scope (one capability, done properly)
"The board changed → refit the enclosure." Engineering API (the model's tools):
- `fit_cavity(clearance, headroom)` · `place_standoff(hole)` · `remove_standoff(hole)`
- `place_opening(connector, tolerance)` · `remove_opening(connector)`

House rules (unwritten — only in the data generator + checker, never in the prompt):
clearance 1.5 mm/side, headroom 3.0 mm, per-connector-type opening tolerance
(usb_c 0.4, hdmi 0.6, rj45 0.3, barrel 0.8, sd 1.0, per side), one standoff per hole.

Checker (binary reward): cavity fit, cavity height, standoffs aligned to holes,
openings aligned + sized per house tolerance, no orphan features.

Cut: PCB layout parsing, datasheets, thermal, Memorable, Superset.

## Build (owners)
| # | Piece | Owner | Files |
|---|---|---|---|
| 1 | Kernel: board/enclosure model, tools, checker | Claude | `rev/kernel.py` |
| 2 | Task generator: 230 "past revisions" (200 train / 30 held out) | Claude | `rev/tasks.py`, `rev/data/` |
| 3 | River SFT + eval (base vs trained) | Claude | `rev/train_river.py`, `rev/eval.py` |
| 4 | Agent loop + API server (SSE) | Claude | `rev/agent.py`, `rev/server.py` |
| 5 | GBrain wrapper + seed project memory | helper agent | `rev/gbrain_io.py`, `brain/` |
| 6 | Web UI: 3D enclosure, checks, tool stream, GBrain, eval chart | helper agent | `web/index.html` |
| 7 | QM: deploy, Slack/webhook → our API, skill doc | lead | `qm/` |
| 8 | Pitch + rehearsal | lead | — |

## QM integration (lead)
- QM runs on Fly/AWS (`qm init . --org <slug> --target fly`, `qm up`) — do it in a separate folder.
  Ask the QM table if there's a hosted hackathon instance first.
- QM must reach our API → public URL via `cloudflared tunnel --url http://localhost:8000`.
- Wiring (pick what QM supports): a QM skill/tool that calls `POST /api/agent/run`,
  and/or a QM webhook ("board revision landed") that wakes the agent.
- Then publish the 3D viewer as a QM app, or link it from the QM thread.

## Timeline (hacking ends 5:00, judging 5:00–5:45 live)
- 2:55 kernel + data generator done → **River training starts by 3:10**
- 3:30 base-model eval done; trained-model eval done by 3:45
- 3:50 agent loop + server + UI wired end-to-end
- 4:15 QM hookup; GBrain Q&A
- **4:30 feature freeze** → 4:30–5:00 rehearse ×3, record backup video

## Risks → fallbacks
- River training slow/broken by 3:30 → local LoRA on the M4 Pro with MLX (still "own your intelligence").
- Trained model not clearly better → report real numbers; never fake. Tighten data, retrain once.
- QM not up by 4:15 → demo from our web UI; show QM deployment/skill as "where it lives".
- Live-demo failure → recorded backup video + cached last good run.
