"""mybrew: install Homebrew formulae on Intel Macs, building missing bottles on demand.

  mybrew install <formula>...   install, building bottles on GitHub if none exist
  mybrew plan <formula>...      show where each piece would come from; change nothing

<formula> may also come from another tap (owner/tap/name). mybrew then supplies
its homebrew/core dependencies as bottles, building missing ones on GitHub,
and brew compiles only that formula itself on this Mac.
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
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from plan_build import load_registry, plan
from progress import LogReader, bar, estimate, last_timestamp, phase_of_log, position

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
    fresh: bool = False  # pulled since the last build finished

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


def active_runs(runs: list[dict]) -> dict[str, tuple[int, str]]:
    """formula -> (run id, created at) of its newest unfinished run, for attaching
    instead of duplicating; the creation time keeps elapsed times honest."""
    result: dict[str, tuple[int, str]] = {}
    for r in runs:
        if r["status"] != "completed" and r["displayTitle"].startswith("Build "):
            name = r["displayTitle"][len("Build "):]
            if name not in result or r["createdAt"] > result[name][1]:
                result[name] = (r["databaseId"], r["createdAt"])
    return result


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


def spinner_frames(encoding: str | None) -> str:
    return SPINNER if "utf" in (encoding or "").lower() else SPINNER_ASCII


ICONS = {DONE: "\033[32m✔\033[0m", FAILED: "\033[31m✘\033[0m", SKIPPED: "\033[33m–\033[0m",
         WAITING: "\033[2m⏸\033[0m"}


def elapsed(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def iso_epoch(stamp: str) -> float:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


PHASE_WIDTH = max(len(p) for p in ("queued", "dispatched", "configuring", "publishing"))
BAR_WIDTH = len("[██████████]  99%")


def render(the_plan: dict, deps: dict[str, set[str]], states: dict[str, str],
           started: dict[str, float], ended: dict[str, float], now: float,
           live: dict[str, dict] | None = None, spinner: str | None = None,
           history: dict[str, float] | None = None) -> list[tuple[str, str]]:
    """(state key, line) per formula; the key changes only when the state or phase does.

    `live[name]` holds what the scheduler knows of a running build: its phase
    (from the job log), when its build job started, and how old the log is.
    `history[name]` is the previous build's duration, which drives the bar and
    the time left. `spinner` is the animation frame (None draws a static icon).
    """
    live, history = live or {}, history or {}
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
            phase = state
        elif state == SKIPPED:
            text, phase = "skipped: a dependency failed", state
        elif state in (DONE, FAILED):
            text = f"{'built' if state == DONE else 'failed'} in {elapsed(ended[name] - started[name])}"
            phase = state
        else:
            info_live = live.get(name, {})
            phase = info_live.get("phase", "preparing") if state == "building" else state
            place = position(phase, verify=name == the_plan["target"])
            column = f"{phase:<{PHASE_WIDTH}} {f'[{place[0]}/{place[1]}]' if place else '':<7}"
            meter, timing = " " * BAR_WIDTH, f"{elapsed(now - started[name])} elapsed"
            build_started = info_live.get("build_started")
            if state == "building" and build_started is not None:
                fraction, left = estimate(now - build_started, history.get(name))
                if fraction is not None:
                    meter = f"[{bar(fraction)}] {int(fraction * 100):>3}%"
                    timing += f" · ~{elapsed(left)} left" if left else " · almost done"
            log_at = info_live.get("log_at")
            if state == "building" and log_at and now - log_at > 300:
                timing += f" · log {elapsed(now - log_at)} old"
            text = f"{column} {meter}  {timing}"
        icon = ICONS.get(state, f"\033[34m{spinner or '⟳'}\033[0m")
        key = f"{name}:{state}:{phase}:{text if state == WAITING else ''}"
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


ANSI_CODE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def truncate(line: str, width: int) -> str:
    """Cut a line to `width` visible characters, keeping its color codes intact."""
    out, visible, i = [], 0, 0
    while i < len(line):
        code = ANSI_CODE.match(line, i)
        if code:
            out.append(code.group(0))
            i = code.end()
            continue
        if visible >= width:
            out.append("…" if width else "")
            break
        out.append(line[i])
        visible += 1
        i += 1
    return "".join(out) + ("\x1b[0m" if "\x1b[" in line else "")


def fit(rows: list[tuple[str, str]], columns: int, height: int) -> list[str]:
    """The block to redraw in place: bottle rows folded into one summary line,
    lines cut to the terminal width and the block to its height, so a redraw
    always lands exactly on the previous frame (wrapped or scrolled lines are
    what leave copies in the scrollback)."""
    static = [line for key, line in rows if key.endswith(":static")]
    lines = [line for key, line in rows if not key.endswith(":static")]
    if static:
        official = sum("official bottle" in line for line in static)
        parts = [f"{official} official"] if official else []
        if len(static) - official:
            parts.append(f"{len(static) - official} mybrew")
        lines.append(f"  {ICONS[DONE]} {len(static)} from bottles: {', '.join(parts)}")
    room = max(height - 1, 1)
    if len(lines) > room:
        hidden = len(lines) - (room - 1)
        lines = lines[:room - 1] + [f"  … and {hidden} more"]
    return [truncate(line, max(columns - 2, 1)) for line in lines]  # + ellipsis = width - 1


class Display:
    """Redraws the status block in place on a terminal; prints changes otherwise."""

    def __init__(self, stream=sys.stdout, env: dict | None = None):
        env = os.environ if env is None else env
        self.stream, self.tty, self.drawn, self.printed = stream, stream.isatty(), 0, set()
        self.animate = self.tty and not env.get("NO_COLOR") and env.get("TERM") != "dumb"
        self.frames = spinner_frames(getattr(stream, "encoding", None))

    def show(self, rows: list[tuple[str, str]]) -> None:
        if self.tty:
            size = shutil.get_terminal_size((100, 40))
            lines = fit(rows, size.columns, size.lines)
            if self.drawn:
                self.stream.write(f"\033[{self.drawn}F")
            else:
                self.stream.write("\033[?7l")  # no wrapping while we own the block
            lines += [""] * (self.drawn - len(lines))  # a shrinking block clears its old tail
            for line in lines:
                self.stream.write(f"\033[2K{line}\n")
            self.drawn = len(lines)
        else:
            for key, line in rows:
                if key not in self.printed:
                    self.printed.add(key)
                    self.stream.write(line + "\n")
        self.stream.flush()

    def close(self) -> None:
        if self.tty and self.drawn:
            self.stream.write("\033[?7h")
            self.stream.flush()


class GitHub:
    """The three gh calls the scheduler needs; tests substitute a fake."""

    def __init__(self, repo: str):
        self.repo = repo
        self.logs = LogReader(repo, run("gh", "auth", "token").strip())

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

    def __init__(self, the_plan: dict, github, parallel: int, clock=time.time,
                 history: dict[str, float] | None = None):
        self.plan, self.github, self.parallel, self.clock = the_plan, github, parallel, clock
        self.deps = graph(the_plan)
        self.states = {n: WAITING for n in self.deps}
        self.runs: dict[str, int | None] = {}
        self.since: dict[str, datetime] = {}
        self.started: dict[str, float] = {}
        self.ended: dict[str, float] = {}
        self.live: dict[str, dict] = {}
        self.history = history or {}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=max(parallel, 1))

    def attach(self) -> None:
        """Pick up runs already in flight, e.g. after Ctrl-C and a rerun."""
        for name, (run_id, created) in active_runs(self.github.runs()).items():
            if name in self.states:
                self.states[name], self.runs[name], self.started[name] = "dispatched", run_id, iso_epoch(created)

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
                build = next((j for j in view.get("jobs", []) if j["name"] == "build"), None)
                if build and build.get("startedAt") and not build["startedAt"].startswith("0001"):
                    entry = self.live.setdefault(name, {})
                    entry["build_started"] = iso_epoch(build["startedAt"])
                    entry["job"] = build.get("databaseId")
        reading = [n for n, s in self.snapshot()[0].items() if s == "building" and self.live.get(n, {}).get("job")]
        list(self.pool.map(self.read_log, reading))

    def read_log(self, name: str) -> None:
        """Refresh a build's phase from its log, downloading it only when it has grown."""
        logs = getattr(self.github, "logs", None)
        if logs is None:
            return
        with self.lock:
            entry = dict(self.live.get(name, {}))
        try:
            size = logs.size(entry["job"])
            if not size or size == entry.get("log_size"):
                return
            text = logs.text(entry["job"])
        except (OSError, ValueError):
            return  # progress is best effort; the next poll tries again
        stamp = last_timestamp(text)
        with self.lock:
            self.live[name].update(log_size=size, phase=phase_of_log(text),
                                   log_at=iso_epoch(stamp) if stamp else None)

    def finished(self) -> bool:
        with self.lock:
            return finished(self.states)

    def snapshot(self) -> tuple:
        with self.lock:
            return (dict(self.states), dict(self.started), dict(self.ended),
                    {n: dict(v) for n, v in self.live.items()})

    def failures(self) -> dict[str, int | None]:
        return {n: self.runs.get(n) for n, s in self.states.items() if s == FAILED}


