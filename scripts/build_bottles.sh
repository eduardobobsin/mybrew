#!/usr/bin/env bash
# Build, test and bottle tap formulae in the given order (dependencies first),
# merging each bottle block into its formula.
# Usage: build_bottles.sh <tap> <root-url> <output-dir> <formula>...
set -euo pipefail
export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 HOMEBREW_NO_AUTOREMOVE=1

tap="$1" root_url="$2" out="$3"; shift 3
mkdir -p "$out"

for formula in "$@"; do
  echo "::group::${formula}"
  # A runner image may ship the core formula of the same name; brew refuses to
  # install one name from two taps.
  if brew list --formula --versions "$formula" >/dev/null 2>&1; then
    brew uninstall --formula --ignore-dependencies --force "$formula"
  fi
  brew install --build-bottle --verbose "${tap}/${formula}"
  brew test --verbose "${tap}/${formula}"
  (cd "$out" && brew bottle --json --root-url="$root_url" "${tap}/${formula}" &&
    brew bottle --merge --write --no-commit "./${formula}--"*.bottle.json)
  echo "::endgroup::"
done

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
