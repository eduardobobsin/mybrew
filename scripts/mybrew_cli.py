"""mybrew: install Homebrew formulae on Intel Macs, building missing bottles on demand.

  mybrew install <formula>...   install, building bottles on GitHub if none exist
  mybrew plan <formula>...      show where each piece would come from; change nothing
  mybrew <anything else>        passed through to brew

Options for install:
  --no-build    fail instead of starting a build on a cache miss
  --replace     swap a formula installed from another tap (e.g. homebrew/core)
                at a different version for the mybrew one; lists what uses it
                first. Without it mybrew never uninstalls anything.

On a cache miss each formula that needs a bottle is built as its own GitHub
run, as soon as its dependencies are published, several at a time. Ctrl-C
leaves running builds alone; running mybrew again picks them back up.

Environment:
  MYBREW_TAP       tap to use (default: the only tapped */homebrew-mybrew)
  MYBREW_PARALLEL  builds to run at once (default 4)
  HOMEBREW_NO_AUTO_UPDATE defaults to 1 for mybrew's own brew calls, since
                mybrew refreshes its tap itself; set it to 0 to keep auto-update.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from plan_build import load_registry, plan

WORKFLOW = "build.yml"
POLL_SECONDS = 10
FRAME_SECONDS = 0.1
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPINNER_ASCII = "|/-\\"
DEFAULT_PARALLEL = 4


class MybrewError(Exception):
    pass


def say(message: str) -> None:
    print(f"\033[1;34m==>\033[0m \033[1m{message}\033[0m", flush=True)


# --- environment -----------------------------------------------------------

@dataclass
class Tap:
    name: str  # owner/mybrew
    path: Path

    @property
    def repo(self) -> str:
        owner, short = self.name.split("/")
        return f"{owner}/homebrew-{short}"

    @property
    def registry_dir(self) -> Path:
        return self.path / "registry"


def run(*cmd: str, capture: bool = True) -> str:
    result = subprocess.run(cmd, check=False, text=True, capture_output=capture)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() if capture else ""
        raise MybrewError(f"`{' '.join(cmd)}` failed{': ' + detail if detail else ''}")
    return result.stdout if capture else ""


def find_tap(brew_repository: Path, env: dict) -> Tap:
    taps = brew_repository / "Library" / "Taps"
    if env.get("MYBREW_TAP"):
        owner, short = env["MYBREW_TAP"].lower().split("/")
        path = taps / owner / f"homebrew-{short}"
        if not path.is_dir():
            raise MybrewError(f"{env['MYBREW_TAP']} is not tapped; run `brew tap {env['MYBREW_TAP']}`")
        return Tap(f"{owner}/{short}", path)
    found = sorted(taps.glob("*/homebrew-mybrew"))
    if len(found) != 1:
        raise MybrewError("set MYBREW_TAP=<owner>/mybrew" if found else
                          "no mybrew tap found; run `brew tap <owner>/mybrew` first")
    return Tap(f"{found[0].parent.name}/mybrew", found[0])


def installed_kegs(cellar: Path, name: str) -> dict | None:
    """Return {"version", "tap"} of the newest installed keg of <name>, if any."""
    kegs = sorted((cellar / name).glob("*/INSTALL_RECEIPT.json"), key=lambda p: p.stat().st_mtime)
    if not kegs:
        return None
    receipt = json.loads(kegs[-1].read_text())
    return {"version": kegs[-1].parent.name, "tap": (receipt.get("source") or {}).get("tap")}


# --- decisions (pure) ------------------------------------------------------

def install_steps(the_plan: dict, tap: str, installed, replace: bool = False) -> list[list[str]]:
    """brew commands that install the plan's target without compiling.

    `installed(name)` returns {"version", "tap"} or None. mybrew formulae are
    drop-in replacements for core ones, so an already-installed keg of the
    right version satisfies a dependency whichever tap it came from. A keg of
    another version from another tap is only swapped out when `replace` is set.
    """
    target = the_plan["target"]
    formulae = the_plan["formulae"]
    if the_plan["order"]:
        raise MybrewError(f"no bottle yet for: {' '.join(the_plan['order'])}")

    steps = []
    for name, info in formulae.items():
        if info["source"] == "official" or name == target or not info.get("runtime", True):
            continue
        current = installed(name)
        if current is None:
            steps.append(["brew", "install", f"{tap}/{name}"])
        elif current["version"] != info["version"]:
            if current["tap"] == tap:
                steps.append(["brew", "upgrade", f"{tap}/{name}"])
            elif replace:
                steps += [["brew", "uninstall", "--formula", "--ignore-dependencies", name],
                          ["brew", "install", f"{tap}/{name}"]]
            else:
                raise MybrewError(
                    f"{name} {current['version']} is installed from {current['tap']}, but {target} needs "
                    f"{info['version']} from {tap}. Run mybrew again with --replace to swap it, or "
                    f"`brew uninstall --ignore-dependencies {name}` yourself.")

    info = formulae[target]
    current = installed(target)
    if info["source"] == "official":
        steps.append(["brew", "install", target])
    elif current and current["tap"] == tap and current["version"] == info["version"]:
        pass
    elif current and current["tap"] != tap:
        if not replace:
            raise MybrewError(f"{target} is installed from {current['tap']}; run mybrew again with "
                              f"--replace to switch it to {tap}")
        steps += [["brew", "uninstall", "--formula", "--ignore-dependencies", target],
                  ["brew", "install", f"{tap}/{target}"]]
    else:
        steps.append(["brew", "install", f"{tap}/{target}"])
    return steps


def describe(the_plan: dict) -> list[str]:
    labels = {"official": "official bottle", "mybrew": "mybrew bottle", "build": "needs build"}
    return [f"{name} {info['version']}: {labels[info['source']]}{'' if info.get('runtime', True) else ' (build-time only)'}"
            for name, info in the_plan["formulae"].items()]


def find_run(runs: list[dict], formula: str, since: datetime) -> int | None:
    """Newest dispatch run for <formula> created at or after <since>."""
    title = f"Build {formula}"
    matches = [r for r in runs if r["displayTitle"] == title
               and datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00")) >= since]
    return max(matches, key=lambda r: r["createdAt"])["databaseId"] if matches else None


def active_runs(runs: list[dict]) -> dict[str, int]:
    """formula -> newest unfinished run, for attaching instead of duplicating."""
    result: dict[str, tuple[str, int]] = {}
    for r in runs:
        if r["status"] != "completed" and r["displayTitle"].startswith("Build "):
            name = r["displayTitle"][len("Build "):]
            if name not in result or r["createdAt"] > result[name][0]:
                result[name] = (r["createdAt"], r["databaseId"])
    return {name: run_id for name, (_, run_id) in result.items()}


# --- build scheduling (pure) ------------------------------------------------

WAITING, QUEUED, DONE, FAILED, SKIPPED = "waiting", "queued", "done", "failed", "skipped"
RUNNING = ("dispatched", QUEUED, "building", "publishing", "verifying")


def graph(the_plan: dict) -> dict[str, set[str]]:
    """For each formula to build, the other to-build formulae it waits for.

    Edges are followed through formulae that come from bottles: libvmaf needs
    meson (official bottle), which needs python@3.14 (to build), so libvmaf
    waits for python@3.14. The walk stops at a to-build formula, which waits
    for its own dependencies.
    """
    formulae, order = the_plan["formulae"], set(the_plan["order"])

    def reachable(name: str) -> set[str]:
        found: set[str] = set()
        stack, seen = list(formulae[name].get("requires", [])), set()
        while stack:
            dep = stack.pop()
            if dep in seen:
                continue
            seen.add(dep)
            if dep in order:
                found.add(dep)
            else:
                stack.extend(formulae[dep].get("requires", []))
        return found

    return {n: reachable(n) for n in the_plan["order"]}


def advance(deps: dict[str, set[str]], states: dict[str, str], parallel: int) -> list[str]:
    """Mark formulae blocked by a failure as skipped; return the ones to start now."""
    changed = True
    while changed:
        changed = False
        for name, needs in deps.items():
            if states[name] == WAITING and any(states[d] in (FAILED, SKIPPED) for d in needs):
                states[name] = SKIPPED
                changed = True
    slots = parallel - sum(1 for s in states.values() if s in RUNNING)
    ready = [n for n, needs in deps.items() if states[n] == WAITING and all(states[d] == DONE for d in needs)]
    return ready[:max(slots, 0)]


def finished(states: dict[str, str]) -> bool:
    return all(s in (DONE, FAILED, SKIPPED) for s in states.values())


def phase_of(view: dict) -> str:
    """Map a run's jobs to a phase: queued, building, publishing, verifying, done, failed."""
    if view["status"] == "completed":
        return DONE if view["conclusion"] == "success" else FAILED
    names = {"build": "building", "publish": "publishing", "verify": "verifying"}
    current = QUEUED
    for job in view.get("jobs", []):
        reached = job["status"] == "in_progress" or (job["status"] == "completed" and job["conclusion"] != "skipped")
        if reached:
            current = names.get(job["name"], current)
    return current


