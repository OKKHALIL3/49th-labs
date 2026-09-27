# QM integration guide for REV

Source of truth: `vendor/qm` (a shallow clone of github.com/yc-software/qm). Every claim below
cites the file it came from. Anything I could not confirm in source is marked **UNVERIFIED**.

Company and project names are fixed as "Acme Devices" / "Sensor Hub". Use role names for
people. Never paste API keys or webhook secrets into chat logs, commits, or this repo.

---

## 0. Before you start (5 min)

| Need | Status on this laptop | Fix |
|---|---|---|
| Node **24+** (`cli/package.json` `engines.node >=24.0.0`) | **v23.7.0, too old** | `brew install node@24 && export PATH="/opt/homebrew/opt/node@24/bin:$PATH"` |
| Docker with Buildx | present (buildx 0.20.1) | none |
| `flyctl` (Fly target) | **missing** | `brew install flyctl && fly auth login` |
| `cloudflared` | installed (2026.9.3) | none |
| Model key | Anthropic, OpenAI, or OpenRouter | goes in the deploy dir's `.env` only |

**Fastest option of all:** ask the QM table whether a hosted hackathon instance exists. The QM
README also links a third-party hosted QM (agent37.com/qm). If either one works, skip to step 3.

---

## 1. Deploy QM (Fly.io, recommended by `cli/templates/deployment/deployment.md`)

Work in a **separate folder**, not this repo. The deploy dir gets its own `.env` with secrets.

```bash
mkdir -p ~/qm-acme && cd ~/qm-acme && git init
npm exec --yes --package=@yc-software/qm@latest -- \
  qm init . --org acme-rev --target fly --model-provider anthropic
npm install
```

- `--org` is the slug. On Fly it becomes `appPrefix`, so apps are named `<prefix>-core`,
  `<prefix>-portal` and so on. The names must be free on fly.dev; if one collides, set a more
  distinctive `appPrefix` in `qm.config.jsonc`.
- `publicUrl` defaults to `https://<prefix>-portal.fly.dev` (`cli/src/provider-scaffold.ts`).
- The default services are `core, slack, web-ui, admin, portal, auth`. If you want to go
  faster without Slack, remove `"slack"` from `services`.

Edit `qm.config.jsonc` to set `flyOrg` (usually `"personal"`), `region` (e.g. `"sjc"`),
`appPrefix` and `sandbox.app`. Then fill `.env` (it must be mode 600 and gitignored):

```
ADMIN_GRANTS=<your-work-email-lowercased>:org_admin
ANTHROPIC_API_KEY=...            # the model provider key is a REQUIRED secret
```

Then run the commands in `cli/templates/deployment/references/fly.md`:

```bash
fly auth whoami && fly orgs list
fly apps create <sandbox-app> --org <fly-org>   # create the sandbox app BEFORE setup
npm exec qm -- setup .       # interactive; SKIP email: you'll log in with admin-login
npm exec qm -- check
npm exec qm -- secrets push
npm exec qm -- plan
npm exec qm -- up            # creates or reuses Tigris storage + Managed Postgres
npm exec qm -- doctor
npm exec qm -- check --live  # real agent-turn canary
npm exec qm -- admin-login   # prints a single-use admin login URL (5 min). Don't share it.
npm exec qm -- outputs --json   # webUiUrl, adminOnboardingUrl, Slack manifest links
```

- **Email is not needed.** `qm admin-login` signs the admin in without Resend or SMTP
  (`cli/README.md` § "Administrator login without email").
- **Billing (Fly):** a core VM (2 CPU, 4 GB), the other service VMs, Managed Postgres,
  Tigris storage, and sandbox machines.
- **Time:** **UNVERIFIED**. I estimate 20 to 40 min; Managed Postgres provisioning is the
  slowest part. Images are prebuilt and pinned by digest, so nothing compiles.
- **Useful commands:** `npm exec qm -- status`, `npm exec qm -- logs core --follow`, and
  `npm exec qm -- down`. `down` stops the apps but does not delete Postgres or storage.

**Slack bot (optional, `references/slack.md`):** run `npm exec qm -- slack render`, then
`npm exec qm -- outputs`. Open the bot manifest URL and create the app, which is Socket Mode.
Install it and create an app-level token with the `connections:write` scope. Paste the bot
token (xoxb) and app token (xapp) into **Admin > Slack card**, invite the bot to a channel,
and @mention it. Without Slack, the web UI (`webUiUrl`) is enough for the demo.

**AWS instead:** use `--target aws`. It needs a 12-digit account ID, a deploy role, Terraform
(`infra/`), and `npm exec qm -- infra build-image` before `plan`, and `up --yes`. It is much
slower. Don't use it at a hackathon.

### Local mode (exists; useful only as a fallback)

