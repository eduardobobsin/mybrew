#!/usr/bin/env bash
# Prepare and build everything a plan needs, dependencies first:
#   mybrew  -> install our existing bottle from the tap (brew would otherwise
#              resolve the name to homebrew/core, which has no Intel bottle)
#   build   -> build, test and bottle from the tap, merging the bottle block
#   official-> left to brew, which pours it when a dependent needs it
# Usage: build_bottles.sh <tap> <root-url> <output-dir> <plan.json>
set -euo pipefail
export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 HOMEBREW_NO_AUTOREMOVE=1

tap="$1" root_url="$2" out="$3" plan="$4"
mkdir -p "$out"

# A runner image may ship a core formula of the same name; brew refuses to
# install one name from two taps. brew uninstall keeps the formula's config in
# etc, and a stale config breaks builds that rewrite it (openldap's inreplace
# of etc/openldap/slapd.conf), so on this disposable runner drop it too.
clear_name() {
  if brew list --formula --versions "$1" >/dev/null 2>&1; then
    brew uninstall --formula --ignore-dependencies --force "$1"
    local etc
    etc="$(brew --prefix)/etc"
    for dir in "$etc/$1" "$etc/${1%%@*}"; do
      if [[ -e "$dir" ]]; then
        echo "removing stale config ${dir} left by the runner image's $1"
        rm -rf "$dir"
      fi
    done
  fi
}

# The plan is read on fd 3: brew reads stdin and would swallow it otherwise.
# brew install fails when a formula built fine but cannot be linked because a
# preinstalled runner formula owns the same files (openssl@1.1 owns
# bin/openssl). Runners are disposable, so overwrite those links; a formula
# that did not build leaves no install receipt and still fails.
install_or_relink() {
  local formula="$1"; shift
  if brew install "$@"; then return 0; fi
  local name="${formula##*/}" receipt
  receipt=$(ls "$(brew --cellar)/${name}"/*/INSTALL_RECEIPT.json 2>/dev/null | tail -1)
  [[ -n "$receipt" ]] || return 1
  echo "::warning::${name} installed but did not link; overwriting conflicting links on this runner"
  brew link --overwrite "$formula"
}

while read -r formula source <&3; do
  case "$source" in
    mybrew)
      echo "::group::${formula} (mybrew bottle)"
      clear_name "$formula"
      install_or_relink "${tap}/${formula}" "${tap}/${formula}"
      ;;
    build)
      echo "::group::${formula} (build)"
      clear_name "$formula"
      install_or_relink "${tap}/${formula}" --build-bottle --verbose "${tap}/${formula}"
      brew test --verbose "${tap}/${formula}"
      (cd "$out" && brew bottle --json --root-url="$root_url" "${tap}/${formula}" &&
        brew bottle --merge --write --no-commit "./${formula}--"*.bottle.json)
      ;;
    *) continue ;;
  esac
  echo "::endgroup::"
done 3< <(python3 -c '
import json, sys
for name, info in json.load(open(sys.argv[1]))["formulae"].items():
    print(name, info["source"])
' "$plan")

# Every planned build must have produced its bottle JSON.
python3 - "$plan" "$out" <<'PY'
import json, pathlib, sys
plan, out = json.load(open(sys.argv[1])), pathlib.Path(sys.argv[2])
missing = [n for n in plan["order"] if not list(out.glob(f"{n}--*.bottle.json"))]
if missing:
    sys.exit(f"no bottle produced for: {' '.join(missing)}")
PY

# brew writes tarballs under their local name; uploads must use the name the
# formula will request.
python3 - "$out" <<'PY'
import json, os, pathlib, sys
out = pathlib.Path(sys.argv[1])
for path in out.glob("*.bottle.json"):
    for entry in json.loads(path.read_text()).values():
        for bottle in entry["bottle"]["tags"].values():
            local, remote = out / bottle["local_filename"], out / bottle["filename"]
            if local != remote and local.exists():
                os.rename(local, remote)
PY
ls -la "$out"
