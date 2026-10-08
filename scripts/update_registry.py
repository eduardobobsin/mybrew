#!/usr/bin/env python3
"""Record published bottles in the tap's registry/bottles.json.

Usage: update_registry.py <registry.json> <plan.json> <bottle-json>...
<plan.json> is the publish job's own plan (see assemble_publish.py); it lists
which bottles to record and each formula's mybrew_dependencies (what a client
must install from the tap first). Bottle JSON for unplanned formulae is ignored.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def record(registry: dict, bottle_json: dict, now: str, plan: dict | None = None) -> dict:
    planned = (plan or {}).get("formulae", {})
    for full_name, entry in bottle_json.items():
        name = full_name.rsplit("/", 1)[-1]
        if plan is not None and name not in plan.get("order", []):
            continue
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
    if len(sys.argv) < 4:
        raise SystemExit(__doc__)
    path, plan_path = Path(sys.argv[1]), Path(sys.argv[2])
    registry = json.loads(path.read_text()) if path.exists() else {}
    plan = json.loads(plan_path.read_text())
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for bottle_path in sys.argv[3:]:
        record(registry, json.loads(Path(bottle_path).read_text()), now, plan)
    path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
