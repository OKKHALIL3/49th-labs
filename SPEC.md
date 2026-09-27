# The 49th Engineer — build contracts (every agent builds to THIS; don't change a contract without updating this file)

49th Labs / The 49th Engineer. The Python package keeps the codename `rev`.

Python: `/opt/anaconda3/bin/python3` (has fastapi, uvicorn, numpy, requests, httpx). Run everything from the repo root.
Package dir `rev/` (has `__init__.py`), import as `from rev import kernel`.
Secrets in `.env` (`RIVER_API_KEY=...`, optional `RIVER_MODEL=...`). Never print secrets. No real people's names/emails anywhere
(company = "Acme Devices", project = "Sensor Hub", people = role names only).

## 1. Domain model (units: mm)

Board (board-local coords: origin = board bottom-left corner, x right, y back; connector z = center height above board top):
```json
{"rev":"A","name":"Sensor Hub","w":85.0,"d":56.0,"t":1.6,"max_component_h":12.0,
 "holes":[{"id":"H1","x":3.5,"y":3.5,"dia":2.7}],
 "connectors":[{"id":"J1","type":"usb_c","side":"front","pos":34.0,"z":1.63,"w":8.94,"h":3.26}]}
```
- `side` ∈ front (y=0 edge) | back (y=d) | left (x=0) | right (x=w). `pos` = connector center along that edge
  (x for front/back, y for left/right). `type` ∈ usb_c | hdmi | rj45 | barrel | sd.
- Connector sizes by type (w,h,z): usb_c 8.94,3.26,1.63 · hdmi 15.0,5.6,2.8 · rj45 16.0,13.5,6.75 · barrel 9.0,11.0,5.5 · sd 12.0,2.0,1.0

Enclosure (cavity coords: origin = interior floor bottom-left corner; x right, y back, z up):
```json
{"wall":2.0,"standoff_h":5.0,
 "cavity":{"w":88.0,"d":59.0,"h":21.6},
 "board_origin":{"x":1.5,"y":1.5},
 "standoffs":[{"hole":"H1","x":5.0,"y":5.0,"dia":5.5,"bore":2.2}],
 "openings":[{"connector":"J1","side":"front","pos":35.5,"z":8.23,"w":9.74,"h":4.06}]}
```
- Board sits at (board_origin.x, board_origin.y) on top of standoffs → board bottom at z=standoff_h, top at z=standoff_h+t.
- Walls: front wall inner face y=0, back y=cavity.d, left x=0, right x=cavity.w; thickness `wall`, outward. Open top (no lid).
- Opening: rectangle on wall `side`, center `pos` along the wall axis (cavity coords), center height `z` from floor, size w×h.

## 2. HOUSE RULES (secret "tribal knowledge": ONLY in kernel.HOUSE, generator, checker — NEVER in any prompt)
clearance 1.5 (each side) · headroom 3.0 · standoff_h 5.0 · wall 2.0 · standoff dia 5.5, bore = hole.dia − 0.5 ·
opening tolerance per side by type: usb_c 0.4 · hdmi 0.6 · rj45 0.3 · barrel 0.8 · sd 1.0

## 3. Engineering tools (what the model calls). A call = `{"tool": "<name>", "args": {...}}`
| tool | args | effect |
|---|---|---|
| `fit_cavity` | clearance, headroom | cavity.w=board.w+2c, cavity.d=board.d+2c, cavity.h=standoff_h+t+max_component_h+headroom, board_origin=(c,c) |
| `place_standoff` | hole | upsert standoff for hole id: x=origin.x+hole.x, y=origin.y+hole.y, dia 5.5, bore=hole.dia−0.5 |
| `remove_standoff` | hole | delete standoff with that hole id |
| `place_opening` | connector, tolerance | upsert opening: side=conn.side; pos=origin.x+conn.pos (front/back) or origin.y+conn.pos (left/right); z=standoff_h+t+conn.z; w=conn.w+2·tol; h=conn.h+2·tol |
| `remove_opening` | connector | delete opening with that connector id |
Unknown ids / missing args → `kernel.ToolError`. Tools are generic; the house values are what the model must supply.

## 4. kernel.py API
`HOUSE`, `CONNECTOR_SPECS`, `ToolError`, `enclosure_for(board)` (correct per house rules),
`apply_call(board, enc, call) -> enc` (pure, deep-copies), `apply_calls(board, enc, calls) -> (enc, errors:list[str])` (skips bad calls, records errors),
`check(board, enc) -> list[{"id","label","pass","detail","refs"}]` with ids in this order:
`cavity_fit` ("Board-to-wall clearance"), `cavity_height` ("Headroom above tallest part"), `standoffs` ("Standoffs on mounting holes"),
`openings` ("Connector openings"), `orphans` ("No orphan features"). eps = 0.051 mm. `refs` = failing hole/connector ids (or ["board"]).
`passes(board, enc) -> bool`, `TOOLS_DOC` (text describing the 5 tools + args, NO house values).

## 5. Tasks (`rev/tasks.py`, data in `rev/data/`)
Row: `{"id","board_a","enclosure_a","board_b","change_summary","gold_calls"}`. `enclosure_a = enclosure_for(board_a)`.
Changes (1–3/task): resize board (right/back-corner holes move with the edge), move connector, add connector, remove connector,
move/add/remove hole, taller component. Gold = minimal diff: `fit_cavity(1.5,3.0)` if w/d/t/max_h changed, then removals, then places.
Invariants (asserted): apply(gold) on board_b passes 100%; enclosure_a on board_b fails ≥1 check.
Files: `train.jsonl` (200, seed 1), `test.jsonl` (30, seed 2, disjoint), `demo.json` (hand-made Sensor Hub scenario:
Rev A 85×56 → Rev B 6 mm wider, USB-C moved 14 mm left, + one extra change).

