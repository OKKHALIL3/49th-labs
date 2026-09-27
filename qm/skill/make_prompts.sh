#!/usr/bin/env bash
# Print the ready-to-paste QM chat messages with your tunnel URL filled in.
#   ./qm/skill/make_prompts.sh https://<random>.trycloudflare.com
# Also writes qm/skill/out/SKILL.md and qm/skill/out/tools/rev-refit/* with the URL baked in
# (use those if you install via the deployment directory instead of chat).
set -euo pipefail
url="${1:?usage: make_prompts.sh https://<random>.trycloudflare.com}"
url="${url%/}"
here="$(cd "$(dirname "$0")" && pwd)"
out="$here/out"
mkdir -p "$out/hardware-enclosure-engineer" "$out/tools/rev-refit" "$out/tools/rev-learn" "$out/tools/rev-pr"
sed "s#https://REPLACE-ME.trycloudflare.com#${url}#g" "$here/hardware-enclosure-engineer/SKILL.md" > "$out/hardware-enclosure-engineer/SKILL.md"
sed "s#https://REPLACE-ME.trycloudflare.com#${url}#g" "$here/tools/rev-refit/rev-refit" > "$out/tools/rev-refit/rev-refit"
chmod 0755 "$out/tools/rev-refit/rev-refit"
cp "$here/tools/rev-refit/tool.json" "$out/tools/rev-refit/tool.json"
sed "s#https://REPLACE-ME.trycloudflare.com#${url}#g" "$here/tools/rev-learn/rev-learn" > "$out/tools/rev-learn/rev-learn"
chmod 0755 "$out/tools/rev-learn/rev-learn"
cp "$here/tools/rev-learn/tool.json" "$out/tools/rev-learn/tool.json"
sed "s#https://REPLACE-ME.trycloudflare.com#${url}#g" "$here/tools/rev-pr/rev-pr" > "$out/tools/rev-pr/rev-pr"
chmod 0755 "$out/tools/rev-pr/rev-pr"
cp "$here/tools/rev-pr/tool.json" "$out/tools/rev-pr/tool.json"

cat <<EOF
==================== 1) PASTE INTO QM (DM with the bot, or the demo channel) ====================
Save a NEW skill in this conversation's scope (POST /v1/skills) with
name: hardware-enclosure-engineer
description: Refit the Acme Devices Sensor Hub enclosure when a new board revision lands; calls the REV server and posts summary, checks and 3D viewer link.
body: exactly the SKILL.md below (between the ---8<--- lines). Confirm when saved, then run step 1 of it once as a smoke test.
---8<---
$(cat "$out/hardware-enclosure-engineer/SKILL.md")
---8<---

==================== 2) PASTE INTO QM (in the channel/thread where results should post) ====================
Create an inbound webhook with the webhook tool: action=create, verification scheme "hmac-sha256"
(generate a strong random secret yourself), filters [{"path":"event","in":["board_revision_landed"]}],
no destinationKey (post here), and task:
"A new Sensor Hub board revision landed (payload below). Use the hardware-enclosure-engineer skill:
POST ${url}/api/qm/refit with {\"model\":\"tuned\"} and post the summary, every check and the viewer_url here."
Then give me the inbound url and the secret verbatim.

==================== 3) FIRE IT FROM YOUR LAPTOP ====================
export QM_WEBHOOK_URL='<url from step 2>'
export QM_WEBHOOK_SECRET='<secret from step 2>'
$here/webhook/send_board_revision.sh B
EOF
