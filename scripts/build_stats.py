#!/usr/bin/env python3
"""Print how long this run's build job took, as JSON, for the registry.

Runs in the publish job and asks the GitHub API (not the build artifact), so
the numbers are as trustworthy as the run itself. Prints {} when unavailable;
progress history is a nicety, never a reason to fail a publish.
Needs GITHUB_REPOSITORY, GITHUB_RUN_ID and GH_TOKEN (actions: read).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime


def seconds_between(start: str, end: str) -> int:
    parse = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))  # noqa: E731
    return int((parse(end) - parse(start)).total_seconds())


def build_seconds(jobs: list[dict]) -> int | None:
    for job in jobs:
        if job["name"] == "build" and job.get("started_at") and job.get("completed_at"):
            return seconds_between(job["started_at"], job["completed_at"])
    return None


def main() -> None:
    try:
        url = (f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}"
               f"/actions/runs/{os.environ['GITHUB_RUN_ID']}/jobs")
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {os.environ['GH_TOKEN']}",
                                                       "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(request, timeout=20) as response:
            seconds = build_seconds(json.loads(response.read())["jobs"])
        print(json.dumps({"build_seconds": seconds} if seconds else {}))
    except Exception as error:  # history is optional
        print(f"build stats unavailable: {error}", file=sys.stderr)
        print("{}")


if __name__ == "__main__":
    main()
