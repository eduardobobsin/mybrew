#!/usr/bin/env python3
"""Turn an untrusted build artifact into trusted tap changes.

Runs in the privileged publish job. The build job executed upstream build
code, so nothing it produced is trusted except bottle tarballs, and those
only after their metadata checks out. Concretely:
- the plan is recomputed here from the Homebrew API and the tap's registry;
- each planned formula's bottle JSON is validated field by field against
  the plan and the expected root URL, and its tarball's SHA-256 is checked;
- each formula is re-imported from homebrew-core (checksum-verified) and
  given a bottle block rendered here from the validated fields.
Formula files and plan.json in the artifact are ignored and deleted.

Usage: assemble_publish.py <target> <tap> <root-url> <tap-dir> <bottle-dir> <plan-out>
Prints GitHub Actions outputs:
  uploads=<newline-free list of tarball:key pairs, space-separated>
  install=<space-separated install order of all non-official formulae>
"""

import hashlib
import json
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from import_formula import import_formula  # noqa: E402
from plan_build import plan  # noqa: E402

TAG = "sequoia"
CELLARS = {"any", "any_skip_relocation", "/usr/local/Cellar"}
SHA256 = re.compile(r"[0-9a-f]{64}")


def fail(message: str) -> None:
    raise SystemExit(f"refusing to publish: {message}")


def validate(name: str, version: str, tap: str, root_url: str, data: dict) -> dict:
    """Return the trusted fields of one `brew bottle --json` document."""
    if list(data) != [f"{tap}/{name}"]:
        fail(f"{name}: unexpected keys {list(data)}")
    entry = data[f"{tap}/{name}"]
    bottle = entry["bottle"]
    if entry["formula"]["pkg_version"] != version:
        fail(f"{name}: built {entry['formula']['pkg_version']}, planned {version}")
    if bottle["root_url"] != root_url:
        fail(f"{name}: root_url {bottle['root_url']!r}")
    if bottle["cellar"] not in CELLARS:
        fail(f"{name}: cellar {bottle['cellar']!r}")
    rebuild = bottle["rebuild"]
    if not isinstance(rebuild, int) or rebuild < 0:
        fail(f"{name}: rebuild {rebuild!r}")
    if list(bottle["tags"]) != [TAG]:
        fail(f"{name}: tags {list(bottle['tags'])}")
    tag = bottle["tags"][TAG]
    if not SHA256.fullmatch(tag["sha256"]):
        fail(f"{name}: sha256 {tag['sha256']!r}")
    raw_name = f"{name}-{version}.{TAG}.bottle{f'.{rebuild}' if rebuild else ''}.tar.gz"
    if tag["filename"] != urllib.parse.quote(raw_name, safe=""):
        fail(f"{name}: filename {tag['filename']!r}")
    return {"cellar": bottle["cellar"], "rebuild": rebuild, "sha256": tag["sha256"],
            "filename": tag["filename"], "key": raw_name}


def render_block(root_url: str, bottle: dict) -> str:
    cellar = bottle["cellar"]
    cellar = f":{cellar}" if cellar in ("any", "any_skip_relocation") else f'"{cellar}"'
    lines = ["  bottle do", f'    root_url "{root_url}"']
    if bottle["rebuild"]:
        lines.append(f"    rebuild {bottle['rebuild']}")
    lines += [f'    sha256 cellar: {cellar}, {TAG}: "{bottle["sha256"]}"', "  end"]
    return "\n".join(lines)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    if len(sys.argv) != 7:
        raise SystemExit(__doc__)
    target, tap, root_url = sys.argv[1:4]
    tap_dir, bottle_dir, plan_out = map(Path, sys.argv[4:7])

    for untrusted in [*bottle_dir.glob("*.rb"), bottle_dir / "plan.json"]:
        untrusted.unlink(missing_ok=True)

    registry_path = tap_dir / "registry" / "bottles.json"
    registry = json.loads(registry_path.read_text()) if registry_path.exists() else {}
    trusted = plan(target, registry)
    if not trusted["order"]:
        fail("plan is empty; nothing should have been built")

    uploads = []
    for name in trusted["order"]:
        version = trusted["formulae"][name]["version"]
        candidates = list(bottle_dir.glob(f"{name}--*.bottle.json"))
        if len(candidates) != 1:
            fail(f"{name}: expected one bottle JSON, found {len(candidates)}")
        bottle = validate(name, version, tap, root_url, json.loads(candidates[0].read_text()))
        tarball = bottle_dir / bottle["filename"]
        if not tarball.is_file() or sha256_file(tarball) != bottle["sha256"]:
            fail(f"{name}: tarball missing or checksum mismatch")
        imported_version, _ = import_formula(name, tap_dir, render_block(root_url, bottle))
        if version.split("_")[0] != imported_version:
            fail(f"{name}: homebrew-core moved to {imported_version} during the build")
        uploads.append(f"{bottle['filename']}:{bottle['key']}")

    plan_out.write_text(json.dumps(trusted, indent=2) + "\n")
    install = [n for n, f in trusted["formulae"].items() if f["source"] != "official"]
    print(f"uploads={' '.join(uploads)}")
    print(f"install={' '.join(install)}")


if __name__ == "__main__":
    main()
