# Video script: The 49th Engineer (about 2:00, recorded)

**Format:** 6 clips recorded separately, silent screen recordings plus a separate voiceover per clip, stitched by the lead with ffmpeg. Output is 1920x1080, 30 fps, 2:00 or less.

**Rules:**
- Say only numbers that are in the result files.
- Acme Devices is fictional. The video says so once (clip 2) and the end card says it again.
- Say "open model" or "generic model", never "generic AI". The generic baseline is Qwen, not a frontier model.
- Never show `.env`, API keys, a home-directory path, or a real person's name or email on screen.

## The 6 clips at a glance

| # | Clip | Screen | Target length |
|---|---|---|---|
| 1 | Hook | Terminal: `bin/49th eval` | 0:00-0:16 |
| 2 | Engineer view | `localhost:8000`: Rev B lands (red), refit (green) | 0:16-0:40 |
| 3 | Teach | Terminal: `bin/49th teach "..."`, sped up 5x in the edit | 0:40-1:02 |
| 4 | The wall | `localhost:8000/fleet`: v1 wave (red tiles), then the wave after the correction (all green), sped up 3x | 1:02-1:28 |
| 5 | QM refit | QM web UI `localhost:9081` (or Slack if [SLACK_STATUS] is live) | 1:28-1:48 |
| 6 | End card | Static PNG | 1:48-2:00 |

---

## Pre-flight (once, about 10 min before recording)

```bash
# terminal A, repo root: the server. Do NOT set FLEET_SOURCE: the default loads the 48 real boards,
# which is where the reported 16/48, 42/48 and 48/48 come from.
python3 -m uvicorn rev.server:app --port 8000

# terminal B, repo root: the recording terminal
PROMPT='$ '            # hides the user name and home path from the prompt
clear
bin/49th status        # expect: Acme Devices, acme-9b v1, GBrain 200 engineering records, QM agent running, server ok
bin/49th reset         # v1 weights, Rev A board
```

- **Terminal look:**
  - Set the font to about **20 pt** (Terminal: Cmd+, then Profiles, Text, Font; or iTerm2 Profiles, Text).
  - Use a dark theme and size the window to fill a 1920x1080 area, at least 110 columns wide so the tables don't wrap.
- **Browser look:**
  - Use Chrome with a clean profile (no avatar or personal bookmarks).
  - Hide the bookmarks bar (Cmd+Shift+B), use 100-110% zoom, and size the window to fill a 1920x1080 area.
- **Mac:**
  - Turn on Do Not Disturb (Control Center, Focus).
  - Hide the Dock (Opt+Cmd+D).
  - Quit Slack and Mail unless they're in the shot.
- **Warm the models:**
  - On `localhost:8000`, press `R`, `1`, `3` once, then `R`. The next tuned run takes about 5 s.
  - On `localhost:8000/fleet`, press `1` and wait for the generic wave to finish (16/48). The scoreboard now already shows wave 1, so the video doesn't wait for it. River sampling isn't bit-deterministic, so if a live wave doesn't land on 16/48, press `P` (Replay recorded runs) and press `1` again, so the screen matches `rev/data/fleet_results_real.json`.
- **QM:** open `localhost:9081` and sign in (see `qm/LOCAL_RUN.md`, section 3). The tunnel and `rev-refit` must be working. Run one test refit and check that it says PASS 5/5.
- **End card:** make one 1920x1080 PNG (Keynote or Figma, exported as `endcard.png`) with this text:
  - **49th Labs · The 49th Engineer**
  - *Your team's 49th engineer. Trained on your other 48.*
  - QM: the teammate · River: weights you own · GBrain: facts and change records · Checker: grading and the learning gate
  - small print: "Acme Devices is a fictional company. Board geometry: 48 real open-source KiCad designs (attribution in REAL_DATA.md)."
  - `[GITHUB_URL]`

## Recording setup (every clip)

1. Press **Cmd+Shift+5** and choose **Record Selected Portion**. Drag the box over the 1920x1080 window. On a Retina Mac this records at 2x, which the ffmpeg step scales down.
2. Under **Options**:
   - Set **Microphone** to **None** (the voiceover is recorded separately).
   - Turn on **Show Mouse Clicks**.
   - Set **Save to** a `49th-clips` folder on the Desktop.
   - Set **Timer** to 5 seconds.
3. Click **Record**. Stop with the stop button in the menu bar or Cmd+Ctrl+Esc.
4. Rename each file as soon as it's saved: `clip1.mov`, `clip2.mov`, `clip3.mov`, `clip4a.mov`, `clip4b.mov`, `clip5.mov`.
5. **Before every take**, run `bin/49th reset`. It restores the v1 weights and the Rev A board.
6. Leave 1 s of stillness at the start and end of each clip so the edit has room.

**Recording order.** The teach step changes the weights, so the order you record in differs from the order the clips play in:

