#!/usr/bin/env python3
"""Record published bottles in the tap's registry/bottles.json.

Usage: update_registry.py <registry.json> <trusted-plan.json>
Reads only the publish job's own plan (written by assemble_publish.py), which
holds the validated bottle fields and each formula's mybrew_dependencies
(what a client must install from the tap first). Nothing from the build
artifact is read here.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

TAG = "sequoia"


def record(registry: dict, plan: dict, now: str) -> dict:
    for name in plan["order"]:
        info, bottle = plan["formulae"][name], plan["bottles"][name]
        item = registry.setdefault(name, {"bottles": {}})
        item["version"] = info["version"]
        item["mybrew_dependencies"] = info["mybrew_dependencies"]
        item["bottles"][TAG] = {**bottle, "built_at": now}
    return registry


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    path, plan_path = Path(sys.argv[1]), Path(sys.argv[2])
    registry = json.loads(path.read_text()) if path.exists() else {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    record(registry, json.loads(plan_path.read_text()), now)
    path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
