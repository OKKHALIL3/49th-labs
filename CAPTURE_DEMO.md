# Capture demo: The 49th Engineer learns from a senior engineer's CAD save

The story: a senior engineer fixes one USB-A opening in the CAD file and saves it. The 49th Engineer diffs the save, turns it into an engineering action, checks it with the geometry checker, and writes it to GBrain as an engineering-change record. The learner pulls the verified captures back out of GBrain, derives the lesson, and River trains it into the company model. The USB-A cells on the 48-board wall then turn green. Nobody typed a rule.

## Before going on stage (once)

```bash
# from the repo root
python3 -m uvicorn rev.server:app --port 8000                              # leave running
curl -s -XPOST localhost:8000/api/learn/reset                            # tuned model back to checkpoint.json
curl -s -XPOST localhost:8000/api/capture/watcher/start -d '{}'          # watcher runs INSIDE the server
```

Run only one watcher. Don't also run `python3 -m rev.watcher`, or every save is captured twice.

## Stage steps

1. **Wall, before.** Open `http://localhost:8000/fleet` and run the tuned model (`POST /api/fleet/run {"model":"tuned"}`). The USB-A cells are red, because the company has no USB-A rule yet (on the 48 real boards: 42/48, and 5 of the 6 misses are USB-A).
2. **Check out the design.**
   `curl -s -XPOST localhost:8000/api/capture/checkout -d '{"open":true}'`
   This writes `workstation/ai-01-audio-interface.scad` and opens it in the editor. Only the `PARAMETERS` block is meant for editing.
3. **Edit two numbers.** In the J3 USB-A cut-out (connector body 13.14 x 5.72), set the opening to 0.5 mm per side:
   ```
   opening_J3_usb_a_w = 14.14;   // was 15.34
   opening_J3_usb_a_h = 6.72;    // was 7.92
   ```
4. **Save.** Within about 1 s, `/api/events` emits `{"type":"capture","record":{...}}`:
   - diff `w 15.34→14.14, h 7.92→6.72`
   - the call it produces: `place_opening(connector=J3, tolerance=0.5)`
   - `inferred_rule {"type":"usb_a","tolerance":0.5}`
   - `verified: true`, with 4 geometry checks passing (board fits, headroom, opening aligned, connector passes through with 0.50 x 0.50 mm per side)

   About 1 s after that, `{"type":"capture_gbrain","slug":"captures/<stamp>-ai-01-audio-interface-j3-usb-a-opening","ok":true}` confirms the record is in GBrain. `GET /api/captures` lists it, and `GET /api/gbrain/stats` shows `capture_records` go up by 1.
5. **Learn from GBrain.** Run `curl -s -XPOST localhost:8000/api/capture/learn -d '{"promote":true}'`. The first event is `pulled N verified captures from GBrain → lesson usb_a 0.5 mm`. It then verifies 48 generated examples with the checker, mixes them 50/50 with replayed history, continues the v1 LoRA on River for 8 steps, and evaluates on the 12 held-out USB-A changes before and after. The recorded run (`rev/data/capture_learn_last.json`) went from 0/12 to 12/12 in 87 s. It then writes a `lessons/usb_a-opening-tolerance` page to GBrain that links the evidence captures and the `river://` checkpoint. Add `"cached":true` to replay the last real run faster.
6. **Re-run the wall** with the promoted model: `POST /api/fleet/run {"model":"tuned","key":"tuned_v2"}` (or press `3` on `/fleet`). The USB-A cells turn green. The recorded real-board wave is 48/48.

## Reset between rehearsals

- `bin/49th reset` (or `POST /api/learn/reset`) puts the tuned model back to v1 (`checkpoint.json`).
- `POST /api/capture/checkout` writes a fresh file with a new baseline.
- The GBrain capture pages stay. They agree on 0.5, so the lesson still derives.

A save that breaks the geometry is recorded as `verified:false`, for example an opening smaller than the connector. It is written to GBrain but never learned. Captures that disagree by more than 0.05 mm are flagged as conflicts and are not learned either.
