#!/usr/bin/env bash
# Move built formulae into the tap, record their bottles in
# registry/bottles.json and commit. Run from the tap checkout.
# Usage: commit_formula.sh <target-formula> <bottle-dir>
set -euo pipefail

target="$1" bottle_dir="$2"
names=()
for rb in "$bottle_dir"/*.rb; do
  cp "$rb" Formula/
  names+=("$(basename "$rb" .rb)")
done
python3 "$(dirname "$0")/update_registry.py" registry/bottles.json "$bottle_dir"
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add Formula/ registry/bottles.json
git commit -m "${target}: add bottles" -m "Built: ${names[*]}"
git pull --rebase
git push