`qm init . --org acme-rev --target docker` runs everything in local containers. The scaffold
gives `publicUrl: http://localhost:8082` and `services: ["core","web-ui"]`
(`cli/src/provider-scaffold.ts`). Agent computers also run locally only when you add
`"sandbox": { "backend": "local" }` (`cli/src/config.ts` `localSandboxActive`). Then run
`npm exec qm -- up`. The deploy guide calls this "a quick local test drive only".
**UNVERIFIED:** how sign-in works on the docker web UI without the portal.

---

## 2. Expose the REV server publicly (QM on Fly cannot reach your laptop)

```bash
# terminal 1 (repo root)
/opt/anaconda3/bin/python3 -m uvicorn rev.server:app --port 8000
# terminal 2
cloudflared tunnel --url http://localhost:8000
# prints https://<random-words>.trycloudflare.com   <- this is PUBLIC_URL
curl -sS -X POST https://<random>.trycloudflare.com/api/qm/refit \
  -H 'content-type: application/json' -d '{"model":"tuned"}'   # must return summary/pass/checks/viewer_url
```

- The quick-tunnel URL **changes every time cloudflared restarts**. Keep that terminal open,
  and if it does restart, redo step 3 with the new URL.
- **Timeout:** Cloudflare cuts proxied requests at about 100 s (HTTP 524). This is general
  Cloudflare behaviour, **UNVERIFIED** for quick tunnels specifically. `POST /api/qm/refit`
  must finish in under about 90 s, or the server needs to return early.
- The server should build `viewer_url` from the tunnel URL (for example a `PUBLIC_URL` env
  var), not from `localhost`, because people open that link from Slack.
- **Egress policy:** QM's default posture, "Auto", blocks only **private-network** addresses
  (`src/security/security-posture.ts`: `auto.denyPrivateNetworks: true`). A public
  trycloudflare.com URL is allowed. `tool.json` `egress` entries are "validated-only; no
  runtime enforcement" (`docs/deploy-directory.md`), so there is no allowlist to register.
  **UNVERIFIED:** whether the Fly Sprites sandbox has an outbound proxy. If the agent's curl
  fails, ask it to run `curl -sS https://<tunnel>/api/state` in its sandbox.

---

## 3. Give the QM agent the `hardware-enclosure-engineer` skill

QM skills are `SKILL.md` files. The frontmatter needs `name` and `description`
(`docs/deploy-directory.md` § Tool descriptors). A name must match
`^[A-Za-z0-9][A-Za-z0-9_.-]*` (`src/skills/skill-name.ts`). The agent runs shell commands in
its sandbox; the Fly sandbox image includes `curl`, `jq` and `python3` (`fly/Dockerfile`).
**Outbound auth:** our endpoint needs none. For a real secret, put its name in
`sandbox.secretEnv` in `qm.config.jsonc`; it is forwarded to every sandbox as an env var.
`rev-refit` sends `REV_TOKEN` as a Bearer token if that variable is set. The alternative is
an admin-vended shared credential that the agent calls through
`$AGENT_API_URL/v1/credentials/broker` (`skills-seed/use-shared-credential/SKILL.md`).

Fill in the URL once:

```bash
./qm/skill/make_prompts.sh https://<random>.trycloudflare.com
```

The script prints three paste-ready blocks and writes `qm/skill/out/` with the URL baked in.

**Option A: through chat (fastest, no redeploy).** Paste block 1 into a DM with the QM
agent, or into the demo channel. The agent saves the skill with `POST /v1/skills
{name, description, body}` (`src/api/agent-api-catalog.ts`; `createSkill` in
`src/api/routes/surface.ts`). The skill lives in *that conversation's scope*. In a DM it is
yours only. In a private channel it belongs to the room. A public channel keeps it
owner-only. The skill does not need the `rev-refit` binary, because its body includes the
curl fallback.

**Option B: through the deployment directory (org-wide, installs the `rev-refit` CLI).**

```bash
./qm/skill/install_into_deploy_dir.sh ~/qm-acme https://<random>.trycloudflare.com
cd ~/qm-acme && npm exec qm -- check && npm exec qm -- up
```

This writes `sandbox/skills/hardware-enclosure-engineer/SKILL.md` and
`sandbox/tools/rev-refit/{tool.json,rev-refit}`. Every `qm up` then pushes them through
`PUT /v1/deployment-layer`. The `install.files` entry puts the script at
`/usr/local/bin/rev-refit` on every sandbox backend (`docs/deploy-directory.md`). I checked
`tool.json` and the layer with QM's own `parseToolDescriptor` and `validateSandboxLayer`:
**0 errors, 0 warnings**. The catch: a new tunnel URL means re-running both commands.

**Test it:** in the thread, send "@bot Rev B of the Sensor Hub board just landed, refit the
enclosure." The agent should post the PASS/FAIL line, one bullet per check, and
`3D viewer: <viewer_url>`.

---

## 4. Webhook "board revision landed" that wakes the agent

These details come from `src/webhooks/{verifiers,webhook-receiver}.ts`,
`src/api/routes/webhooks.ts` and `plugins/portal/src/index.ts`.