def build_missing(repo: str, the_plan: dict, parallel: int, history: dict[str, float] | None = None) -> None:
    """Build every formula in the plan's order as its own run, dependencies first."""
    scheduler = Scheduler(the_plan, GitHub(repo), parallel, history=history)
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
            states, started, ended, live = scheduler.snapshot()
            spinner = display.frames[frame % len(display.frames)] if display.animate else None
            display.show(render(the_plan, scheduler.deps, states, started, ended, time.time(), live, spinner,
                                scheduler.history))
            frame += 1
            poller.join(FRAME_SECONDS if display.animate else 1.0)
        states, started, ended, live = scheduler.snapshot()
        display.show(render(the_plan, scheduler.deps, states, started, ended, time.time(), live, None,
                            scheduler.history))
    except KeyboardInterrupt:
        stop.set()
        raise MybrewError("interrupted; builds already started keep running on GitHub. "
                          "Run mybrew again to pick them up.") from None
    finally:
        display.close()
        scheduler.pool.shutdown(wait=False)

    if error:
        raise error[0] if isinstance(error[0], MybrewError) else MybrewError(f"polling GitHub failed: {error[0]}")
    failed = scheduler.failures()
    if failed:
        links = "\n  ".join(f"{n}: https://github.com/{repo}/actions/runs/{r}" for n, r in failed.items())
        raise MybrewError(f"build failed:\n  {links}")


