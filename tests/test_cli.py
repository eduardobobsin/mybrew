import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import io  # noqa: E402

from mybrew_cli import (  # noqa: E402
    DONE, FAILED, SKIPPED, WAITING, Display, MybrewError, active_runs, advance, brew_env, find_run, finished,
    Scheduler, core_name, graph, install_steps, merge_plans, unique, fit, phase_of, render, spinner_frames, truncate,
)
import threading  # noqa: E402
import time  # noqa: E402

TAP = "me/mybrew"


def plan_of(target, formulae, order=(), build_time=()):
    return {"target": target, "order": list(order),
            "formulae": {n: {"version": v, "source": s, "runtime": n not in build_time} for n, (v, s) in formulae.items()}}


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
        with self.assertRaisesRegex(MybrewError, "--replace"):
            self.steps(CALC, {"readline": {"version": "8.2", "tap": "homebrew/core"}})

    def test_replace_swaps_mismatched_core_dependency(self):
        steps = install_steps(CALC, TAP, {"readline": {"version": "8.2", "tap": "homebrew/core"}}.get, replace=True)
        self.assertEqual(steps, [["brew", "uninstall", "--formula", "--ignore-dependencies", "readline"],
                                 ["brew", "install", "me/mybrew/readline"],
                                 ["brew", "install", "me/mybrew/calc"]])

    def test_replace_keeps_matching_core_dependency(self):
        steps = install_steps(CALC, TAP, {"readline": {"version": "8.3", "tap": "homebrew/core"}}.get, replace=True)
        self.assertEqual(steps, [["brew", "install", "me/mybrew/calc"]])

    def test_replace_switches_target_from_another_tap(self):
        installed = {"readline": {"version": "8.3", "tap": TAP}, "calc": {"version": "2.16", "tap": "homebrew/core"}}
        steps = install_steps(CALC, TAP, installed.get, replace=True)
        self.assertEqual(steps[0], ["brew", "uninstall", "--formula", "--ignore-dependencies", "calc"])

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
        with self.assertRaisesRegex(MybrewError, "--replace"):
            self.steps(CALC, {"readline": {"version": "8.3", "tap": TAP}, "calc": {"version": "2.17", "tap": "homebrew/core"}})

    def test_build_time_only_formulae_are_not_installed(self):
        the_plan = plan_of("lz4", {"cmake": ("4.4", "mybrew"), "lz4": ("1.10", "mybrew")}, build_time=["cmake"])
        self.assertEqual(self.steps(the_plan), [["brew", "install", "me/mybrew/lz4"]])

    def test_unbuilt_plan_is_refused(self):
        with self.assertRaises(MybrewError):
            self.steps(plan_of("calc", {"calc": ("2.17", "build")}, order=["calc"]))


class BrewEnvTest(unittest.TestCase):
    def test_defaults_auto_update_off(self):
        self.assertEqual(brew_env({})["HOMEBREW_NO_AUTO_UPDATE"], "1")

    def test_defaults_autoremove_off(self):
        self.assertEqual(brew_env({})["HOMEBREW_NO_AUTOREMOVE"], "1")

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


LIBZIP = {
    "target": "libzip",
    "order": ["xz", "cmake", "lz4", "libzip"],
    "formulae": {
        "xz": {"version": "5.8", "source": "build", "runtime": True, "requires": []},
        "cmake": {"version": "4.4", "source": "build", "runtime": False, "requires": []},
        "lz4": {"version": "1.10", "source": "build", "runtime": True, "requires": ["cmake"]},
        "zstd": {"version": "1.5", "source": "official", "runtime": True, "requires": []},
        "libzip": {"version": "1.12", "source": "build", "runtime": True, "requires": ["xz", "lz4", "zstd"]},
    },
}


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.deps = graph(LIBZIP)
        self.states = {n: WAITING for n in self.deps}

    def test_graph_keeps_only_edges_to_formulae_being_built(self):
        self.assertEqual(self.deps["libzip"], {"xz", "lz4"})
        self.assertEqual(self.deps["lz4"], {"cmake"})

    def test_waits_for_builds_hidden_behind_bottled_formulae(self):
        the_plan = {
            "target": "libvmaf", "order": ["python@3.14", "libvmaf"],
            "formulae": {
                "python@3.14": {"source": "build", "requires": []},
                "meson": {"source": "official", "requires": ["python@3.14"]},
                "ninja": {"source": "mybrew", "requires": []},
                "libvmaf": {"source": "build", "requires": ["meson", "ninja"]},
            },
        }
        self.assertEqual(graph(the_plan)["libvmaf"], {"python@3.14"})

    def test_independent_leaves_start_together(self):
        self.assertEqual(advance(self.deps, self.states, 4), ["xz", "cmake"])

    def test_parallel_limit_is_respected(self):
        self.assertEqual(advance(self.deps, self.states, 1), ["xz"])
        self.states["xz"] = "building"
        self.assertEqual(advance(self.deps, self.states, 1), [])

    def test_dependents_start_once_dependencies_are_done(self):
        self.states.update(xz=DONE, cmake=DONE)
        self.assertEqual(advance(self.deps, self.states, 4), ["lz4"])
        self.states["lz4"] = DONE
        self.assertEqual(advance(self.deps, self.states, 4), ["libzip"])

    def test_failure_skips_everything_downstream_but_not_siblings(self):
        self.states.update(cmake=FAILED, xz="building")
        self.assertEqual(advance(self.deps, self.states, 4), [])
        self.assertEqual(self.states["lz4"], SKIPPED)
        self.assertEqual(self.states["libzip"], SKIPPED)
        self.assertFalse(finished(self.states))
        self.states["xz"] = DONE
        self.assertTrue(finished(self.states))


