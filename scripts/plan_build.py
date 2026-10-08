#!/usr/bin/env python3
"""Work out which formulae mybrew must bottle so <formula> installs without compiling.

Walks the runtime dependency graph using the Homebrew JSON API. A formula needs
a mybrew bottle when it has no official bottle usable on Intel macOS and the
tap's registry does not already hold its current version. Build-only
dependencies are left to brew: they are compiled on the runner if needed but
never reach users, so they are not published.

Usage: plan_build.py <formula> <registry.json> [<plan.json>]
Prints `formulae=<space-separated build order>` (GitHub Actions output format)
and, if <plan.json> is given, writes the full plan there.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

API_URL = "https://formulae.brew.sh/api/formula/{name}.json"

# Older Intel tags remain installable on newer macOS; arm64_* and Linux do not
# help an Intel Mac.
INTEL_MACOS_TAGS = {"tahoe", "sequoia", "sonoma", "ventura", "monterey", "big_sur", "catalina", "all"}


def fetch_api(name: str) -> dict:
    with urllib.request.urlopen(API_URL.format(name=name), timeout=30) as response:
        return json.loads(response.read())


def has_official_intel_bottle(meta: dict) -> bool:
    files = (meta.get("bottle") or {}).get("stable", {}).get("files", {})
    return bool(INTEL_MACOS_TAGS & set(files))


def plan(target: str, registry: dict, fetch=fetch_api) -> dict:
    """Return {"order": [...], "formulae": {name: {...}}}; order lists deps first."""
    metas: dict[str, dict] = {}
    order: list[str] = []
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if name in metas:
            return
        if name in visiting:
            raise SystemExit(f"dependency cycle through {name}")
        visiting.add(name)
        meta = fetch(name)
        for dep in meta.get("dependencies", []):
            visit(dep)
        visiting.discard(name)
        metas[name] = meta
        order.append(name)

    visit(target)

    formulae = {}
    for name in order:
        meta = metas[name]
        version = meta["versions"]["stable"]
        if meta.get("revision"):
            version = f"{version}_{meta['revision']}"
        if has_official_intel_bottle(meta):
            source = "official"
        elif registry.get(name, {}).get("version") == version:
            source = "mybrew"
        else:
            source = "build"
        formulae[name] = {"version": version, "source": source, "dependencies": meta.get("dependencies", [])}

    for info in formulae.values():
        info["mybrew_dependencies"] = [d for d in info["dependencies"] if formulae[d]["source"] != "official"]

    return {"target": target, "order": [n for n in order if formulae[n]["source"] == "build"], "formulae": formulae}


def main() -> None:
    if len(sys.argv) not in (3, 4):
        raise SystemExit(__doc__)
    registry_path = Path(sys.argv[2])
    registry = json.loads(registry_path.read_text()) if registry_path.exists() else {}
    result = plan(sys.argv[1], registry)
    if len(sys.argv) == 4:
        Path(sys.argv[3]).write_text(json.dumps(result, indent=2) + "\n")
    for name, info in result["formulae"].items():
        print(f"  {name} {info['version']}: {info['source']}", file=sys.stderr)
    print(f"formulae={' '.join(result['order'])}")


if __name__ == "__main__":
    main()
