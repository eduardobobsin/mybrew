#!/usr/bin/env bash
# Install mybrew formulae from the published tap in the given order
# (dependencies first) and fail unless every one was poured from a bottle.
# Usage: verify_install.sh <tap> <formula>...
set -euo pipefail
export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 HOMEBREW_NO_AUTOREMOVE=1

tap="$1"; shift
formulae=("$@")

brew tap "$tap"
brew trust --tap "$tap"

for formula in "${formulae[@]}"; do
  if brew list --formula --versions "$formula" >/dev/null 2>&1; then
    brew uninstall --formula --ignore-dependencies --force "$formula"
  fi
  brew install "${tap}/${formula}"
done

status=0
for formula in "${formulae[@]}"; do
  receipt=$(ls "$(brew --cellar)/${formula}"/*/INSTALL_RECEIPT.json | tail -1)
  read -r tap_used poured < <(python3 -c '
import json, sys
t = json.load(open(sys.argv[1]))
print(t["source"]["tap"], t["poured_from_bottle"])
' "$receipt")
  echo "${formula}: tap=${tap_used} poured_from_bottle=${poured}"
  [[ "$tap_used" == "$tap" && "$poured" == True ]] || status=1
done
exit "$status"
