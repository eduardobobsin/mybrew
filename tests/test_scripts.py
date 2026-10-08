import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from assemble_publish import render_block, validate  # noqa: E402
from import_formula import prepare, strip_bottle_block  # noqa: E402
from plan_build import plan  # noqa: E402
from update_registry import record  # noqa: E402

FORMULA = """class Dos2unix < Formula
  desc "Convert text between DOS, UNIX, and Mac formats"
  url "https://example.com/dos2unix-7.5.2.tar.gz"
  sha256 "abc"

  livecheck do
    url :homepage
  end

  bottle do
    sha256 cellar: :any_skip_relocation, arm64_sequoia: "111"
    sha256 cellar: :any_skip_relocation, arm64_sonoma:  "222"
  end

  def install
    system "make"
  end
end
"""


class StripBottleBlockTest(unittest.TestCase):
    def test_removes_only_the_bottle_block(self):
        result = strip_bottle_block(FORMULA)
        self.assertNotIn("bottle do", result)
        self.assertNotIn("arm64_sequoia", result)
        self.assertIn("livecheck do\n    url :homepage\n  end\n", result)
        self.assertIn("def install", result)

    def test_leaves_formula_without_bottle_block_untouched(self):
        source = FORMULA.replace(FORMULA[FORMULA.index("  bottle do"):FORMULA.index("  def install")], "")
        self.assertEqual(strip_bottle_block(source), source)


class PrepareTest(unittest.TestCase):
    BLOCK = '  bottle do\n    root_url "https://example.com"\n    sha256 cellar: :any, sequoia: "abc"\n  end'

    def test_replaces_upstream_block_in_place(self):
        result = prepare(FORMULA, self.BLOCK)
        self.assertNotIn("arm64_sequoia", result)
        self.assertLess(result.index("livecheck do"), result.index("bottle do"))
        self.assertLess(result.index("bottle do"), result.index("def install"))

    def test_inserts_block_when_upstream_has_none(self):
        result = prepare(strip_bottle_block(FORMULA), self.BLOCK)
        self.assertLess(result.index("bottle do"), result.index("def install"))

    def test_removes_official_only_stanzas(self):
        source = FORMULA.replace("  livecheck do", '  no_autobump! because: :requires_manual_review\n\n  livecheck do')
        self.assertNotIn("no_autobump!", prepare(source))


SHA = "a" * 64


def bottle_json(name="calc", version="2.17", **overrides):
    bottle = {"root_url": "https://r", "cellar": "any", "rebuild": 0,
              "tags": {"sequoia": {"filename": f"{name}-{version}.sequoia.bottle.tar.gz", "sha256": SHA}}}
    bottle.update(overrides)
    return {f"me/mybrew/{name}": {"formula": {"pkg_version": version}, "bottle": bottle}}


class ValidateTest(unittest.TestCase):
    def check(self, data, name="calc", version="2.17"):
        return validate(name, version, "me/mybrew", "https://r", data)

    def test_accepts_well_formed_bottle(self):
        self.assertEqual(self.check(bottle_json())["key"], "calc-2.17.sequoia.bottle.tar.gz")

    def test_rejects_tampered_fields(self):
        cases = {
            "root_url": bottle_json(root_url="https://evil"),
            "cellar": bottle_json(cellar='"; system "rm -rf /"; "'),
            "tag": bottle_json(tags={"arm64_sequoia": {"filename": "x", "sha256": SHA}}),
            "sha": bottle_json(tags={"sequoia": {"filename": "calc-2.17.sequoia.bottle.tar.gz", "sha256": '" + `id` + "'}}),
            "filename": bottle_json(tags={"sequoia": {"filename": "../../x.tar.gz", "sha256": SHA}}),
            "rebuild": bottle_json(rebuild="1\n  system 'id'"),
            "version": bottle_json(version="9.9"),
            "other formula": bottle_json(name="readline"),
        }
        for label, data in cases.items():
            with self.subTest(label), self.assertRaises(SystemExit):
                self.check(data)

    def test_url_encodes_at_sign_but_keys_raw_name(self):
        data = bottle_json("openssl@3", "3.5", tags={"sequoia": {"filename": "openssl%403-3.5.sequoia.bottle.tar.gz", "sha256": SHA}})
        result = self.check(data, "openssl@3", "3.5")
        self.assertEqual(result["key"], "openssl@3-3.5.sequoia.bottle.tar.gz")

    def test_renders_block_with_rebuild(self):
        block = render_block("https://r", {"cellar": "/usr/local/Cellar", "rebuild": 2, "sha256": SHA})
        self.assertIn("    rebuild 2\n", block)
        self.assertIn(f'sha256 cellar: "/usr/local/Cellar", sequoia: "{SHA}"', block)


