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

Usage: plan_build.py [--single] <formula> <registry-dir> [<plan.json>]
<registry-dir> holds one <name>.json per published formula (a legacy
bottles.json in it is read too). Prints `formulae=<space-separated build
order>` (GitHub Actions output format) and, if <plan.json> is given, writes
the full plan there. With --single, fails unless <formula> is the only thing
to build: the mybrew client schedules each formula as its own run, after its
dependencies are published.
"""

from __future__ import annotations

import functools
import json
import sys
import urllib.request
from pathlib import Path

API_URL = "https://formulae.brew.sh/api/formula/{name}.json"

# Older Intel tags remain installable on newer macOS; arm64_* and Linux do not
# help an Intel Mac.
INTEL_MACOS_TAGS = {"tahoe", "sequoia", "sonoma", "ventura", "monterey", "big_sur", "catalina", "all"}


@functools.lru_cache(maxsize=None)
def fetch_api(name: str) -> dict:
    """One API request per formula per process; plans for many targets share it."""
    with urllib.request.urlopen(API_URL.format(name=name), timeout=30) as response:
        return json.loads(response.read())


def has_official_intel_bottle(meta: dict) -> bool:
    files = (meta.get("bottle") or {}).get("stable", {}).get("files", {})
    return bool(INTEL_MACOS_TAGS & set(files))


def load_registry(directory: Path) -> dict:
    registry: dict = {}
    legacy = directory / "bottles.json"
    if legacy.exists():
        registry.update(json.loads(legacy.read_text()))
    for path in sorted(directory.glob("*.json")):
        if path.name != "bottles.json":
            registry[path.stem] = json.loads(path.read_text())
    return registry


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
        formulae[name] = {"version": version, "source": source, "runtime": False,
                          "dependencies": meta.get("dependencies", []), "requires": edges}

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


def single_violation(result: dict) -> str | None:
    """Why a --single run must not proceed, or None."""
    extra = [n for n in result["order"] if n != result["target"]]
    if extra:
        return f"{result['target']} needs these built first: {' '.join(extra)}"
    return None


def main() -> None:
    args = sys.argv[1:]
    single = "--single" in args
    args = [a for a in args if a != "--single"]
    if len(args) not in (2, 3):
        raise SystemExit(__doc__)
    result = plan(args[0], load_registry(Path(args[1])))
    if len(args) == 3:
        Path(args[2]).write_text(json.dumps(result, indent=2) + "\n")
    for name, info in result["formulae"].items():
        role = "" if info["runtime"] else " (build-time)"
        print(f"  {name} {info['version']}: {info['source']}{role}", file=sys.stderr)
    if single and (reason := single_violation(result)):
        raise SystemExit(f"--single: {reason}")
    print(f"formulae={' '.join(result['order'])}")


if __name__ == "__main__":
    main()
