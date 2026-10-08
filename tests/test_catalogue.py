import gzip
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from catalogue import Catalogue, max_age, slim  # noqa: E402
from plan_build import plan  # noqa: E402

FULL = [
    {"name": "readline", "versions": {"stable": "8.3", "head": "HEAD"}, "revision": 0, "dependencies": [],
     "build_dependencies": [], "test_dependencies": [], "desc": "x" * 100,
     "bottle": {"stable": {"files": {"arm64_sequoia": {"url": "u", "sha256": "s"}}}}},
    {"name": "calc", "versions": {"stable": "2.17"}, "revision": 1, "dependencies": ["readline"],
     "build_dependencies": [], "test_dependencies": [],
     "bottle": {"stable": {"files": {"sonoma": {"url": "u", "sha256": "s"}}}}},
]


def headers(**values):
    message = Message()
    for key, value in values.items():
        message[key.replace("_", "-")] = value
    return message


class Response(io.BytesIO):
    def __init__(self, body, **hdrs):
        super().__init__(body)
        self.headers = headers(**hdrs)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Server:
    def __init__(self):
        self.requests, self.mode = [], "200"

    def __call__(self, request, timeout):
        self.requests.append(dict(request.header_items()))
        if self.mode == "offline":
            raise urllib.error.URLError("no route")
        if self.mode == "304":
            raise urllib.error.HTTPError(request.full_url, 304, "Not Modified", headers(Cache_Control="max-age=600"), None)
        return Response(gzip.compress(json.dumps(FULL).encode()), Content_Encoding="gzip", ETag='W/"v1"',
                        Cache_Control="max-age=600")


class CatalogueTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.server, self.now = Server(), [1000.0]
        self.catalogue = Catalogue(self.dir, urlopen=self.server, clock=lambda: self.now[0])

    def test_first_load_downloads_and_keeps_only_planner_fields(self):
        data = self.catalogue.load()
        self.assertEqual(sorted(data), ["calc", "readline"])
        self.assertNotIn("desc", data["readline"])
        self.assertEqual(data["calc"]["bottle"]["stable"]["files"], {"sonoma": {}})

    def test_fresh_copy_needs_no_request(self):
        self.catalogue.load()
        self.now[0] += 599
        self.catalogue.load()
        self.assertEqual(len(self.server.requests), 1)

    def test_stale_copy_is_revalidated_with_its_etag(self):
        self.catalogue.load()
        self.now[0] += 601
        self.server.mode = "304"
        self.assertIn("calc", self.catalogue.load())
        self.assertEqual(self.server.requests[-1].get("If-none-match"), 'W/"v1"')
        self.now[0] += 300
        self.catalogue.load()
        self.assertEqual(len(self.server.requests), 2)  # the 304 restarted the freshness window

    def test_offline_uses_the_last_copy(self):
        self.catalogue.load()
        self.now[0] += 10_000
        self.server.mode = "offline"
        self.assertIn("calc", self.catalogue.load())

    def test_offline_without_a_copy_fails(self):
        self.server.mode = "offline"
        with self.assertRaises(urllib.error.URLError):
            self.catalogue.load()

    def test_slim_entries_plan_like_full_ones(self):
        full = {f["name"]: f for f in FULL}
        slimmed = {name: slim(meta) for name, meta in full.items()}
        self.assertEqual(plan("calc", {}, fetch=full.__getitem__), plan("calc", {}, fetch=slimmed.__getitem__))

    def test_max_age(self):
        self.assertEqual(max_age("public, max-age=600"), 600)
        self.assertEqual(max_age(None), 600)


if __name__ == "__main__":
    unittest.main()