class RecordTest(unittest.TestCase):
    def test_records_each_tag(self):
        bottle_json = {
            "eduardobobsin/mybrew/dos2unix": {
                "formula": {"pkg_version": "7.5.2"},
                "bottle": {
                    "root_url": "https://example.com/bottles",
                    "tags": {"sequoia": {"filename": "dos2unix--7.5.2.sequoia.bottle.tar.gz", "sha256": "f00"}},
                },
            }
        }
        registry = record({}, bottle_json, "2026-10-07T00:00:00+00:00")
        self.assertEqual(registry["dos2unix"]["version"], "7.5.2")
        self.assertEqual(registry["dos2unix"]["bottles"]["sequoia"]["sha256"], "f00")

    def test_records_mybrew_dependencies_from_plan(self):
        bottle_json = {
            "eduardobobsin/mybrew/calc": {
                "formula": {"pkg_version": "2.17"},
                "bottle": {"root_url": "https://example.com", "tags": {"sequoia": {"filename": "c", "sha256": "1"}}},
            }
        }
        plan = {"order": ["calc"], "formulae": {"calc": {"mybrew_dependencies": ["readline"]}}}
        registry = record({}, bottle_json, "now", plan)
        self.assertEqual(registry["calc"]["mybrew_dependencies"], ["readline"])

    def test_ignores_bottles_outside_the_plan(self):
        bottle_json = {"me/mybrew/evil": {"formula": {"pkg_version": "1"}, "bottle": {"root_url": "r", "tags": {}}}}
        self.assertEqual(record({}, bottle_json, "now", {"order": ["calc"], "formulae": {}}), {})


def api(version, deps=(), tags=()):
    return {"versions": {"stable": version}, "revision": 0, "dependencies": list(deps),
            "bottle": {"stable": {"files": {t: {} for t in tags}}}}


class PlanTest(unittest.TestCase):
    API = {
        "calc": api("2.17", ["readline"], ["arm64_sequoia"]),
        "readline": api("8.3", [], ["arm64_sequoia"]),
        "jdupes": api("1.31", ["libjodycode"], ["arm64_sequoia"]),
        "libjodycode": api("4.0", [], ["sonoma", "arm64_sequoia"]),
    }

    def plan(self, target, registry=None):
        return plan(target, registry or {}, fetch=self.API.__getitem__)

    def test_builds_missing_dependency_before_target(self):
        result = self.plan("calc")
        self.assertEqual(result["order"], ["readline", "calc"])
        self.assertEqual(result["formulae"]["calc"]["mybrew_dependencies"], ["readline"])

    def test_official_intel_bottle_is_not_rebuilt(self):
        result = self.plan("jdupes")
        self.assertEqual(result["order"], ["jdupes"])
        self.assertEqual(result["formulae"]["libjodycode"]["source"], "official")
        self.assertEqual(result["formulae"]["jdupes"]["mybrew_dependencies"], [])

    def test_current_registry_entry_is_reused(self):
        result = self.plan("calc", {"readline": {"version": "8.3"}})
        self.assertEqual(result["order"], ["calc"])
        self.assertEqual(result["formulae"]["readline"]["source"], "mybrew")

    def test_outdated_registry_entry_is_rebuilt(self):
        result = self.plan("calc", {"readline": {"version": "8.2"}})
        self.assertEqual(result["order"], ["readline", "calc"])


if __name__ == "__main__":
    unittest.main()
