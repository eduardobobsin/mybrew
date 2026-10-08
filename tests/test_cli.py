import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from mybrew_cli import MybrewError, brew_env, find_run, install_steps  # noqa: E402

TAP = "me/mybrew"


def plan_of(target, formulae, order=()):
    return {"target": target, "order": list(order),
            "formulae": {n: {"version": v, "source": s} for n, (v, s) in formulae.items()}}


CALC = plan_of("calc", {"readline": ("8.3", "mybrew"), "calc": ("2.17", "mybrew")})


class InstallStepsTest(unittest.TestCase):
    def steps(self, the_plan, installed=None):
        installed = installed or {}
        return install_steps(the_plan, TAP, installed.get)

    def test_fresh_machine_installs_dependencies_first(self):
        self.assertEqual(self.steps(CALC), [["brew", "install", "me/mybrew/readline"],
                                            ["brew", "install", "me/mybrew/calc"]])

    def test_matching_core_dependency_is_kept(self):
        steps = self.steps(CALC, {"readline": {"version": "8.3", "tap": "homebrew/core"}})
        self.assertEqual(steps, [["brew", "install", "me/mybrew/calc"]])

    def test_mismatched_core_dependency_is_not_touched(self):
        with self.assertRaisesRegex(MybrewError, "brew uninstall --ignore-dependencies readline"):
            self.steps(CALC, {"readline": {"version": "8.2", "tap": "homebrew/core"}})

    def test_outdated_mybrew_dependency_is_upgraded(self):
        steps = self.steps(CALC, {"readline": {"version": "8.2", "tap": TAP}})
        self.assertEqual(steps[0], ["brew", "upgrade", "me/mybrew/readline"])

    def test_already_installed_target_is_a_no_op(self):
        installed = {"readline": {"version": "8.3", "tap": TAP}, "calc": {"version": "2.17", "tap": TAP}}
        self.assertEqual(self.steps(CALC, installed), [])

    def test_official_target_uses_plain_brew_after_mybrew_deps(self):
        the_plan = plan_of("foo", {"bar": ("1", "mybrew"), "foo": ("2", "official")})
        self.assertEqual(self.steps(the_plan), [["brew", "install", "me/mybrew/bar"], ["brew", "install", "foo"]])

    def test_target_from_another_tap_is_refused(self):
        with self.assertRaisesRegex(MybrewError, "brew uninstall calc"):
            self.steps(CALC, {"readline": {"version": "8.3", "tap": TAP}, "calc": {"version": "2.17", "tap": "homebrew/core"}})

    def test_unbuilt_plan_is_refused(self):
        with self.assertRaises(MybrewError):
            self.steps(plan_of("calc", {"calc": ("2.17", "build")}, order=["calc"]))


class BrewEnvTest(unittest.TestCase):
    def test_defaults_auto_update_off(self):
        self.assertEqual(brew_env({})["HOMEBREW_NO_AUTO_UPDATE"], "1")

    def test_respects_users_choice(self):
        self.assertEqual(brew_env({"HOMEBREW_NO_AUTO_UPDATE": "0"})["HOMEBREW_NO_AUTO_UPDATE"], "0")


class FindRunTest(unittest.TestCase):
    RUNS = [
        {"databaseId": 1, "displayTitle": "Build calc", "createdAt": "2026-10-08T00:00:00Z"},
        {"databaseId": 2, "displayTitle": "Build jdupes", "createdAt": "2026-10-08T00:10:05Z"},
        {"databaseId": 3, "displayTitle": "Build calc", "createdAt": "2026-10-08T00:10:04Z"},
    ]
    SINCE = datetime(2026, 10, 8, 0, 10, tzinfo=timezone.utc)

    def test_picks_this_formulas_run_after_dispatch(self):
        self.assertEqual(find_run(self.RUNS, "calc", self.SINCE), 3)

    def test_ignores_older_runs(self):
        self.assertIsNone(find_run(self.RUNS[:1], "calc", self.SINCE))


if __name__ == "__main__":
    unittest.main()