| Step | Record | Why |
|---|---|---|
| 1 | `bin/49th reset` | clean state, v1 weights |
| 2 | clip 1 (eval) | reads result files only |
| 3 | clip 2 (engineer view) | uses v1 |
| 4 | clip 4a (wall, v1 wave) | must run before the correction |
| 5 | clip 3 (teach) | trains and promotes v2 |
| 6 | clip 4b (wall, after the correction) | uses v2 |
| 7 | clip 5 (QM refit) | works on v1 or v2 |

---

## Clip 1: hook, `bin/49th eval` (0:00-0:16)

**Screen:** terminal B, cleared. Type slowly, then press Enter:

```bash
bin/49th eval
```

The table prints the generic rows in red (397B 1/30, 122B 1/30, 35B 0/30, 9B 0/30) and the Acme-tuned rows in green (the 397B row is marked partial). Hold 4 s on the finished table. Move the mouse to point at the `Qwen3.5-397B` row (1/30), then at `acme-9b` (29/30).

**Voiceover (vo1):**
> "Thirty engineering changes at a hardware company. A 397-billion-parameter open model gets one right. A 9-billion model we trained on the company's own history, in seventeen minutes, gets twenty-nine."

## Clip 2: engineer view, Rev B from red to green (0:16-0:40)

**Screen:** browser at `http://localhost:8000`.

| Key | What happens |
|---|---|
| `R` | Rev A, all checks green. Hold 1 s |
| `1` | The Rev B board drops into the Rev A enclosure and the checks go red. Hold 3 s |
| `3` | The Acme-tuned model's tool calls stream in, the geometry regenerates, and every check goes green. Hold 3 s on 5/5 |

Optional: drag the 3D view to orbit slowly while the checks are green.

**Voiceover (vo2):**
> "Here's the job at Acme Devices, our fictional demo company. The Sensor Hub board just changed: six millimeters wider, USB-C moved, an RJ45 added. The old enclosure goes red. The 49th Engineer calls Acme's own model on River, and a deterministic checker grades it. Five of five, in about five seconds."

## Clip 3: teach, `bin/49th teach` (0:40-1:02, sped up 5x in the edit)

**Screen:** terminal B, cleared. Type the command and press Enter:

```bash
bin/49th teach "Our USB-A cutouts always get 0.5 mm per side."
```

Keep recording through the whole live run, which takes about 90 to 110 s. The steps fill in one at a time: the rule is understood, 48 practice changes are verified by the checker, 48 past changes are mixed in, River training runs 8 steps with a loss sparkline, the USB-A held-out score goes from 0/12 to 12/12, and the new weights are promoted. Hold 3 s on the finished panel.

**Voiceover (vo3):**
> "Acme has never shipped USB-A, so its model gets those cutouts wrong. An engineer fixes it with one sentence. That becomes forty-eight practice changes, each checked, mixed half and half with past work so nothing is forgotten. Eight steps on River. USB-A: zero of twelve, to twelve of twelve."

**Re-take:** run `bin/49th reset`, then `bin/49th teach --replay "Our USB-A cutouts always get 0.5 mm per side."`. This replays the last real run in about 20 s and promotes its checkpoint. The CLI marks it as a replay. For the final cut, prefer a live take.

## Clip 4: the wall, red to green (1:02-1:28, two parts, 3x in the edit)

**Screen:** browser at `http://localhost:8000/fleet`. Wave 1 was pre-run, so it shows 16/48.

- **4a (record before clip 3):** press `2` ("Acme's own model"). 48 tiles refit in parallel in about 30 s. The scoreboard stops at **42/48**. Hold 3 s so the red tiles are readable. Five of the six red tiles are USB-A boards.
- **4b (record after clip 3):** press `3` (the wave after the correction). The USB-A tiles turn green, and the scoreboard stops at **48/48**. Hold 3 s.

**Voiceover (vo4):**
> "Now a whole product line: forty-eight real open-source circuit boards, refit in parallel. The generic model passes sixteen. Acme's model passes forty-two, and five of the six misses are USB-A. Same boards, with the weights from that one sentence: forty-eight of forty-eight."

**Re-take:** press `P` to tick "Replay recorded runs", then press `2` or `3`. This replays the real recorded wave with its original timing, and the status pill says so. **If a live wave changes a number, the docs must match `rev/data/fleet_results_real.json`.** Otherwise, use the replay in the final cut.

## Clip 5: QM refit (1:28-1:48; speed up 2x if the agent is slow)

**Screen:** QM web UI at `http://localhost:9081`, in a new chat. If [SLACK_STATUS] is live, use the Slack channel instead and crop out the workspace sidebar. Type and send:

```
Rev B landed — refit the enclosure
```

The agent runs `rev-refit` and replies with PASS 5/5, one line per check, and the 3D viewer link. Hold 3 s on the reply.

**Voiceover (vo5):**
> "And it lives where the team works. In QM, an engineer types: Rev B landed, refit the enclosure. The agent runs Acme's model and posts the checker's verdict. Pass, five of five, with a 3D link."

**Fallback if QM doesn't answer:** record `bin/49th refit B` in the terminal. It calls the same `/api/qm/refit` endpoint that QM's tool calls. Change the voiceover to: "The same refit QM runs, from the terminal: pass, five of five."

## Clip 6: end card (1:48-2:00)

