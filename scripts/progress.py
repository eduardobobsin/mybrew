"""Progress of a build run: its phase, and a time estimate.

GitHub's job-log API serves a snapshot of a running job's log that is
refreshed only in large chunks (it can lag 15-20 minutes), and the blob store
ignores Range requests. So the bar and time left come from the previous
build's duration, which moves smoothly, and the log names the phase: it is
downloaded again only when its size changes (a cheap HEAD request), and
reported with its age when stale.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request

# Every line starts with an ISO timestamp; brew output carries ANSI colors.
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ?")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Ordered: the first match wins, so `make install` is installing, not compiling.
COMMAND_PHASES = [
    (re.compile(r"^(Patching|Applying )"), "patching"),
    (re.compile(r"^(\.\.?/)?(configure|autogen\.sh|bootstrap)\b|^(autoreconf|meson setup|cmake -S|cmake \.\.|cmake -G)"),
     "configuring"),
    (re.compile(r"^(g?make|ninja|meson|cmake --install).*\binstall\b|^cmake --install"), "installing"),
    (re.compile(r"^(g?make|ninja|meson compile|cmake --build|cargo|go build|scons|xcodebuild|swift build)\b"),
     "compiling"),
    (re.compile(r"^Testing "), "testing"),
    (re.compile(r"^(Determining .* bottle rebuild|Bottling )"), "bottling"),
]

# The pipeline of one run; the target formula's run adds verifying.
PHASES = ["preparing", "fetching", "patching", "configuring", "compiling", "installing", "testing",
          "bottling", "publishing", "verifying"]


def position(phase: str, verify: bool) -> tuple[int, int] | None:
    """1-based place of a phase in its run's pipeline, and the pipeline length."""
    if phase not in PHASES:
        return None
    return PHASES.index(phase) + 1, len(PHASES) if verify else len(PHASES) - 1


def clean(line: str) -> str:
    return ANSI.sub("", TIMESTAMP.sub("", line)).rstrip("\r\n")


def phase_of_log(text: str) -> str:
    """The latest phase named in a build job's log."""
    phase, building = "preparing", False
    for raw in text.splitlines():
        line = clean(raw)
        if line.startswith("##[group]") and line.endswith("(mybrew bottle)"):
            phase, building = "preparing", False
        elif line.startswith("##[group]") and line.endswith("(build)"):
            phase, building = "fetching", True
        elif building and line.startswith("==> "):
            command = line[4:]
            for pattern, name in COMMAND_PHASES:
                if pattern.search(command):
                    phase = name
                    break
    return phase


def estimate(elapsed: float, previous_seconds: float | None) -> tuple[float | None, float | None]:
    """(fraction done, seconds left) of a build against the previous build's duration.

    The fraction stops just short of 1: a newer version can take longer than
    the last one did, and the bar should then read "almost done", not overflow.
    """
    if not previous_seconds or elapsed < 0:
        return None, None
    return min(elapsed / previous_seconds, 0.99), max(previous_seconds - elapsed, 0)


def last_timestamp(text: str) -> str | None:
    """ISO timestamp of the log's last line: how fresh the snapshot is."""
    for raw in reversed(text.splitlines()):
        match = TIMESTAMP.match(raw)
        if match:
            return match.group(0).strip()
    return None


def bar(fraction: float, width: int = 10) -> str:
    filled = int(round(fraction * width))
    return "█" * filled + "░" * (width - filled)


def megabytes(n: int) -> str:
    return f"{n / 1_000_000:.1f} MB"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class LogReader:
    """Reads job logs through the REST API, which redirects to a signed blob URL."""

    def __init__(self, repo: str, token: str):
        self.repo, self.token = repo, token
        self.opener = urllib.request.build_opener(_NoRedirect)

    def _blob_url(self, job_id: int) -> str | None:
        request = urllib.request.Request(
            f"https://api.github.com/repos/{self.repo}/actions/jobs/{job_id}/logs",
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json"})
        try:
            self.opener.open(request, timeout=20)
        except urllib.error.HTTPError as error:
            if error.code in (301, 302, 303, 307, 308):
                return error.headers.get("Location")
        return None

    def size(self, job_id: int) -> int | None:
        url = self._blob_url(job_id)
        if not url:
            return None
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=20) as response:
            length = response.headers.get("Content-Length")
        return int(length) if length else None

    def text(self, job_id: int) -> str:
        url = self._blob_url(job_id)
        if not url:
            return ""
        with urllib.request.urlopen(url, timeout=60) as response:
            return response.read().decode("utf-8", "replace")