# --- commands --------------------------------------------------------------

def core_name(formula: str) -> str | None:
    """`foo` or `homebrew/core/foo` -> "foo"; owner/tap/foo from another tap -> None."""
    parts = formula.split("/")
    if len(parts) == 1:
        return formula
    if len(parts) == 3 and parts[:2] == ["homebrew", "core"]:
        return parts[2]
    if len(parts) == 3 and all(parts):
        return None
    raise MybrewError(f"not a formula name: {formula}")


def merge_plans(plans: list[dict]) -> dict:
    """One schedule for several targets' plans; each plan is deps-first, so is the union."""
    formulae: dict[str, dict] = {}
    for p in plans:
        for name, info in p["formulae"].items():
            formulae.setdefault(name, info)
    return {"target": None, "order": [n for n, f in formulae.items() if f["source"] == "build"],
            "formulae": formulae}


def unique(steps: list[list[str]]) -> list[list[str]]:
    seen, result = set(), []
    for step in steps:
        if tuple(step) not in seen:
            seen.add(tuple(step))
            result.append(step)
    return result


def build_history(tap: Tap) -> dict[str, float]:
    """Previous build durations by formula, for progress estimates."""
    return {n: e["build_seconds"] for n, e in load_registry(tap.registry_dir).items() if e.get("build_seconds")}