**Screen:** `endcard.png` (see pre-flight), 12 s.

**Voiceover (vo6):**
> "GBrain remembers what's true. River holds weights your company owns. The checker grades every change. 49th Labs: your team's 49th engineer, trained on your other 48."

---

## Voiceover recording

- Record each line as its own file in QuickTime (File, New Audio Recording), named `vo1.m4a` to `vo6.m4a`.
- Sit about 20 cm from the mic in a quiet room. Do one 5 s test first and listen for fan noise and clipping.
- Read at a normal pace. Each line must be **shorter than its clip** after speed-up. The ffmpeg step pads the audio to the video length and cuts anything past the video's end.

## Stitching (lead, ffmpeg)

Run these from the `49th-clips` folder. All intermediate files go into `build/`.

```bash
mkdir -p build

# 1. Normalize: speed factor, 1920x1080 letterboxed, 30 fps, H.264, no audio.
#    tpad holds the last frame 2 s longer, which gives the voiceover some room.
norm() {  # norm <in> <speed> <out>
  ffmpeg -y -i "$1" -an -vf "setpts=PTS/$2,scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,fps=30,tpad=stop_mode=clone:stop_duration=2,format=yuv420p" \
    -c:v libx264 -crf 18 -preset medium "$3"
}
norm clip1.mov  1 build/v1.mp4
norm clip2.mov  1 build/v2.mp4
norm clip3.mov  5 build/v3.mp4     # teach: 5x
norm clip4a.mov 3 build/v4a.mp4    # wall waves: 3x
norm clip4b.mov 3 build/v4b.mp4
norm clip5.mov  2 build/v5.mp4     # QM: use 1 if the reply came quickly
ffmpeg -y -loop 1 -i endcard.png -t 12 -vf "scale=1920:1080,fps=30,format=yuv420p" -c:v libx264 -crf 18 build/v6.mp4

# 2. Join the two wall parts.
printf "file 'v4a.mp4'\nfile 'v4b.mp4'\n" > build/wall.txt
ffmpeg -y -f concat -safe 0 -i build/wall.txt -c copy build/v4.mp4

# 3. Optional speed labels. Needs drawtext: check with `ffmpeg -filters | grep drawtext`.
#    Otherwise, say "sped up" in the voiceover or add the label in iMovie.
label() {  # label <in> <text> <out>
  ffmpeg -y -i "$1" -vf "drawtext=text='$2':fontfile=/System/Library/Fonts/Supplemental/Arial.ttf:fontsize=36:fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=12:x=w-tw-40:y=40" \
    -c:v libx264 -crf 18 "$3"
}
label build/v3.mp4 "5x speed" build/v3l.mp4 && mv build/v3l.mp4 build/v3.mp4
label build/v4.mp4 "3x speed" build/v4l.mp4 && mv build/v4l.mp4 build/v4.mp4

# 4. Check that every voiceover is shorter than its video.
for i in 1 2 3 4 5 6; do
  printf "clip %s  video %s  vo %s\n" $i \
    "$(ffprobe -v error -show_entries format=duration -of csv=p=0 build/v$i.mp4)" \
    "$(ffprobe -v error -show_entries format=duration -of csv=p=0 vo$i.m4a)"
done

# 5. Add each voiceover to its clip (the audio is padded to the video length).
for i in 1 2 3 4 5 6; do
  ffmpeg -y -i build/v$i.mp4 -i vo$i.m4a -filter_complex "[1:a]aresample=48000,apad[a]" \
    -map 0:v -map "[a]" -c:v copy -c:a aac -b:a 192k -ac 2 -shortest build/c$i.mp4
done

# 6. Concatenate and check the total length (it must be 120 s or less).
printf "file 'c%s.mp4'\n" 1 2 3 4 5 6 > build/list.txt
ffmpeg -y -f concat -safe 0 -i build/list.txt -c copy 49th-engineer.mp4
ffprobe -v error -show_entries format=duration -of csv=p=0 49th-engineer.mp4
```

**If the video runs over 2:00:**
- Trim the holds with `-ss` / `-t` on the longest clip.
- Or raise the speed factor on clip 3 (to 6x) or clip 4 (to 4x).

**Final checks:**
- Watch the full video once for private info: a user name in a prompt, an email address, a Slack sidebar.
- Upload it unlisted (YouTube or Loom) and paste the link into `SUBMISSION.md` as `[VIDEO_URL]`.

## Fallbacks (every one of them replays or re-runs something real)

| Problem | Fix |
|---|---|
| A tuned run hangs on `localhost:8000` | Tick the **cached** checkbox and press `3` again. It replays the last real response for that exact scenario |
| A wall wave errors | Press `P` (Replay recorded runs), then press `2` or `3`. The page also falls back to the recorded run on its own |
| The teach run fails or runs long | `bin/49th reset`, then `bin/49th teach --replay "..."` |
| QM doesn't answer | `bin/49th refit B` in the terminal, with the fallback voiceover from clip 5 |
| The 3D view is blank (CDN unreachable) | Keep going. The checks and tool calls still update |