## 6. Prompts (`rev/prompts.py`)
`SYSTEM` (you are Acme Devices' enclosure engineer; apply the engineering change; output ONLY a JSON array of tool calls;
tools = TOOLS_DOC; NO house values), `user_prompt(task) -> str` (current design Rev A board+enclosure JSON, new board Rev B JSON,
change_summary), `messages(task)`, `completion(task) -> json.dumps(gold_calls)`,
`parse_calls(text) -> list|None` (strip <think>…</think>, code fences; parse first JSON array).

## 7. River (`rev/river_util.py`) — docs: docs.river.ai/quickstart, docs.river.ai/guides/sft
`pip install river-client` · `import river_client as river` · `river.Client(api_key=...)` · `client.get_capabilities()` ·
`client.session(project=...)` → `session.create_model(base_model=BASE, lora=river.LoraConfig(rank=32))` ·
`model.forward_backward(batch, loss_fn="cross_entropy")` · `model.optim_step(lr=2e-4, grad_clip_norm=1.0)` ·
`model.sample(prompt, max_tokens, temperature=0.0, stop=[...])` · `model.save_weights(name, mode="inference")` →
`session.sample(prompt, base_model=BASE, checkpoint=ckpt, ...)` · base: `client.sample(prompt, base_model=BASE, max_tokens=...)`.
Datum = `{"input_ids","target_tokens","weights"}` (prompt weights 0, completion+EOS weights 1). Default BASE `Qwen/Qwen3.5-9B` if available.
Exports: `get_client()`, `BASE_MODEL`, `render(messages)->str` (chat template, add_generation_prompt, thinking off),
`make_datum(prompt_text, completion_text)`, `sample_base(prompt_text, **kw)->str`, `sample_tuned(prompt_text, **kw)->str`
(uses checkpoint recorded in `rev/data/checkpoint.json`).

## 8. Server (`rev/server.py`, FastAPI on :8000, `python3 -m uvicorn rev.server:app --port 8000`)
State JSON: `{"project":{...},"board":Board,"prev_board":Board|null,"enclosure":Enclosure,"checks":[...],"log":[{"t","kind","text"}],"running":bool,"model":str|null}`
- `GET /` → `web/index.html`; `GET /api/state`; `POST /api/reset` (Rev A, all green); `POST /api/revision` (load demo Rev B board, old enclosure → red)
- `POST /api/agent/run` `{"model":"base"|"tuned"}` → async run; `GET /api/events` SSE, each `data:` is JSON:
  `{"type":"state","state":...}` · `{"type":"thinking","model":...}` · `{"type":"tool","index":i,"call":{...},"ok":bool,"error":str|null}` ·
  `{"type":"gbrain","slug":...,"title":...}` · `{"type":"done","pass":bool,"model":...,"latency_s":...}`
- `GET /api/eval` → `rev/data/eval_results.json` `{"base_model","runs":[{"name":"base"|"tuned","label","passed","total","by_check":{id:pass_count}}],"examples":[...]}`
- `GET /api/gbrain/pages`, `GET /api/gbrain/page?slug=`, `GET /api/gbrain/search?q=`
- `POST /api/qm/refit` `{"model":"tuned"}` → synchronous run, returns `{"summary": str, "pass": bool, "checks": [...], "viewer_url": str}` (for QM)

## 9. GBrain (`rev/gbrain_io.py`) — least important; CLI at `~/.bun/bin/gbrain`, local PGLite brain, no embeddings
`put_page(slug, title, markdown)`, `get_page(slug)->str|None`, `search(q)->list[{"slug","title","snippet"}]`, `list_pages()`.
Must never raise into callers (log + return None/[] on failure). Seed: `sensor-hub` project page, `sensor-hub/rev-a-enclosure` design page.

## 10. THE WALL (`rev/fleet.py`, `rev/fleet_api.py`, `web/fleet.html`) — 48 boards refit in parallel
Default source (`FLEET_SOURCE` unset or `real`): `rev/data/real_fleet.json`, 48 real open-source KiCad boards with synthetic Rev B changes
(see REAL_DATA.md), results in `rev/data/fleet_results_real.json`. `FLEET_SOURCE=synthetic` uses the generated fleet below
(results in `rev/data/fleet_results.json`).
Data `rev/data/fleet.json` (`python3 -m rev.fleet build`, seed 4242, disjoint from train/test): 36 product-line tasks + 12 that ADD a `usb_a`
connector; each task = task row + `name` ("SH-01 Sensor Hub"), `code`, `family`, `usb_a`. Page: `GET /fleet` (also works as `web/fleet.html?mock=1`).
- `POST /api/fleet/run` `{"model":"base"|"tuned","key"?:"base"|"tuned"|"tuned_v2","cached"?:bool,"speed"?:float,"workers"?:16}`
  (`tuned_v2` samples `checkpoint_v2.json`; `cached` replays the last REAL run of that key from `rev/data/fleet_results.json`)
- `GET /api/fleet/events` SSE: `fleet_snapshot` on connect, then `{"type":"fleet_start","model","key","label","n","cached"}` ·
  `{"type":"fleet_cell","id","name","status":"running|pass|fail","failed":[check ids],"refs":[...],"latency_s","calls","errors","checks","enclosure","usb_a","cached"}` ·
  `{"type":"fleet_done","model","key","label","passed","total","seconds","cached"}` · `{"type":"fleet_error","error"}`
- `GET /api/fleet` → `{"n","labels","tasks":[...],"runs":{key:{passed,total,seconds,cells:{id:{...}}}},"running"}`
- CLI: `python3 -m rev.fleet run base|tuned [workers] [limit]` (env `FLEET_KEY=tuned_v2`).