class PhaseTest(unittest.TestCase):
    def view(self, status="in_progress", conclusion="", **jobs):
        return {"status": status, "conclusion": conclusion,
                "jobs": [{"name": n, "status": s.split("/")[0], "conclusion": (s.split("/") + [""])[1]}
                         for n, s in jobs.items()]}

    def test_phases_follow_the_jobs(self):
        self.assertEqual(phase_of(self.view(status="queued")), "queued")
        self.assertEqual(phase_of(self.view(build="in_progress")), "building")
        self.assertEqual(phase_of(self.view(build="completed/success", publish="in_progress")), "publishing")
        self.assertEqual(phase_of(self.view(build="completed/success", publish="completed/success",
                                            verify="in_progress")), "verifying")

    def test_skipped_job_is_not_a_phase(self):
        view = self.view(build="completed/success", publish="completed/success", verify="completed/skipped")
        self.assertEqual(phase_of(view), "publishing")

    def test_completed_run(self):
        self.assertEqual(phase_of(self.view("completed", "success")), DONE)
        self.assertEqual(phase_of(self.view("completed", "failure")), FAILED)


class RenderTest(unittest.TestCase):
    def rows(self, states, live=None, history=None, now=840):
        return render(LIBZIP, graph(LIBZIP), states, {"xz": 0, "cmake": 0, "lz4": 0, "libzip": 0}, {"xz": 192, "cmake": 300, "lz4": 300, "libzip": 300},
                      now, live or {}, "⠹", history or {})  # noqa: E501

    def line(self, rows, name):
        return next(line for _, line in rows if f" {name} " in line)

    def test_lines_describe_each_formula(self):
        states = {"xz": DONE, "cmake": "building", "lz4": WAITING, "libzip": WAITING}
        text = "\n".join(line for _, line in self.rows(states, {"cmake": {"phase": "compiling"}}))
        self.assertIn("xz      built in 3m12s", text)
        self.assertIn("cmake   compiling   [5/9]", text)
        self.assertIn("14m00s elapsed", text)
        self.assertIn("lz4     waiting for cmake", text)
        self.assertIn("zstd    official bottle", text)

    def test_target_pipeline_has_a_verifying_phase(self):
        states = {"xz": DONE, "cmake": DONE, "lz4": DONE, "libzip": "verifying"}
        self.assertIn("verifying   [10/10]", self.line(self.rows(states), "libzip"))

    def test_bar_and_time_left_from_previous_build(self):
        states = {"xz": DONE, "cmake": "building", "lz4": WAITING, "libzip": WAITING}
        live = {"cmake": {"phase": "compiling", "build_started": 240}}
        line = self.line(self.rows(states, live, {"cmake": 1000}), "cmake")
        self.assertIn("[██████░░░░]  60%", line)
        self.assertIn("14m00s elapsed · ~6m40s left", line)

    def test_overrun_reads_almost_done(self):
        states = {"xz": DONE, "cmake": "building", "lz4": WAITING, "libzip": WAITING}
        line = self.line(self.rows(states, {"cmake": {"build_started": 0}}, {"cmake": 600}), "cmake")
        self.assertIn(" 99%", line)
        self.assertIn("almost done", line)

    def test_no_history_shows_elapsed_only_in_aligned_columns(self):
        states = {"xz": DONE, "cmake": "building", "lz4": "building", "libzip": WAITING}
        live = {"cmake": {"phase": "compiling", "build_started": 0}, "lz4": {"phase": "configuring"}}
        rows = self.rows(states, live)
        a, b = self.line(rows, "cmake"), self.line(rows, "lz4")
        self.assertNotIn("%", a)
        self.assertEqual(a.index("elapsed"), b.index("elapsed"))

    def test_stale_log_is_flagged(self):
        states = {"xz": DONE, "cmake": "building", "lz4": WAITING, "libzip": WAITING}
        line = self.line(self.rows(states, {"cmake": {"phase": "compiling", "log_at": 840 - 17 * 60}}), "cmake")
        self.assertIn("log 17m00s old", line)

    def test_non_terminal_prints_only_state_and_phase_changes(self):
        out = io.StringIO()
        display = Display(out)
        states = {"xz": "building", "cmake": "building", "lz4": WAITING, "libzip": WAITING}
        display.show(self.rows(states, {"xz": {"phase": "compiling"}}, now=10))
        display.show(self.rows(states, {"xz": {"phase": "compiling"}}, now=20))
        display.show(self.rows(states, {"xz": {"phase": "testing"}}, now=30))
        self.assertEqual(out.getvalue().count(" xz "), 2)