- **Create:** paste block 2 from `make_prompts.sh` **in the channel or thread where results
  should appear**. The agent's built-in `webhook` tool is called with
  `action=create, task, verification{scheme, secret}, filters?, destinationKey?`. With no
  `destinationKey`, results go to the conversation's default destination. **UNVERIFIED:**
  whether that means the same Slack thread or the top of the channel. You can also create
  webhooks from the web UI's **Webhooks** sidebar view (`plugins/web-ui/README.md`).
- **Returned once:** the inbound URL and the secret. The URL format is
  `https://<prefix>-portal.fly.dev/v1/webhooks/incoming/<id>`. The portal passes
  `POST /v1/webhooks/incoming/:id` straight through to core without auth; the per-webhook
  signature is the auth.
- **Signature schemes:** `github` (`x-hub-signature-256`), `slack`, `stripe`, `linear`, and
  `hmac-sha256`. For `hmac-sha256` the header is
  `x-signature: sha256=<hex HMAC-SHA256(secret, raw body)>`, and the `sha256=` prefix is
  optional. Use this scheme.
- **Filters:** `[{"path":"event","in":["board_revision_landed"]}]`. `path` is a dotted path
  into the JSON body, and the event passes if `String(value)` appears in `in`. A miss returns
  `200 "skipped"` and no turn runs.
- **Dedup:** the delivery id is the SHA-256 of the raw body, so an identical body gets
  `200 "duplicate"`. Our sender adds a `nonce` so repeated demos still fire.
- **Wake-up:** a matching event returns `202` immediately. QM then runs one agent turn with a
  `<wake ... surface="webhook">` envelope containing your `task` text plus the payload
  (capped at 16,000 characters).
- **Other responses:** `401` means the signature is bad; `404` means the webhook is unknown
  or disabled.

Send it:

```bash
export QM_WEBHOOK_URL='https://<prefix>-portal.fly.dev/v1/webhooks/incoming/<id>'
export QM_WEBHOOK_SECRET='<secret>'          # keep out of git
./qm/skill/webhook/send_board_revision.sh B  # expect: HTTP/1.1 202
```

The same request as raw curl:

```bash
BODY='{"event":"board_revision_landed","project":"Sensor Hub","revision":"B","nonce":"'$(date +%s)'"}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$QM_WEBHOOK_SECRET" | sed 's/^.*= //')
curl -i -X POST "$QM_WEBHOOK_URL" -H 'content-type: application/json' -H "x-signature: sha256=$SIG" --data-binary "$BODY"
```

I checked the signature from this script against QM's own `getVerifier("hmac-sha256")`, and
it returns `true`.

---

## 5. Publish the 3D viewer as a QM app (optional)

The source is `skills-seed/publish/SKILL.md` and the `apps` tool in
`src/harness/agent-tools.ts`. The agent publishes a directory from its workspace:

```
apps({ action: "publish", dir: "rev-viewer-app", entrypoint: "node server.js", name: "rev-viewer",
       env: { REV_PUBLIC_URL: "https://<random>.trycloudflare.com" } })
```

- It returns `{id, name, version, url}`, where the URL is
  `<public web url>/d/<name>/`, for example `https://<prefix>-portal.fly.dev/d/rev-viewer/`.
- The app is private by default. Add `public: true` to make it open to anyone with the link,
  or share it with `apps({action:"share", id:"rev-viewer", toScope:"org", permission:"read"})`.
- The app must listen on `$PORT`.
- Our app is `qm/skill/rev-viewer-app/server.js`: one file with no dependencies. It frames
  the live viewer and has a full-screen link. To get it into the agent's workspace, ask the
  agent to write that file. It is short enough to paste.
- **UNVERIFIED:** whether this deployment's app runtime is enabled and has `node`. Linking
  `viewer_url` in the thread is the zero-risk fallback.

---

## Files in `qm/skill/`

| File | What it is |
|---|---|
| `hardware-enclosure-engineer/SKILL.md` | The skill. It uses `REPLACE-ME` as a placeholder, which `make_prompts.sh` fills in. |
| `tools/rev-refit/tool.json` + `rev-refit` | Deployment-layer CLI: `rev-refit [--model tuned\|base] [--url URL] [--json]`. It prints the text for the thread. |
| `make_prompts.sh <PUBLIC_URL>` | Prints the chat blocks for the skill, the webhook and the sender, and writes `out/` with the URL filled in. |
| `install_into_deploy_dir.sh <deploy-dir> <PUBLIC_URL>` | Copies the skill and tool into `<deploy-dir>/sandbox/`. |
| `webhook/send_board_revision.sh [rev]` | Sends the signed "board revision landed" event. |
| `rev-viewer-app/server.js` | The QM-publishable viewer app. |

Local check without QM: `REV_PUBLIC_URL=http://localhost:8000 ./qm/skill/tools/rev-refit/rev-refit`.
