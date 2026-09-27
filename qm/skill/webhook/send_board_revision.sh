#!/usr/bin/env bash
# Fire the "board revision landed" webhook at QM, signed with QM's hmac-sha256 scheme.
#
#   export QM_WEBHOOK_URL='https://<prefix>-portal.fly.dev/v1/webhooks/incoming/<id>'
#   export QM_WEBHOOK_SECRET='<secret QM showed once>'
#   ./send_board_revision.sh [rev]        # default rev = B
#
# QM verifies header  x-signature: sha256=<hex HMAC-SHA256(secret, raw body)>
# (src/webhooks/verifiers.ts, scheme "hmac-sha256"). Delivery id = sha256(raw body), so an
# identical body is dropped as "duplicate"; the nonce below makes every send unique.
# Responses: 202 accepted (agent woken) | 200 "skipped" (filter miss) | 200 "duplicate"
#            | 401 bad signature | 404 unknown/disabled webhook.
set -euo pipefail
: "${QM_WEBHOOK_URL:?export QM_WEBHOOK_URL (the inbound URL QM returned)}"
: "${QM_WEBHOOK_SECRET:?export QM_WEBHOOK_SECRET (the secret QM returned once)}"

rev="${1:-B}"
nonce="$(date +%s)-$RANDOM"
body=$(printf '{"event":"board_revision_landed","project":"Sensor Hub","company":"Acme Devices","revision":"%s","previous_revision":"A","change_summary":"Rev %s: board 6 mm wider, USB-C moved 14 mm left","nonce":"%s"}' "$rev" "$rev" "$nonce")
sig="$(printf '%s' "$body" | openssl dgst -sha256 -hmac "$QM_WEBHOOK_SECRET" | sed 's/^.*= //')"

curl -sS -i -X POST "$QM_WEBHOOK_URL" \
  -H 'content-type: application/json' \
  -H "x-signature: sha256=${sig}" \
  --data-binary "$body"
echo
