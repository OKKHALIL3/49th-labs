# QM running locally (Docker) with the REV skill

Deploy dir: `../qm-local` (its own git repo; `.env` holds secrets, mode 600, gitignored).
Web UI: **http://localhost:9081** (portal front door). Admin: http://localhost:9081/admin

---

## 1. SLACK — do this first, in parallel (about 5 min, needs a personal/test Slack workspace)

The bot uses **Socket Mode**, so no public URL is needed for Slack.

1. Open the manifest link. Get it with:
   ```bash
   cd ../qm-local && export PATH="/opt/homebrew/opt/node@24/bin:$PATH"
   npm exec qm -- outputs | grep 'Slack app'
   ```
   Or go to https://api.slack.com/apps, click **Create New App**, choose **From an app manifest**, pick
   your test workspace, and paste the contents of `qm-local/slack-app-manifest.yml`. Click **Create**.
2. Go to **Install App** (left sidebar), click **Install to Workspace**, then **Allow**. Copy the
   **Bot User OAuth Token** (`xoxb-...`).
3. Go to **Basic Information**, then **App-Level Tokens**, and click **Generate Token and Scopes**. Name it `socket`,
   add the scope **`connections:write`**, and click **Generate**. Copy the token (`xapp-...`).
4. Give the tokens to QM with **either** method:
   - **A, Admin card (no restart):** sign in (section 3), open http://localhost:9081/admin, find the
     **Slack** card, paste the xoxb and xapp tokens, and save.
   - **B, .env:** add these two lines to `qm-local/.env`, then run `npm exec qm -- up` again:
     ```
     SLACK_BOT_TOKEN=xoxb-...
     SLACK_APP_TOKEN=xapp-...
     ```
5. In Slack, create a channel (e.g. `#sensor-hub`) and run `/invite @qm`. Mention it in a message:
   `@qm Rev B landed — refit the enclosure`. You can also DM the bot (Messages tab).
   You can rename the bot later in Admin > Branding.

Never paste the tokens into chat, commits, or this repo.

---

## 2. (Re)start everything

```bash
# 0. Docker Desktop
open -a Docker; until docker info >/dev/null 2>&1; do sleep 2; done

# 1. REV server on :8000 (repo root). Another agent may already run it.
curl -s localhost:8000/api/health

# 2. Public tunnel to REV (the QM agent computer calls this URL)
cd ../qm-local
pkill -f "cloudflared tunnel --url http://localhost:8000" ; nohup cloudflared tunnel --url http://localhost:8000 > tunnel.log 2>&1 &
sleep 8; URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' tunnel.log | head -1); echo $URL
curl -s $URL/api/health            # expect {"ok":true,...}

# 3. If the URL changed: reinstall the skill and tools with the new URL
./qm/skill/install_into_deploy_dir.sh \
  ../qm-local "$URL"

# 4. QM up (Node 24 required; every `up` re-syncs sandbox/skills + sandbox/tools)
export PATH="/opt/homebrew/opt/node@24/bin:$PATH"
npm exec qm -- check && npm exec qm -- up
npm exec qm -- status        # logs: npm exec qm -- logs core ; stop: npm exec qm -- down
```

Ports: core 9080, portal 9081 (the one to open), web-ui 9082, admin 9083. 8080 is taken on this Mac,
so `basePort` is set to 9080 in `qm.config.jsonc`.

After **every** `qm up` that recreates core, run these two local workarounds (both are idempotent):

```bash
# a) core runs as a non-root user; open the Docker Desktop socket so core can start agent computers
docker exec -u root qm-acme-rev-core chmod 666 /var/run/docker.sock
# b) the shim lives in core's persistent /data volume (LOCAL_SANDBOX_DOCKER_BIN in qm.config.jsonc).
#    Re-copy it only if the core data volume was deleted (e.g. `qm down --purge`):
docker exec -u root qm-acme-rev-core sh -c 'mkdir -p /data/shim && chown node /data/shim'
docker cp core-shim/qm-docker-shim qm-acme-rev-core:/data/shim/ && docker cp core-shim/fwd.js qm-acme-rev-core:/data/shim/
docker exec -u root qm-acme-rev-core sh -c 'chown -R node /data/shim && chmod 755 /data/shim/qm-docker-shim'
```

If the tunnel URL changed, re-bake the REV CLIs into the agent-computer image too. The local sandbox backend does not
install `sandbox/tools` files, so the image carries them:

```bash
cp sandbox/tools/rev-refit/rev-refit sandbox/tools/rev-learn/rev-learn sandbox/tools/rev-pr/rev-pr sandbox-image/
docker build --platform linux/amd64 -t qm-acme-rev-sandbox-local:latest sandbox-image
docker rm -f $(docker ps -aq --filter label=qm.sandbox=1) 2>/dev/null   # next turn recreates it with the new image
```

Copies of the shim, the forwarder and the Dockerfile are kept in `hackathon/qm/local-docker/`.

---

## 3. Sign in to the web UI (single-use link, valid 5 min)

```bash
cd ../qm-local && export PATH="/opt/homebrew/opt/node@24/bin:$PATH"
npm exec qm -- admin-login        # prints http://localhost:9081/auth/admin-login#token=...
```

Open the link in the browser and click **Sign in**. You are the local admin, `admin@example.com`
(no email is needed). Chat is at http://localhost:9081 and Admin (including the Slack card) is at http://localhost:9081/admin.
The session cookie lasts, so you need to repeat this only in a fresh browser profile.

---

## 4. Demo prompts (web UI chat, or `@49th Engineer ...` in Slack)

1. `Rev B landed — refit the enclosure`
   The agent runs `rev-refit --model tuned`, which calls `POST {tunnel}/api/qm/refit`. It posts PASS/FAIL, each check,
   and the 3D viewer link. Verified end to end at 15:16: PASS 5/5, with the viewer link returned.
2. `Our USB-A cutouts always get 0.5 mm per side`
   The agent runs `rev-learn "<text>"`, which calls `POST {tunnel}/api/learn` and then polls `/api/learn/status`
   (up to about 4 min). It posts the held-out before/after score and says whether the weights were promoted.
   To un-promote between rehearsals: `curl -X POST localhost:8000/api/learn/reset`.

3. `Heads up: USB-A cutouts get 0.5 mm per side — tighter ones cracked in drop tests.` (sent by a senior engineer)
   The agent runs `rev-pr --author "<role>" "<text>"`, which calls `POST {tunnel}/api/pr` (source `qm`) and polls
   `/api/prs`. It replies with one weight-PR line: GBrain ✓ · conflict check ✓ · trained on River (K steps) ·
   tests: lesson a→b, regression x/30 · MERGED → v2. About 165 s live. Add "replay" to the message for the recorded run.
4. `HDMI cutouts should be 0.2 mm per side from now on.` (sent by a junior engineer)
   This is blocked in about 2 s: it contradicts 34 verified changes in GBrain (they used 0.6 mm), with the evidence slugs.
   To reset between rehearsals: `curl -X POST localhost:8000/api/ledger/reset` (production goes back to v1).
   Terminal views: `python3 -m rev_cli_ci log | prs | blame usb_a | pr "<text>" --author "<role>" [--replay]`.

Branding: the bot is **49th Engineer** and the org is **Acme Devices** (`botName`/`orgName` in `qm.config.jsonc`).

---

## 5. What is where / troubleshooting

- Config: `qm-local/qm.config.jsonc`. It has services core, slack, web-ui, admin, portal and auth, `basePort` 9080,
  `sandbox.backend: local`, `HARNESS=pi`, and model provider anthropic.
- Secrets: `qm-local/.env` (mode 600). `ANTHROPIC_API_KEY` is filled in. `PUBLIC_API_URL=http://qm-acme-rev-core:8080`.
  Sign-in email is disabled (SMTP is unset), so use `qm admin-login`.
- Logs: `npm exec qm -- logs core`, `docker logs qm-acme-rev-portal`, and the agent computer `docker ps | grep qm-sbx`.
- If the agent says **"exec service refusing connections / computer down"**: redo workarounds (a) and (b) above.
  Check that `LOCAL_SANDBOX_DOCKER_BIN` is present with `docker exec qm-acme-rev-core printenv LOCAL_SANDBOX_DOCKER_BIN`.
  The root cause is that the core image puts `QM_CORE_CONTAINER` into the AWS sandbox config instead of the local one,
  so core dials `127.0.0.1:<port>`. The shim answers `docker port` with a forwarder that reaches the sandbox over its private network.
- If the agent says **"REV server unreachable"**: check `curl $URL/api/health` and restart the tunnel (section 2).
- Images are amd64 only and run under emulation on this arm64 Mac. The first turn in a new scope takes about 30 to 60 s.
