#!/usr/bin/env bash
# Record published bottles in registry/bottles.json and commit the formulae
# assemble_publish.py wrote. Run from the tap checkout.
# Usage: commit_formula.sh <target-formula> <trusted-plan.json> <bottle-dir>
set -euo pipefail

target="$1" plan="$2" bottle_dir="$3"
python3 "$(dirname "$0")/update_registry.py" registry/bottles.json "$plan" "$bottle_dir"/*.bottle.json
built=$(python3 -c 'import json, sys; print(" ".join(json.load(open(sys.argv[1]))["order"]))' "$plan")
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add Formula/ registry/bottles.json
git commit -m "${target}: add bottles" -m "Built: ${built}"
git pull --rebase
git push
