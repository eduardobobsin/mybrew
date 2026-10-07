#!/usr/bin/env bash
# Record bottles in registry/bottles.json and commit the formula they belong to.
# Usage: commit_formula.sh <formula> <bottle-dir>   (run from the tap checkout)
set -euo pipefail

formula="$1" bottle_dir="$2"
python3 "$(dirname "$0")/update_registry.py" registry/bottles.json "$bottle_dir"/*.bottle.json
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add "Formula/${formula}.rb" registry/bottles.json
git commit -m "${formula}: add bottle"
git pull --rebase
git push
