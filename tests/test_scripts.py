import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from import_formula import strip_bottle_block  # noqa: E402
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
        plan = {"formulae": {"calc": {"mybrew_dependencies": ["readline"]}}}
        registry = record({}, bottle_json, "now", plan)
        self.assertEqual(registry["calc"]["mybrew_dependencies"], ["readline"])


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
