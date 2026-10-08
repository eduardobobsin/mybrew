#!/usr/bin/env bash
# Record published bottles in registry/<name>.json and commit the formulae
# assemble_publish.py wrote. Run from the tap checkout. Retries the push, since
# parallel runs publish other formulae at the same time (different files, so
# the rebase is clean).
# Usage: commit_formula.sh <target-formula> <trusted-plan.json>
set -euo pipefail

target="$1" plan="$2"
python3 "$(dirname "$0")/update_registry.py" registry "$plan"
built=$(python3 -c 'import json, sys; print(" ".join(json.load(open(sys.argv[1]))["order"]))' "$plan")
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add Formula/ registry/
[[ -d Aliases ]] && git add Aliases/
git commit -m "${target}: add bottles" -m "Built: ${built}"
for attempt in 1 2 3 4 5; do
  git pull --rebase --quiet && git push --quiet && exit 0
  echo "push rejected (attempt ${attempt}); retrying"
  sleep $((attempt * 3 + RANDOM % 5))
done
echo "could not push after 5 attempts" >&2
exit 1
