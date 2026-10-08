#!/usr/bin/env python3
"""Work out which formulae mybrew must bottle so <formula> installs without compiling.

Walks the dependency graph using the Homebrew JSON API. A formula needs a
mybrew bottle when it has no official bottle usable on Intel macOS and the
tap's registry does not already hold its current version.

Runtime dependencies are always walked. Build and test dependencies are walked
for every formula that will be built, because brew will not compile a missing
dependency on its own; those without an Intel bottle are built and published
too, so later builds can reuse them. Each formula is marked `runtime` when the
target needs it installed; only those matter to users.

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
    """Return {"target", "order", "formulae"}; formulae and order are deps-first."""
    formulae: dict[str, dict] = {}
    visiting: set[str] = set()

    def source_of(name: str, meta: dict) -> tuple[str, str]:
        version = meta["versions"]["stable"]
        if meta.get("revision"):
            version = f"{version}_{meta['revision']}"
        if has_official_intel_bottle(meta):
            return version, "official"
        if registry.get(name, {}).get("version") == version:
            return version, "mybrew"
        return version, "build"

    def visit(name: str) -> None:
        if name in formulae:
            return
        if name in visiting:
            raise SystemExit(f"dependency cycle through {name}")
        visiting.add(name)
        meta = fetch(name)
        version, source = source_of(name, meta)
        edges = list(meta.get("dependencies", []))
        if source == "build":
            edges += meta.get("build_dependencies", []) + meta.get("test_dependencies", [])
        for dep in edges:
            visit(dep)
        visiting.discard(name)
        formulae[name] = {"version": version, "source": source,
                          "dependencies": meta.get("dependencies", []), "runtime": False}

    visit(target)

    def mark_runtime(name: str) -> None:
        if not formulae[name]["runtime"]:
            formulae[name]["runtime"] = True
            for dep in formulae[name]["dependencies"]:
                mark_runtime(dep)

    mark_runtime(target)
    for info in formulae.values():
        info["mybrew_dependencies"] = [d for d in info["dependencies"] if formulae[d]["source"] != "official"]

    return {"target": target, "order": [n for n, f in formulae.items() if f["source"] == "build"],
            "formulae": formulae}


def main() -> None:
    if len(sys.argv) not in (3, 4):
        raise SystemExit(__doc__)
    registry_path = Path(sys.argv[2])
    registry = json.loads(registry_path.read_text()) if registry_path.exists() else {}
    result = plan(sys.argv[1], registry)
    if len(sys.argv) == 4:
        Path(sys.argv[3]).write_text(json.dumps(result, indent=2) + "\n")
    for name, info in result["formulae"].items():
        role = "" if info["runtime"] else " (build-time)"
        print(f"  {name} {info['version']}: {info['source']}{role}", file=sys.stderr)
    print(f"formulae={' '.join(result['order'])}")


if __name__ == "__main__":
    main()
