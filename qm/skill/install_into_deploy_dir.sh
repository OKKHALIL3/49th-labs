#!/usr/bin/env bash
# Copy the skill + tool into a QM deployment directory (the one made by `qm init`), org-wide.
#   ./qm/skill/install_into_deploy_dir.sh <deploy-dir> https://<random>.trycloudflare.com
# Then in <deploy-dir>:  npm exec qm -- check  &&  npm exec qm -- up
# (every `up` syncs sandbox/skills + sandbox/tools to core via PUT /v1/deployment-layer).
set -euo pipefail
dest="${1:?usage: install_into_deploy_dir.sh <deploy-dir> <public-url>}"
url="${2:?usage: install_into_deploy_dir.sh <deploy-dir> <public-url>}"
here="$(cd "$(dirname "$0")" && pwd)"
[ -f "$dest/qm.config.jsonc" ] || { echo "no qm.config.jsonc in $dest" >&2; exit 1; }
"$here/make_prompts.sh" "$url" >/dev/null
mkdir -p "$dest/sandbox/skills/hardware-enclosure-engineer" "$dest/sandbox/tools/rev-refit" "$dest/sandbox/tools/rev-learn" "$dest/sandbox/tools/rev-pr"
cp "$here/out/hardware-enclosure-engineer/SKILL.md" "$dest/sandbox/skills/hardware-enclosure-engineer/SKILL.md"
cp "$here/out/tools/rev-refit/tool.json" "$dest/sandbox/tools/rev-refit/tool.json"
cp "$here/out/tools/rev-refit/rev-refit" "$dest/sandbox/tools/rev-refit/rev-refit"
chmod 0755 "$dest/sandbox/tools/rev-refit/rev-refit"
cp "$here/out/tools/rev-learn/tool.json" "$dest/sandbox/tools/rev-learn/tool.json"
cp "$here/out/tools/rev-learn/rev-learn" "$dest/sandbox/tools/rev-learn/rev-learn"
chmod 0755 "$dest/sandbox/tools/rev-learn/rev-learn"
cp "$here/out/tools/rev-pr/tool.json" "$dest/sandbox/tools/rev-pr/tool.json"
cp "$here/out/tools/rev-pr/rev-pr" "$dest/sandbox/tools/rev-pr/rev-pr"
chmod 0755 "$dest/sandbox/tools/rev-pr/rev-pr"
echo "installed into $dest/sandbox — now run: (cd $dest && npm exec qm -- check && npm exec qm -- up)"
