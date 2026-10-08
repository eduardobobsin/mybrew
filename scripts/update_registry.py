#!/usr/bin/env python3
"""Record published bottles in the tap's registry: one <name>.json per formula.

Usage: update_registry.py <registry-dir> <trusted-plan.json>
Reads only the publish job's own plan (written by assemble_publish.py), which
holds the validated bottle fields and each formula's mybrew_dependencies
(what a client must install from the tap first). Nothing from the build
artifact is read here. One file per formula keeps parallel publishes of
different formulae from conflicting.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

TAG = "sequoia"


def entries(plan: dict, previous: dict, now: str) -> dict:
    """New registry entries for every formula the plan built."""
    result = {}
    for name in plan["order"]:
        info, bottle = plan["formulae"][name], plan["bottles"][name]
        item = {"bottles": dict(previous.get(name, {}).get("bottles", {}))}
        item["version"] = info["version"]
        item["mybrew_dependencies"] = info["mybrew_dependencies"]
        item["build_time_only"] = not info["runtime"]
        item["bottles"][TAG] = {**bottle, "built_at": now}
        result[name] = item
    return result


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    directory, plan_path = Path(sys.argv[1]), Path(sys.argv[2])
    directory.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from plan_build import load_registry

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for name, item in entries(json.loads(plan_path.read_text()), load_registry(directory), now).items():
        (directory / f"{name}.json").write_text(json.dumps(item, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