def load_plan(tap: Tap, formula: str) -> dict:
    if not tap.fresh:
        run("git", "-C", str(tap.path), "pull", "--ff-only", "--quiet")
        tap.fresh = True
    try:
        return plan(formula, load_registry(tap.registry_dir))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise MybrewError(f"no homebrew/core formula named {error.url.rsplit('/', 1)[-1][:-5]}") from None
        raise MybrewError(f"Homebrew API error {error.code} for {formula}") from None
    except urllib.error.URLError as error:
        raise MybrewError(f"cannot reach the Homebrew API: {error.reason}") from None


def cmd_plan(tap: Tap, formulae: list[str]) -> None:
    for formula in formulae:
        if core_name(formula) is None:
            cmd_third_party(tap, Path("/nonexistent"), formula, False, 0, False, install=False)
            continue
        formula = core_name(formula)
        say(f"{formula}")
        for line in describe(load_plan(tap, formula)):
            print(f"    {line}")


def cmd_install(tap: Tap, cellar: Path, formulae: list[str], allow_build: bool, parallel: int,
                replace: bool = False) -> None:
    for formula in formulae:
        if core_name(formula) is None:
            cmd_third_party(tap, cellar, formula, allow_build, parallel, replace, install=True)
            continue
        formula = core_name(formula)
        say(f"Checking {formula}")
        the_plan = load_plan(tap, formula)

        if the_plan["order"]:
            if not allow_build:
                raise MybrewError(f"no bottle for {' '.join(the_plan['order'])} (--no-build given)")
            require_gh()
            say(f"Building {len(the_plan['order'])} bottle(s) on github.com/{tap.repo}")
            build_missing(tap.repo, the_plan, parallel, build_history(tap))
            tap.fresh = False
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


def third_party_deps(formula: str) -> tuple[list[str], list[str]]:
    """Direct dependencies (runtime and build) of another tap's formula, as (core, other)."""
    try:
        names = run("brew", "deps", "--include-build", "--direct", "--full-name", formula).split()
    except MybrewError as error:
        raise MybrewError(f"cannot read {formula}'s dependencies (is its tap tapped and trusted?): {error}") from None
    core = [n for n in names if core_name(n)]
    return [core_name(n) for n in core], [n for n in names if not core_name(n)]


def cmd_third_party(tap: Tap, cellar: Path, formula: str, allow_build: bool, parallel: int,
                    replace: bool, install: bool) -> None:
    say(f"Checking {formula} (from another tap; brew compiles it here)")
    core, other = third_party_deps(formula)
    plans = [load_plan(tap, dep) for dep in core]
    merged = merge_plans(plans)
    if other:
        print(f"    left to brew (other taps): {' '.join(other)}")
    if merged["order"]:
        if not install:
            for line in describe(merged):
                print(f"    {line}")
            return
        if not allow_build:
            raise MybrewError(f"no bottle for {' '.join(merged['order'])} (--no-build given)")
        require_gh()
        say(f"Building {len(merged['order'])} bottle(s) for {formula}'s dependencies on github.com/{tap.repo}")
        build_missing(tap.repo, merged, parallel, build_history(tap))
        tap.fresh = False
        plans = [load_plan(tap, dep) for dep in core]
    elif not install:
        for line in describe(merged):
            print(f"    {line}")
        return

    installed = lambda name: installed_kegs(cellar, name)  # noqa: E731
    steps = unique([step for p in plans for step in install_steps(p, tap.name, installed, replace)])
    for step in steps + [["brew", "install", formula]]:
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
        for n in names:
            core_name(n)  # rejects malformed names early
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
