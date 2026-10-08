"""A local copy of the Homebrew formula catalogue, for fast planning on the client.

Planning a big tree (php@7.4: ~70 formulae) one API request at a time took
14 s. The whole catalogue is one 5 MB download, and the server says how long
it stays valid (Cache-Control: max-age, 10 minutes). So the client keeps a
slim copy (only what the planner reads) and follows the server's caching
rules: no request while fresh, a conditional request (ETag) after that, and
the last copy if the network is down.

The build workflow keeps asking the API per formula, so the plan that decides
what gets published never depends on a client's cache.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

URL = "https://formulae.brew.sh/api/formula.json"
DEFAULT_MAX_AGE = 600


def cache_dir(env: dict | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("MYBREW_CACHE") or Path.home() / "Library" / "Caches" / "mybrew")


def slim(meta: dict) -> dict:
    """The fields plan_build.plan() reads, in the API's own shape."""
    files = (meta.get("bottle") or {}).get("stable", {}).get("files", {})
    return {
        "versions": {"stable": meta["versions"]["stable"]},
        "revision": meta.get("revision", 0),
        "dependencies": meta.get("dependencies", []),
        "build_dependencies": meta.get("build_dependencies", []),
        "test_dependencies": meta.get("test_dependencies", []),
        "bottle": {"stable": {"files": {tag: {} for tag in files}}},
    }


def max_age(cache_control: str | None) -> int:
    for part in (cache_control or "").split(","):
        name, _, value = part.strip().partition("=")
        if name == "max-age" and value.isdigit():
            return int(value)
    return DEFAULT_MAX_AGE


class Catalogue:
    def __init__(self, directory: Path, urlopen=urllib.request.urlopen, clock=time.time):
        self.directory, self.urlopen, self.clock = directory, urlopen, clock
        self.data_path, self.meta_path = directory / "formula.slim.json", directory / "formula.meta.json"

    def load(self) -> dict[str, dict]:
        meta = json.loads(self.meta_path.read_text()) if self.meta_path.exists() else {}
        have = self.data_path.exists()
        if have and self.clock() - meta.get("checked_at", 0) < meta.get("max_age", DEFAULT_MAX_AGE):
            return self._read()

        headers = {"Accept-Encoding": "gzip"}
        if have and meta.get("etag"):
            headers["If-None-Match"] = meta["etag"]
        try:
            with self.urlopen(urllib.request.Request(URL, headers=headers), timeout=60) as response:
                body = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                data = {f["name"]: slim(f) for f in json.loads(body)}
                self.directory.mkdir(parents=True, exist_ok=True)
                self.data_path.write_text(json.dumps(data, separators=(",", ":")))
                meta = {"etag": response.headers.get("ETag")}
                cache_control = response.headers.get("Cache-Control")
        except urllib.error.HTTPError as error:
            if error.code != 304 or not have:
                raise
            cache_control = error.headers.get("Cache-Control")
        except urllib.error.URLError as error:
            if not have:
                raise
            print(f"mybrew: offline ({error.reason}); planning with the cached formula catalogue",
                  file=sys.stderr)
            return self._read()

        meta.update(checked_at=self.clock(), max_age=max_age(cache_control))
        self.directory.mkdir(parents=True, exist_ok=True)
        self.meta_path.write_text(json.dumps(meta))
        return self._read()

    def _read(self) -> dict[str, dict]:
        return json.loads(self.data_path.read_text())