def step_progress(view: dict) -> tuple[int, int] | None:
    """(finished, total) steps of the job currently running, if any."""
    for job in view.get("jobs", []):
        if job["status"] == "in_progress" and job.get("steps"):
            done = sum(1 for step in job["steps"] if step["status"] == "completed")
            return done, len(job["steps"])
    return None


def spinner_frames(encoding: str | None) -> str:
    return SPINNER if "utf" in (encoding or "").lower() else SPINNER_ASCII


ICONS = {DONE: "\033[32m✔\033[0m", FAILED: "\033[31m✘\033[0m", SKIPPED: "\033[33m–\033[0m",
         WAITING: "\033[2m⏸\033[0m"}


def elapsed(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def render(the_plan: dict, deps: dict[str, set[str]], states: dict[str, str],
           started: dict[str, float], ended: dict[str, float], now: float,
           progress: dict[str, tuple[int, int]] | None = None, spinner: str | None = None) -> list[tuple[str, str]]:
    """(state key, line) per formula; the key changes only when the state does.

    `spinner` is the current animation frame for running formulae (None draws a
    static icon); `progress` holds (finished, total) steps of each running job.
    """
    progress = progress or {}
    width = max(len(n) for n in the_plan["formulae"])
    lines = []
    for name, info in the_plan["formulae"].items():
        note = "" if info.get("runtime", True) else "  (build-time)"
        if name not in deps:
            label = "official bottle" if info["source"] == "official" else "mybrew bottle"
            lines.append((f"{name}:static", f"  {ICONS[DONE]} {name:<{width}}  {label}{note}"))
            continue
        state = states[name]
        if state == WAITING:
            waiting = sorted(d for d in deps[name] if states[d] != DONE)
            text = f"waiting for {', '.join(waiting)}" if waiting else "ready"
        elif state == SKIPPED:
            text = "skipped: a dependency failed"
        elif state in (DONE, FAILED):
            text = f"{'built' if state == DONE else 'failed'} in {elapsed(ended[name] - started[name])}"
        else:
            steps = f" [{progress[name][0]}/{progress[name][1]}]" if name in progress else ""
            text = f"{state}{steps} {elapsed(now - started[name])}"
        icon = ICONS.get(state, f"\033[34m{spinner or '⟳'}\033[0m")
        key = f"{name}:{state}:{text if state == WAITING else ''}"
        lines.append((key, f"  {icon} {name:<{width}}  {text}{note}"))
    return lines


# --- GitHub ----------------------------------------------------------------

def require_gh() -> None:
    try:
        run("gh", "auth", "status")
    except (MybrewError, FileNotFoundError) as error:
        raise MybrewError("building needs the GitHub CLI, logged in: `gh auth login`") from error


def list_runs(repo: str) -> list[dict]:
    return json.loads(run("gh", "run", "list", "--repo", repo, "--workflow", WORKFLOW, "--event",
                          "workflow_dispatch", "--limit", "50", "--json", "databaseId,displayTitle,createdAt,status"))


class Display:
    """Redraws the status block in place on a terminal; prints changes otherwise."""

    def __init__(self, stream=sys.stdout, env: dict | None = None):
        env = os.environ if env is None else env
        self.stream, self.tty, self.drawn, self.printed = stream, stream.isatty(), 0, set()
        self.animate = self.tty and not env.get("NO_COLOR") and env.get("TERM") != "dumb"
        self.frames = spinner_frames(getattr(stream, "encoding", None))

    def show(self, rows: list[tuple[str, str]]) -> None:
        if self.tty:
            if self.drawn:
                self.stream.write(f"\033[{self.drawn}F")
            for _, line in rows:
                self.stream.write(f"\033[2K{line}\n")
            self.drawn = len(rows)
        else:
            for key, line in rows:
                if key not in self.printed:
                    self.printed.add(key)
                    self.stream.write(line + "\n")
        self.stream.flush()


class GitHub:
    """The three gh calls the scheduler needs; tests substitute a fake."""

    def __init__(self, repo: str):
        self.repo = repo

    def dispatch(self, formula: str, verify: bool) -> None:
        run("gh", "workflow", "run", WORKFLOW, "--repo", self.repo, "-f", f"formula={formula}",
            "-f", "single=true", "-f", f"verify={'true' if verify else 'false'}")

    def runs(self) -> list[dict]:
        return list_runs(self.repo)

    def view(self, run_id: int) -> dict:
        return json.loads(run("gh", "run", "view", str(run_id), "--repo", self.repo,
                              "--json", "status,conclusion,jobs"))


class Scheduler:
    """Builds a plan's missing bottles as one run per formula, dependencies first.

    All GitHub traffic happens in poll(), which a background thread calls every
    POLL_SECONDS; status checks for active runs go out in parallel. snapshot()
    gives the display a consistent copy at any moment, so the spinner never
    waits on the network.
    """

    def __init__(self, the_plan: dict, github, parallel: int, clock=time.time):
        self.plan, self.github, self.parallel, self.clock = the_plan, github, parallel, clock
        self.deps = graph(the_plan)
        self.states = {n: WAITING for n in self.deps}
        self.runs: dict[str, int | None] = {}
        self.since: dict[str, datetime] = {}
        self.started: dict[str, float] = {}
        self.ended: dict[str, float] = {}
        self.progress: dict[str, tuple[int, int]] = {}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=max(parallel, 1))

    def attach(self) -> None:
        """Pick up runs already in flight, e.g. after Ctrl-C and a rerun."""
        for name, run_id in active_runs(self.github.runs()).items():
            if name in self.states:
                self.states[name], self.runs[name], self.started[name] = "dispatched", run_id, self.clock()

    def poll(self) -> None:
        with self.lock:
            ready = advance(self.deps, self.states, self.parallel)
        for name in ready:
            since = datetime.now(timezone.utc) - timedelta(seconds=30)
            self.github.dispatch(name, verify=name == self.plan["target"])
            with self.lock:
                self.since[name] = since
                self.states[name], self.runs[name], self.started[name] = "dispatched", None, self.clock()

        with self.lock:
            unresolved = [n for n, r in self.runs.items() if r is None]
        if unresolved:
            listed = self.github.runs()
            with self.lock:
                for name in unresolved:
                    self.runs[name] = find_run(listed, name, self.since[name])

        with self.lock:
            active = [(n, r) for n, r in self.runs.items() if r is not None and self.states[n] not in (DONE, FAILED)]
        views = list(self.pool.map(lambda item: (item[0], self.github.view(item[1])), active))
        with self.lock:
            for name, view in views:
                self.states[name] = phase_of(view)
                if self.states[name] in (DONE, FAILED):
                    self.ended[name] = self.clock()
                    self.progress.pop(name, None)
                elif (steps := step_progress(view)):
                    self.progress[name] = steps

    def finished(self) -> bool:
        with self.lock:
            return finished(self.states)

    def snapshot(self) -> tuple:
        with self.lock:
            return (dict(self.states), dict(self.started), dict(self.ended), dict(self.progress))

    def failures(self) -> dict[str, int | None]:
        return {n: self.runs.get(n) for n, s in self.states.items() if s == FAILED}


