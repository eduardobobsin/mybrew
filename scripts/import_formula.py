#!/usr/bin/env python3
"""Copy a homebrew/core formula into a mybrew tap.

The formula source is fetched at the exact homebrew-core commit the
Homebrew JSON API reports for it, and verified against the API's
SHA-256 checksum. Then:
- the upstream `bottle do ... end` block is removed (it describes official
  bottles on Homebrew's registry), or replaced by a mybrew bottle block;
- stanzas Homebrew only accepts in official taps (`no_autobump!`) are removed.

Usage: import_formula.py <formula> <tap-dir> [<bottle-block-file>]
Prints `version=<x>` and `path=<file>` lines (GitHub Actions output format).
"""

import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path

API_URL = "https://formulae.brew.sh/api/formula/{name}.json"
RAW_URL = "https://raw.githubusercontent.com/Homebrew/homebrew-core/{commit}/{path}"

BOTTLE_BLOCK = re.compile(r"^(?P<indent>[ \t]*)bottle do\n.*?^(?P=indent)end\n(?:[ \t]*\n)?", re.M | re.S)
OFFICIAL_ONLY = re.compile(r"^[ \t]*no_autobump!.*\n(?:[ \t]*\n)?", re.M)
# Where a bottle block goes when upstream had none: before the first of these.
BOTTLE_ANCHOR = re.compile(r"^  (?:depends_on|uses_from_macos|on_\w+ do|resource|patch|def install)\b", re.M)


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read()


def strip_bottle_block(source: str) -> str:
    return prepare(source)


def prepare(source: str, bottle_block: str | None = None) -> str:
    source = OFFICIAL_ONLY.sub("", source)
    replacement = f"{bottle_block}\n\n" if bottle_block else ""
    result, count = BOTTLE_BLOCK.subn(lambda _: replacement, source, count=1)
    if count or not bottle_block:
        return result
    anchor = BOTTLE_ANCHOR.search(source)
    if not anchor:
        raise SystemExit("no place to insert the bottle block")
    return source[: anchor.start()] + replacement + source[anchor.start():]


def import_formula(name: str, tap_dir: Path, bottle_block: str | None = None) -> tuple[str, Path]:
    meta = json.loads(fetch(API_URL.format(name=name)))
    commit = meta["tap_git_head"]
    source_path = meta["ruby_source_path"]
    expected = meta["ruby_source_checksum"]["sha256"]

    source = fetch(RAW_URL.format(commit=commit, path=source_path))
    actual = hashlib.sha256(source).hexdigest()
    if actual != expected:
        raise SystemExit(f"checksum mismatch for {source_path}@{commit}: {actual} != {expected}")

    dest = tap_dir / "Formula" / f"{name}.rb"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(prepare(source.decode(), bottle_block))
    return meta["versions"]["stable"], dest


def main() -> None:
    if len(sys.argv) not in (3, 4):
        raise SystemExit(__doc__)
    block = Path(sys.argv[3]).read_text().rstrip("\n") if len(sys.argv) == 4 else None
    version, dest = import_formula(sys.argv[1], Path(sys.argv[2]), block)
    print(f"version={version}")
    print(f"path={dest}")


if __name__ == "__main__":
    main()
