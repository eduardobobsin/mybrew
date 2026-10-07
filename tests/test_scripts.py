import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from import_formula import strip_bottle_block  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