def build_missing(repo: str, the_plan: dict, parallel: int) -> None:
    """Build every formula in the plan's order as its own run, dependencies first."""
    scheduler = Scheduler(the_plan, GitHub(repo), parallel)
    display = Display()
    stop, error = threading.Event(), []

    def poll_loop() -> None:
        try:
            scheduler.attach()
            while not stop.is_set():
                scheduler.poll()
                if scheduler.finished():
                    break
                stop.wait(POLL_SECONDS)
        except Exception as exc:  # surfaced on the main thread
            error.append(exc)

    poller = threading.Thread(target=poll_loop, name="mybrew-poll", daemon=True)
    poller.start()
    try:
        frame = 0
        while poller.is_alive():
            states, started, ended, progress = scheduler.snapshot()
            spinner = display.frames[frame % len(display.frames)] if display.animate else None
            display.show(render(the_plan, scheduler.deps, states, started, ended, time.time(), progress, spinner))
            frame += 1
            poller.join(FRAME_SECONDS if display.animate else 1.0)
        states, started, ended, progress = scheduler.snapshot()
        display.show(render(the_plan, scheduler.deps, states, started, ended, time.time(), progress, None))
    except KeyboardInterrupt:
        stop.set()
        raise MybrewError("interrupted; builds already started keep running on GitHub. "
                          "Run mybrew again to pick them up.") from None
    finally:
        scheduler.pool.shutdown(wait=False)

    if error:
        raise error[0] if isinstance(error[0], MybrewError) else MybrewError(f"polling GitHub failed: {error[0]}")
    failed = scheduler.failures()
    if failed:
        links = "\n  ".join(f"{n}: https://github.com/{repo}/actions/runs/{r}" for n, r in failed.items())
        raise MybrewError(f"build failed:\n  {links}")


