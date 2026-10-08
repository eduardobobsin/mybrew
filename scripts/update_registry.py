#!/usr/bin/env python3
"""Record published bottles in the tap's registry/bottles.json.

Usage: update_registry.py <registry.json> <bottle-dir>
Reads every *.bottle.json written by `brew bottle --json` in <bottle-dir>, and
plan.json from plan_build.py if present, which supplies each formula's
mybrew_dependencies (what a client must install from the tap first).
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def record(registry: dict, bottle_json: dict, now: str, plan: dict | None = None) -> dict:
    planned = (plan or {}).get("formulae", {})
    for full_name, entry in bottle_json.items():
        name = full_name.rsplit("/", 1)[-1]
        formula = entry["formula"]
        item = registry.setdefault(name, {"bottles": {}})
        item["version"] = formula["pkg_version"]
        if name in planned:
            item["mybrew_dependencies"] = planned[name]["mybrew_dependencies"]
        for tag, bottle in entry["bottle"]["tags"].items():
            item["bottles"][tag] = {
                "filename": bottle["filename"],
                "sha256": bottle["sha256"],
                "root_url": entry["bottle"]["root_url"],
                "built_at": now,
            }
    return registry


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    path, bottle_dir = Path(sys.argv[1]), Path(sys.argv[2])
    registry = json.loads(path.read_text()) if path.exists() else {}
    plan_path = bottle_dir / "plan.json"
    plan = json.loads(plan_path.read_text()) if plan_path.exists() else None
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for bottle_path in sorted(bottle_dir.glob("*.bottle.json")):
        record(registry, json.loads(bottle_path.read_text()), now, plan)
    path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