class FitTest(unittest.TestCase):
    ROWS = [("a:static", "  ✔ a  official bottle"), ("b:static", "  ✔ b  mybrew bottle"),
            ("c:static", "  ✔ c  official bottle"), ("x:building:compiling:", "  ⠹ x  compiling"),
            ("y:waiting:waiting:", "  ⏸ y  waiting for x")]

    def test_bottle_rows_fold_into_one_summary(self):
        lines = fit(self.ROWS, 100, 40)
        self.assertEqual(len(lines), 3)
        self.assertIn("3 from bottles: 2 official, 1 mybrew", lines[-1])

    def test_block_never_exceeds_terminal_height(self):
        rows = [(f"n{i}:building:compiling:", f"  ⠹ n{i}") for i in range(50)]
        lines = fit(rows, 100, 10)
        self.assertEqual(len(lines), 9)
        self.assertIn("… and 42 more", lines[-1])

    def test_lines_never_exceed_terminal_width(self):
        line = "\x1b[34m⠹\x1b[0m " + "x" * 200
        cut = truncate(line, 40)
        visible = __import__("re").sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", cut)
        self.assertEqual(len(visible), 41)  # 40 characters and the ellipsis
        self.assertTrue(visible.endswith("…"))
        self.assertEqual(truncate("short", 40), "short")

    def test_redraw_lands_on_the_previous_frame(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True
        out = Tty()
        display = Display(out, {"TERM": "xterm"})
        display.show(self.ROWS)
        display.show(self.ROWS[:4])  # a shorter frame
        frames = out.getvalue().split("\x1b[3F")
        self.assertEqual(len(frames), 2)  # moved up exactly the 3 lines drawn before
        self.assertEqual(frames[1].count("\n"), 3)  # and overwrote all 3, blanking the leftover
        display.close()
        self.assertTrue(out.getvalue().endswith("\x1b[?7h"))


class AnimationTest(unittest.TestCase):
    def test_running_line_shows_spinner_frame(self):
        states = {"xz": "building", "cmake": WAITING, "lz4": WAITING, "libzip": WAITING}
        rows = render(LIBZIP, graph(LIBZIP), states, {"xz": 0}, {}, 75, {"xz": {"phase": "fetching"}}, "⠹")
        line = next(line for _, line in rows if " xz " in line)
        self.assertIn("⠹", line)
        self.assertIn("fetching    [2/9]", line)

    def test_ascii_fallback_and_no_animation_off_terminal(self):
        self.assertEqual(spinner_frames("US-ASCII"), "|/-\\")
        self.assertEqual(spinner_frames("utf-8")[0], "⠋")
        self.assertFalse(Display(io.StringIO(), {}).animate)


class FakeGitHub:
    """Each dispatched run finishes after `polls_to_finish` status checks."""

    def __init__(self, delay=0.0, fail=(), polls_to_finish=2):
        self.delay, self.fail, self.polls_to_finish = delay, set(fail), polls_to_finish
        self.dispatched, self.created, self.checks = [], {}, {}
        self.in_flight = self.max_in_flight = 0
        self.lock = threading.Lock()

    def dispatch(self, formula, verify):
        with self.lock:
            self.dispatched.append((formula, verify))
            self.created[formula] = len(self.created) + 1

    def runs(self):
        return [{"databaseId": i, "displayTitle": f"Build {n}", "createdAt": "2999-01-01T00:00:00Z",
                 "status": "in_progress"} for n, i in self.created.items()]

    def view(self, run_id):
        name = next(n for n, i in self.created.items() if i == run_id)
        with self.lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.checks[name] = self.checks.get(name, 0) + 1
            count = self.checks[name]
        time.sleep(self.delay)
        with self.lock:
            self.in_flight -= 1
        if count < self.polls_to_finish:
            return {"status": "in_progress", "conclusion": "", "jobs": [{"name": "build", "status": "in_progress", "steps": []}]}
        return {"status": "completed", "conclusion": "failure" if name in self.fail else "success", "jobs": []}


class SchedulerClassTest(unittest.TestCase):
    def run_to_end(self, github, parallel=4):
        scheduler = Scheduler(LIBZIP, github, parallel)
        for _ in range(20):
            scheduler.poll()
            if scheduler.finished():
                break
        return scheduler

    def test_full_run_respects_dependency_order_and_verifies_only_target(self):
        github = FakeGitHub()
        self.run_to_end(github)
        order = [name for name, _ in github.dispatched]
        self.assertEqual(set(order[:2]), {"xz", "cmake"})
        self.assertLess(order.index("cmake"), order.index("lz4"))
        self.assertEqual(order[-1], "libzip")
        self.assertEqual([n for n, verify in github.dispatched if verify], ["libzip"])

    def test_status_checks_run_in_parallel(self):
        github = FakeGitHub(delay=0.3)
        scheduler = Scheduler(LIBZIP, github, 4)
        scheduler.poll()  # dispatches xz and cmake
        begin = time.time()
        scheduler.poll()  # checks both
        self.assertEqual(github.max_in_flight, 2)
        self.assertLess(time.time() - begin, 0.55)

    def test_snapshot_never_waits_on_the_network(self):
        github = FakeGitHub(delay=0.5)
        scheduler = Scheduler(LIBZIP, github, 4)
        scheduler.poll()
        worker = threading.Thread(target=scheduler.poll)
        worker.start()
        time.sleep(0.1)  # poll is now inside the slow status checks
        begin = time.time()
        scheduler.snapshot()
        self.assertLess(time.time() - begin, 0.05)
        worker.join()

    def test_failure_skips_dependents_only(self):
        scheduler = self.run_to_end(FakeGitHub(fail={"cmake"}))
        states = scheduler.snapshot()[0]
        self.assertEqual(states["cmake"], FAILED)
        self.assertEqual(states["xz"], DONE)
        self.assertEqual((states["lz4"], states["libzip"]), (SKIPPED, SKIPPED))
        self.assertEqual(list(scheduler.failures()), ["cmake"])


class CoreNameTest(unittest.TestCase):
    def test_plain_and_core_qualified_names(self):
        self.assertEqual(core_name("libzip"), "libzip")
        self.assertEqual(core_name("homebrew/core/libzip"), "libzip")

    def test_other_taps_are_recognised(self):
        self.assertIsNone(core_name("shivammathur/php/php@7.4"))

    def test_malformed_names_are_refused(self):
        with self.assertRaises(MybrewError):
            core_name("a/b")


class ThirdPartyTest(unittest.TestCase):
    def test_merged_plan_keeps_dependency_order_and_has_no_target(self):
        gd = {"target": "gd", "order": ["libpng", "gd"], "formulae": {
            "libpng": {"version": "1", "source": "build", "requires": []},
            "gd": {"version": "2", "source": "build", "requires": ["libpng"]}}}
        curl = {"target": "curl", "order": ["libpng", "curl"], "formulae": {
            "libpng": {"version": "1", "source": "build", "requires": []},
            "zstd": {"version": "1", "source": "official", "requires": []},
            "curl": {"version": "8", "source": "build", "requires": ["libpng", "zstd"]}}}
        merged = merge_plans([gd, curl])
        self.assertIsNone(merged["target"])
        self.assertEqual(merged["order"], ["libpng", "gd", "curl"])
        self.assertEqual(graph(merged)["curl"], {"libpng"})

    def test_unique_keeps_first_occurrence(self):
        steps = [["brew", "install", "a"], ["brew", "install", "b"], ["brew", "install", "a"]]
        self.assertEqual(unique(steps), [["brew", "install", "a"], ["brew", "install", "b"]])


class ActiveRunsTest(unittest.TestCase):
    def test_newest_unfinished_run_per_formula(self):
        runs = [
            {"databaseId": 1, "displayTitle": "Build cmake", "createdAt": "2026-10-08T01:00:00Z", "status": "in_progress"},
            {"databaseId": 2, "displayTitle": "Build cmake", "createdAt": "2026-10-08T01:05:00Z", "status": "queued"},
            {"databaseId": 3, "displayTitle": "Build xz", "createdAt": "2026-10-08T01:05:00Z", "status": "completed"},
        ]
        self.assertEqual(active_runs(runs), {"cmake": 2})


if __name__ == "__main__":
    unittest.main()
