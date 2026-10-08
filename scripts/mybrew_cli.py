"""mybrew: install Homebrew formulae on Intel Macs, building missing bottles on demand.

  mybrew install <formula>...   install, building bottles on GitHub if none exist
  mybrew plan <formula>...      show where each piece would come from; change nothing
  mybrew <anything else>        passed through to brew

Options for install:
  --no-build    fail instead of starting a build on a cache miss

Environment:
  MYBREW_TAP    tap to use (default: the only tapped */homebrew-mybrew)
  HOMEBREW_NO_AUTO_UPDATE defaults to 1 for mybrew's own brew calls, since
                mybrew refreshes its tap itself; set it to 0 to keep auto-update.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from plan_build import plan

WORKFLOW = "build.yml"
POLL_SECONDS = 15


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
    def registry_path(self) -> Path:
        return self.path / "registry" / "bottles.json"


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

def install_steps(the_plan: dict, tap: str, installed) -> list[list[str]]:
    """brew commands that install the plan's target without compiling.

    `installed(name)` returns {"version", "tap"} or None. mybrew formulae are
    drop-in replacements for core ones, so an already-installed keg of the
    right version satisfies a dependency whichever tap it came from.
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
            else:
                raise MybrewError(
                    f"{name} {current['version']} is installed from {current['tap']}, but {target} needs "
                    f"{info['version']} from {tap}. Replace it with `brew uninstall --ignore-dependencies "
                    f"{name}` and run mybrew again.")

    info = formulae[target]
    current = installed(target)
    if info["source"] == "official":
        steps.append(["brew", "install", target])
    elif current and current["tap"] == tap and current["version"] == info["version"]:
        pass
    elif current and current["tap"] != tap:
        raise MybrewError(f"{target} is installed from {current['tap']}; "
                          f"`brew uninstall {target}` first to switch it to {tap}")
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


# --- GitHub ----------------------------------------------------------------

def require_gh() -> None:
    try:
        run("gh", "auth", "status")
    except (MybrewError, FileNotFoundError) as error:
        raise MybrewError("building needs the GitHub CLI, logged in: `gh auth login`") from error


def trigger_build(repo: str, formula: str) -> int:
    since = datetime.now(timezone.utc) - timedelta(seconds=30)
    run("gh", "workflow", "run", WORKFLOW, "--repo", repo, "-f", f"formula={formula}")
    for _ in range(20):
        time.sleep(3)
        runs = json.loads(run("gh", "run", "list", "--repo", repo, "--workflow", WORKFLOW,
                              "--event", "workflow_dispatch", "--limit", "20",
                              "--json", "databaseId,displayTitle,createdAt"))
        run_id = find_run(runs, formula, since)
        if run_id:
            return run_id
    raise MybrewError(f"started a build of {formula} but could not find its run on {repo}")


def wait_for_run(repo: str, run_id: int) -> None:
    url = f"https://github.com/{repo}/actions/runs/{run_id}"
    say(f"Building on GitHub: {url}")
    seen: dict[str, str] = {}
    while True:
        view = json.loads(run("gh", "run", "view", str(run_id), "--repo", repo, "--json", "status,conclusion,jobs"))
        for job in view["jobs"]:
            state = job["conclusion"] or job["status"]
            if seen.get(job["name"]) != state:
                seen[job["name"]] = state
                print(f"    {job['name']}: {state}", flush=True)
        if view["status"] == "completed":
            if view["conclusion"] != "success":
                raise MybrewError(f"build {view['conclusion']}: {url}")
            return
        time.sleep(POLL_SECONDS)


# --- commands --------------------------------------------------------------

def load_plan(tap: Tap, formula: str) -> dict:
    run("git", "-C", str(tap.path), "pull", "--ff-only", "--quiet")
    registry = json.loads(tap.registry_path.read_text()) if tap.registry_path.exists() else {}
    return plan(formula, registry)


def cmd_plan(tap: Tap, formulae: list[str]) -> None:
    for formula in formulae:
        say(f"{formula}")
        for line in describe(load_plan(tap, formula)):
            print(f"    {line}")


def cmd_install(tap: Tap, cellar: Path, formulae: list[str], allow_build: bool) -> None:
    for formula in formulae:
        say(f"Checking {formula}")
        the_plan = load_plan(tap, formula)
        for line in describe(the_plan):
            print(f"    {line}")

        if the_plan["order"]:
            if not allow_build:
                raise MybrewError(f"no bottle for {' '.join(the_plan['order'])} (--no-build given)")
            require_gh()
            say(f"Requesting bottles for {' '.join(the_plan['order'])}")
            wait_for_run(tap.repo, trigger_build(tap.repo, formula))
            the_plan = load_plan(tap, formula)

        steps = install_steps(the_plan, tap.name, lambda name: installed_kegs(cellar, name))
        if not steps:
            say(f"{formula} is already installed")
        for step in steps:
            say(" ".join(step))
            run(*step, capture=False)


def brew_env(env: dict) -> dict:
    """The tap was just refreshed; skip brew's own auto-update unless asked for."""
    return {"HOMEBREW_NO_AUTO_UPDATE": "1", **env}


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    command, rest = argv[0], argv[1:]
    if command not in ("install", "plan"):
        os.execvp("brew", ["brew", *argv])

    names = [a for a in rest if not a.startswith("-")]
    unknown = [a for a in rest if a.startswith("-") and a != "--no-build"]
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
            cmd_install(tap, cellar, names, allow_build="--no-build" not in rest)
    except MybrewError as error:
        print(f"\033[1;31mError:\033[0m {error}", file=sys.stderr)
        return 1
    return 0
