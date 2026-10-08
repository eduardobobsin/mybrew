#!/usr/bin/env python3
"""Copy a homebrew/core formula into a mybrew tap.

The formula source is fetched at the exact homebrew-core commit the
Homebrew JSON API reports for it, and verified against the API's
SHA-256 checksum. Then:
- the upstream `bottle do ... end` block is removed (it describes official
  bottles on Homebrew's registry), or replaced by a mybrew bottle block;
- stanzas Homebrew only accepts in official taps (`no_autobump!`) are removed;
- the formula's aliases are recreated as Aliases/<alias> symlinks, because
  brew links opt/<alias> from them and build shims rely on that (pkgconf is
  invoked as opt/pkg-config);
- local patches the formula applies (`file "Patches/..."`) are fetched from
  homebrew-core at the same commit, since brew reads them from the tap root.

Usage: import_formula.py <formula> <tap-dir> [<bottle-block-file>]
Prints `version=<x>` and `path=<file>` lines (GitHub Actions output format).
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path

API_URL = "https://formulae.brew.sh/api/formula/{name}.json"
RAW_URL = "https://raw.githubusercontent.com/Homebrew/homebrew-core/{commit}/{path}"

BOTTLE_BLOCK = re.compile(r"^(?P<indent>[ \t]*)bottle do\n.*?^(?P=indent)end\n(?:[ \t]*\n)?", re.M | re.S)
LOCAL_PATCH = re.compile(r'^\s*file\s+"(Patches/[^"]+)"', re.M)
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
    write_aliases(name, meta.get("aliases", []), tap_dir)
    for patch in local_patches(source.decode()):
        target = tap_dir / patch
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(fetch(RAW_URL.format(commit=commit, path=patch)))
    return meta["versions"]["stable"], dest


def local_patches(source: str) -> list[str]:
    """Tap-relative patch files the formula applies; refuses paths leaving Patches/."""
    found = []
    for path in LOCAL_PATCH.findall(source):
        parts = Path(path).parts
        if ".." in parts or parts[0] != "Patches" or len(parts) < 2:
            raise SystemExit(f"refusing patch path {path!r}")
        found.append(path)
    return found


def write_aliases(name: str, aliases: list[str], tap_dir: Path) -> None:
    directory = tap_dir / "Aliases"
    for alias in aliases:
        if "/" in alias or alias.startswith("."):
            raise SystemExit(f"refusing alias {alias!r} for {name}")
        directory.mkdir(exist_ok=True)
        link = directory / alias
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(f"../Formula/{name}.rb")


def main() -> None:
    if len(sys.argv) not in (3, 4):
        raise SystemExit(__doc__)
    block = Path(sys.argv[3]).read_text().rstrip("\n") if len(sys.argv) == 4 else None
    version, dest = import_formula(sys.argv[1], Path(sys.argv[2]), block)
    print(f"version={version}")
    print(f"path={dest}")


if __name__ == "__main__":
    main()
