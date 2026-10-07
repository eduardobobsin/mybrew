#!/usr/bin/env python3
"""Copy a homebrew/core formula into a mybrew tap, ready to be bottled.

The formula source is fetched at the exact homebrew-core commit the
Homebrew JSON API reports for it, and verified against the API's
SHA-256 checksum. The upstream `bottle do ... end` block is removed:
it describes official bottles on Homebrew's registry, and keeping it
would make `brew bottle --merge` publish tags we never built.

Usage: import_formula.py <formula> <tap-dir>
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


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read()


def strip_bottle_block(source: str) -> str:
    stripped, count = BOTTLE_BLOCK.subn("", source, count=1)
    return stripped if count else source


def import_formula(name: str, tap_dir: Path) -> tuple[str, Path]:
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
    dest.write_text(strip_bottle_block(source.decode()))
    return meta["versions"]["stable"], dest


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    version, dest = import_formula(sys.argv[1], Path(sys.argv[2]))
    print(f"version={version}")
    print(f"path={dest}")


if __name__ == "__main__":
    main()