# --- commands --------------------------------------------------------------

def load_plan(tap: Tap, formula: str) -> dict:
    run("git", "-C", str(tap.path), "pull", "--ff-only", "--quiet")
    return plan(formula, load_registry(tap.registry_dir))


def cmd_plan(tap: Tap, formulae: list[str]) -> None:
    for formula in formulae:
        say(f"{formula}")
        for line in describe(load_plan(tap, formula)):
            print(f"    {line}")


def cmd_install(tap: Tap, cellar: Path, formulae: list[str], allow_build: bool, parallel: int,
                replace: bool = False) -> None:
    for formula in formulae:
        say(f"Checking {formula}")
        the_plan = load_plan(tap, formula)

        if the_plan["order"]:
            if not allow_build:
                raise MybrewError(f"no bottle for {' '.join(the_plan['order'])} (--no-build given)")
            require_gh()
            say(f"Building {len(the_plan['order'])} bottle(s) on github.com/{tap.repo}")
            build_missing(tap.repo, the_plan, parallel)
            the_plan = load_plan(tap, formula)
        else:
            for line in describe(the_plan):
                print(f"    {line}")

        steps = install_steps(the_plan, tap.name, lambda name: installed_kegs(cellar, name), replace)
        if not steps:
            say(f"{formula} is already installed")
        for step in steps:
            if step[1] == "uninstall":
                name = step[-1]
                current = installed_kegs(cellar, name) or {}
                users = run("brew", "uses", "--installed", name).split()
                say(f"Replacing {name} {current.get('version', '')} from {current.get('tap', '?')} "
                    f"with {the_plan['formulae'][name]['version']} from {tap.name}"
                    + (f" (used by: {', '.join(users)})" if users else ""))
            say(" ".join(step))
            run(*step, capture=False)


def brew_env(env: dict) -> dict:
    """The tap was just refreshed; skip brew's own auto-update unless asked for.
    Never let an uninstall mybrew runs autoremove unrelated formulae."""
    return {"HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_AUTOREMOVE": "1", **env}


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    command, rest = argv[0], argv[1:]
    if command not in ("install", "plan"):
        os.execvp("brew", ["brew", *argv])

    names = [a for a in rest if not a.startswith("-")]
    unknown = [a for a in rest if a.startswith("-") and a not in ("--no-build", "--replace")]
    if unknown or not names:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    os.environ.update(brew_env(dict(os.environ)))
    try:
        tap = find_tap(Path(run("brew", "--repository").strip()), os.environ)
        if command == "plan":
            cmd_plan(tap, names)
        else:
            cellar = Path(run("brew", "--cellar").strip())
            parallel = int(os.environ.get("MYBREW_PARALLEL", DEFAULT_PARALLEL))
            cmd_install(tap, cellar, names, allow_build="--no-build" not in rest, parallel=parallel,
                        replace="--replace" in rest)
    except MybrewError as error:
        print(f"\033[1;31mError:\033[0m {error}", file=sys.stderr)
        return 1
    return 0
