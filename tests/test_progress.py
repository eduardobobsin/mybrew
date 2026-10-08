import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from progress import estimate, last_timestamp, phase_of_log, position  # noqa: E402

E = "\x1b"
LOG = "\n".join([
    "2026-10-08T12:44:00.1Z ##[group]Run eduardobobsin/mybrew/actions/plan@v0.7.7",
    "2026-10-08T12:45:00.1Z ##[group]openssl@3 (mybrew bottle)",
    f"2026-10-08T12:45:01.1Z {E}[34m==>{E}[0m {E}[1mDownloading https://example.com/openssl.tar.gz{E}[0m",
    "2026-10-08T12:46:00.1Z ##[group]gcc (build)",
    f"2026-10-08T12:46:01.1Z {E}[34m==>{E}[0m {E}[1mDownloading https://ftp.gnu.org/gcc-16.2.0.tar.xz{E}[0m",
    f"2026-10-08T12:47:00.1Z {E}[34m==>{E}[0m {E}[1mPatching{E}[0m",
    f"2026-10-08T12:47:30.1Z {E}[34m==>{E}[0m {E}[1m../configure --prefix=/usr/local/opt/gcc{E}[0m",
    f"2026-10-08T12:52:00.1Z {E}[34m==>{E}[0m {E}[1mgmake BOOT_LDFLAGS=-Wl{E}[0m",
    "2026-10-08T13:49:16.1Z libtool: compile: xgcc -c foo.c",
]) + "\n"


def upto(marker: str) -> str:
    return LOG[:LOG.index(marker)]


class PhaseTest(unittest.TestCase):
    def test_follows_brew_commands_after_the_build_group(self):
        self.assertEqual(phase_of_log(LOG), "compiling")
        self.assertEqual(phase_of_log(upto("gmake")), "configuring")
        self.assertEqual(phase_of_log(upto("../configure")), "patching")
        self.assertEqual(phase_of_log(upto("Patching")), "fetching")

    def test_dependency_downloads_count_as_preparing(self):
        self.assertEqual(phase_of_log(upto("##[group]gcc")), "preparing")

    def test_install_test_and_bottle(self):
        more = LOG + "2026-10-08T14:00:00Z ==> gmake install\n"
        self.assertEqual(phase_of_log(more), "installing")
        more += "2026-10-08T14:05:00Z ==> Testing me/mybrew/gcc\n"
        self.assertEqual(phase_of_log(more), "testing")
        more += "2026-10-08T14:06:00Z ==> Determining me/mybrew/gcc bottle rebuild...\n"
        self.assertEqual(phase_of_log(more), "bottling")

    def test_cmake_and_meson_projects(self):
        cmake = "##[group]lz4 (build)\n==> cmake -S . -B build\n"
        self.assertEqual(phase_of_log(cmake), "configuring")
        self.assertEqual(phase_of_log(cmake + "==> cmake --build build\n"), "compiling")
        self.assertEqual(phase_of_log(cmake + "==> cmake --build build\n==> cmake --install build\n"), "installing")
        self.assertEqual(phase_of_log("##[group]p (build)\n==> meson setup build\n"), "configuring")

    def test_last_timestamp(self):
        self.assertEqual(last_timestamp(LOG), "2026-10-08T13:49:16.1Z")


class EstimateTest(unittest.TestCase):
    def test_fraction_and_time_left(self):
        self.assertEqual(estimate(600, 1000), (0.6, 400))

    def test_without_history(self):
        self.assertEqual(estimate(600, None), (None, None))

    def test_overrun_is_capped(self):
        self.assertEqual(estimate(1500, 1000), (0.99, 0))

    def test_positions(self):
        self.assertEqual(position("preparing", False), (1, 9))
        self.assertEqual(position("publishing", False), (9, 9))
        self.assertEqual(position("verifying", True), (10, 10))
        self.assertIsNone(position("queued", False))


if __name__ == "__main__":
    unittest.main()
