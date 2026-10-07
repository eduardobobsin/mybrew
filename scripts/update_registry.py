#!/usr/bin/env python3
"""Record a published bottle in the tap's registry/bottles.json.

Usage: update_registry.py <registry.json> <bottle.json>...
Each <bottle.json> is the file written by `brew bottle --json`.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def record(registry: dict, bottle_json: dict, now: str) -> dict:
    for full_name, entry in bottle_json.items():
        name = full_name.rsplit("/", 1)[-1]
        formula = entry["formula"]
        item = registry.setdefault(name, {"bottles": {}})
        item["version"] = formula["pkg_version"]
        for tag, bottle in entry["bottle"]["tags"].items():
            item["bottles"][tag] = {
                "filename": bottle["filename"],
                "sha256": bottle["sha256"],
                "root_url": entry["bottle"]["root_url"],
                "built_at": now,
            }
    return registry


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    path = Path(sys.argv[1])
    registry = json.loads(path.read_text()) if path.exists() else {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for bottle_path in sys.argv[2:]:
        record(registry, json.loads(Path(bottle_path).read_text()), now)
    path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
