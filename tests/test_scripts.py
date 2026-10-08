import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from assemble_publish import render_block, validate  # noqa: E402
from import_formula import local_patches, prepare, strip_bottle_block, write_aliases  # noqa: E402
import json  # noqa: E402
import tempfile  # noqa: E402

from plan_build import load_registry, plan, single_violation  # noqa: E402
from update_registry import entries  # noqa: E402

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


class LocalPatchTest(unittest.TestCase):
    def test_finds_patch_files_in_patch_blocks(self):
        source = 'class P < Formula\n  patch do\n    file "Patches/python/3.13-sysconfig.diff"\n    type :unofficial\n  end\nend\n'
        self.assertEqual(local_patches(source), ["Patches/python/3.13-sysconfig.diff"])
        self.assertEqual(local_patches(FORMULA), [])

    def test_refuses_paths_that_escape_patches(self):
        with self.assertRaises(SystemExit):
            local_patches('  patch do\n    file "Patches/../Formula/evil.rb"\n  end\n')


class AliasTest(unittest.TestCase):
    def test_writes_relative_symlinks_to_the_formula(self):
        with tempfile.TemporaryDirectory() as d:
            write_aliases("pkgconf", ["pkg-config", "pkgconfig"], Path(d))
            link = Path(d) / "Aliases" / "pkg-config"
            self.assertEqual(str(link.readlink() if hasattr(link, "readlink") else __import__("os").readlink(link)),
                             "../Formula/pkgconf.rb")
            self.assertTrue((Path(d) / "Aliases" / "pkgconfig").is_symlink())

    def test_refuses_path_like_aliases(self):
        with tempfile.TemporaryDirectory() as d, self.assertRaises(SystemExit):
            write_aliases("x", ["../../etc/passwd"], Path(d))


class RegistryTest(unittest.TestCase):
    PLAN = {
        "order": ["cmake", "calc"],
        "formulae": {"readline": {"version": "8.3", "mybrew_dependencies": [], "runtime": True},
                     "cmake": {"version": "4.4", "mybrew_dependencies": [], "runtime": False},
                     "calc": {"version": "2.17", "mybrew_dependencies": ["readline"], "runtime": True}},
        "bottles": {"cmake": {"filename": "cmake-4.4.sequoia.bottle.tar.gz", "sha256": "c0", "root_url": "https://r"},
                    "calc": {"filename": "calc-2.17.sequoia.bottle.tar.gz", "sha256": "f00", "root_url": "https://r"}},
    }

    def test_entries_only_for_built_formulae(self):
        result = entries(self.PLAN, {}, "now")
        self.assertEqual(sorted(result), ["calc", "cmake"])
        self.assertEqual(result["calc"]["mybrew_dependencies"], ["readline"])
        self.assertEqual(result["calc"]["bottles"]["sequoia"]["sha256"], "f00")
        self.assertTrue(result["cmake"]["build_time_only"])

    def test_load_registry_merges_legacy_file_and_per_formula_files(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)
            (path / "bottles.json").write_text(json.dumps({"calc": {"version": "1"}, "dos2unix": {"version": "7"}}))
            (path / "calc.json").write_text(json.dumps({"version": "2"}))
            self.assertEqual(load_registry(path), {"calc": {"version": "2"}, "dos2unix": {"version": "7"}})


def api(version, deps=(), tags=(), build=(), test=()):
    return {"versions": {"stable": version}, "revision": 0, "dependencies": list(deps),
            "build_dependencies": list(build), "test_dependencies": list(test),
            "bottle": {"stable": {"files": {t: {} for t in tags}}}}


class PlanTest(unittest.TestCase):
    API = {
        "calc": api("2.17", ["readline"], ["arm64_sequoia"]),
        "readline": api("8.3", [], ["arm64_sequoia"]),
        "jdupes": api("1.31", ["libjodycode"], ["arm64_sequoia"]),
        "libjodycode": api("4.0", [], ["sonoma", "arm64_sequoia"]),
        "lz4": api("1.10", [], ["arm64_sequoia"], build=["cmake"]),
        "cmake": api("4.4", [], ["arm64_sequoia"]),
        "zstd": api("1.5", ["lz4"], ["sonoma"], build=["not-in-api"]),
        "pkgconf": api("2.5", [], ["sonoma"]),
        "tool": api("1.0", [], ["arm64_sequoia"], build=["pkgconf"], test=["cmake"]),
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

    def test_build_only_dependency_without_bottle_is_built_first_but_not_runtime(self):
        result = self.plan("lz4")
        self.assertEqual(result["order"], ["cmake", "lz4"])
        self.assertFalse(result["formulae"]["cmake"]["runtime"])
        self.assertTrue(result["formulae"]["lz4"]["runtime"])
        self.assertEqual(result["formulae"]["lz4"]["mybrew_dependencies"], [])

    def test_build_dependencies_of_bottled_formulae_are_not_walked(self):
        result = self.plan("zstd")  # walking zstd's build deps would raise KeyError
        self.assertEqual(result["order"], ["cmake", "lz4"])
        self.assertEqual(result["formulae"]["zstd"]["mybrew_dependencies"], ["lz4"])

    def test_cached_build_dependency_is_reused(self):
        result = self.plan("lz4", {"cmake": {"version": "4.4"}})
        self.assertEqual(result["order"], ["lz4"])
        self.assertEqual(result["formulae"]["cmake"]["source"], "mybrew")

    def test_test_dependencies_are_walked_and_official_build_deps_left_to_brew(self):
        result = self.plan("tool")
        self.assertEqual(result["order"], ["cmake", "tool"])
        self.assertEqual(result["formulae"]["pkgconf"]["source"], "official")

    def test_requires_lists_every_edge_walked(self):
        result = self.plan("tool")
        self.assertEqual(result["formulae"]["tool"]["requires"], ["pkgconf", "cmake"])
        self.assertEqual(result["formulae"]["pkgconf"]["requires"], [])

    def test_single_refuses_when_dependencies_still_need_building(self):
        self.assertIn("readline", single_violation(self.plan("calc")))
        self.assertIsNone(single_violation(self.plan("calc", {"readline": {"version": "8.3"}})))

    def test_outdated_registry_entry_is_rebuilt(self):
        result = self.plan("calc", {"readline": {"version": "8.2"}})
        self.assertEqual(result["order"], ["readline", "calc"])


if __name__ == "__main__":
    unittest.main()
