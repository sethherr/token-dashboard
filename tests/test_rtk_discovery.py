"""Locating the rtk binary.

RTK has no single canonical install location — Homebrew (/opt/homebrew/bin),
Cargo (~/.cargo/bin), and the install script (~/.local/bin) all differ — so
discovery walks an override, then PATH, then known fallback dirs.
"""
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from token_dashboard import server

# Only home-relative fallbacks, so a real rtk in /opt/homebrew/bin on the
# machine running these tests can't leak into the results.
HERMETIC_DIRS = (("~", ".local", "bin"), ("~", ".cargo", "bin"))


def _fake_rtk(directory: Path, name: str = "rtk") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / name
    p.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


class _SandboxedDirs(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.home.mkdir()
        patcher = mock.patch.object(server, "_RTK_FALLBACK_DIRS", HERMETIC_DIRS)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class FindRtkTests(_SandboxedDirs):

    def test_returns_none_when_absent_everywhere(self):
        self.assertIsNone(server._find_rtk(home=self.home, env={"PATH": ""}))

    def test_finds_rtk_on_path(self):
        binary = _fake_rtk(self.tmp / "brewish")
        found = server._find_rtk(home=self.home, env={"PATH": str(binary.parent)})
        self.assertEqual(found, str(binary))

    def test_finds_rtk_outside_dot_local_bin(self):
        """The regression this fixes: rtk installed somewhere other than ~/.local/bin."""
        binary = _fake_rtk(self.tmp / "opt" / "homebrew" / "bin")
        self.assertIsNone(server._find_rtk(home=self.home, env={"PATH": ""}))
        found = server._find_rtk(home=self.home, env={"PATH": str(binary.parent)})
        self.assertEqual(found, str(binary))

    def test_falls_back_to_home_local_bin_when_path_is_bare(self):
        binary = _fake_rtk(self.home / ".local" / "bin")
        found = server._find_rtk(home=self.home, env={"PATH": ""})
        self.assertEqual(found, str(binary))

    def test_falls_back_to_home_cargo_bin(self):
        binary = _fake_rtk(self.home / ".cargo" / "bin")
        found = server._find_rtk(home=self.home, env={"PATH": ""})
        self.assertEqual(found, str(binary))

    def test_env_override_wins_over_path(self):
        on_path = _fake_rtk(self.tmp / "onpath")
        override = _fake_rtk(self.tmp / "custom", name="rtk")
        env = {"PATH": str(on_path.parent), server.RTK_ENV_VAR: str(override)}
        self.assertEqual(server._find_rtk(home=self.home, env=env), str(override))

    def test_bad_override_reports_missing_rather_than_falling_back(self):
        on_path = _fake_rtk(self.tmp / "onpath")
        env = {
            "PATH": str(on_path.parent),
            server.RTK_ENV_VAR: str(self.tmp / "does-not-exist"),
        }
        self.assertIsNone(server._find_rtk(home=self.home, env=env))

    def test_non_executable_file_is_not_accepted(self):
        d = self.home / ".local" / "bin"
        d.mkdir(parents=True)
        (d / "rtk").write_text("not executable", encoding="utf-8")
        (d / "rtk").chmod(0o644)
        self.assertIsNone(server._find_rtk(home=self.home, env={"PATH": ""}))


class RtkPayloadTests(_SandboxedDirs):
    def _script(self, body: str) -> Path:
        d = self.home / ".local" / "bin"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "rtk"
        p.write_text(body, encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
        return p

    def test_available_false_when_missing(self):
        body = server._rtk_payload(home=self.home, env={"PATH": ""})
        self.assertFalse(body["available"])
        self.assertIsNone(body["summary"])

    def test_parses_json_from_a_discovered_binary(self):
        self._script('#!/bin/sh\necho \'{"summary": {"saved": 42}, "daily": []}\'\n')
        body = server._rtk_payload(home=self.home, env={"PATH": ""})
        self.assertTrue(body["available"])
        self.assertEqual(body["summary"]["saved"], 42)
        self.assertIn("install_url", body)

    def test_non_dict_json_degrades_to_installed_but_empty(self):
        """A different tool also ships as `rtk`; exit 0 with the wrong shape must not crash."""
        self._script("#!/bin/sh\necho '[1, 2, 3]'\n")
        body = server._rtk_payload(home=self.home, env={"PATH": ""})
        self.assertTrue(body["available"])
        self.assertIsNone(body["summary"])

    def test_nonzero_exit_degrades_to_installed_but_empty(self):
        self._script("#!/bin/sh\necho 'unknown subcommand' >&2\nexit 1\n")
        body = server._rtk_payload(home=self.home, env={"PATH": ""})
        self.assertTrue(body["available"])
        self.assertIsNone(body["summary"])

    def test_garbage_output_degrades_to_installed_but_empty(self):
        self._script("#!/bin/sh\necho 'not json at all'\n")
        body = server._rtk_payload(home=self.home, env={"PATH": ""})
        self.assertTrue(body["available"])
        self.assertIsNone(body["summary"])

    def test_resolved_binary_dir_is_prepended_to_path(self):
        self._script('#!/bin/sh\nprintf \'{"summary": {"path": "%s"}}\' "$PATH"\n')
        body = server._rtk_payload(home=self.home, env={"PATH": "/nowhere"})
        expected = str(self.home / ".local" / "bin")
        self.assertTrue(body["summary"]["path"].startswith(expected + os.pathsep))


if __name__ == "__main__":
    unittest.main()
