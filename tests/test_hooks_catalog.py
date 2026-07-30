import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from token_dashboard import hooks_catalog as hooks_mod
from token_dashboard import plugins as plugins_mod
from token_dashboard.hooks_catalog import scan_commands, scan_agents


def _write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")


class HooksCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.settings = self.tmp / "settings.json"
        self.commands_dir = self.tmp / "commands"
        # cached_plugins() keeps a 60s TTL cache at module level, shared across
        # the whole test process -- reset it so an earlier test's fixture
        # can't leak into this one.
        plugins_mod._cache["at"] = 0.0
        plugins_mod._cache["data"] = []

    def _hooks(self):
        with mock.patch.object(hooks_mod, "SETTINGS_JSON", self.settings):
            return hooks_mod.scan_hooks()

    def test_script_regex_preserves_windows_drive_letter(self):
        # Regression: the drive letter must stay attached to the path, or
        # Path(path).is_file() checks the wrong drive (whichever one happens
        # to be the current working directory's) instead of the real one.
        m = hooks_mod._SCRIPT_RE.search(
            'pwsh -File "C:/Users/marcu/.claude/hooks/peon-ping/peon.ps1"'
        )
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "C:/Users/marcu/.claude/hooks/peon-ping/peon.ps1")

    def test_script_regex_still_matches_posix_path_without_drive_letter(self):
        m = hooks_mod._SCRIPT_RE.search("bash /usr/local/bin/hook.sh --foo")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "/usr/local/bin/hook.sh")

    def test_no_settings_returns_empty(self):
        self.assertEqual(self._hooks(), [])

    def test_hook_with_existing_script_is_ok(self):
        script = self.tmp / "myhook.sh"
        script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        # Real hook commands (checked against this machine's settings.json)
        # always use forward-slash paths, even on Windows -- str(Path) would
        # render backslashes here and the regex is intentionally /-anchored.
        _write_json(self.settings, {
            "hooks": {"PreToolUse": [{"hooks": [{"command": script.as_posix()}]}]},
        })
        rows = self._hooks()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "myhook.sh")
        self.assertEqual(rows[0]["event"], "PreToolUse")
        self.assertEqual(rows[0]["status"], "ok")

    def test_hook_with_missing_script_is_broken(self):
        missing = self.tmp / "gone.py"
        _write_json(self.settings, {
            "hooks": {"PreToolUse": [{"hooks": [{"command": missing.as_posix()}]}]},
        })
        rows = self._hooks()
        self.assertEqual(rows[0]["status"], "broken — script missing")

    def test_inline_command_has_no_path(self):
        _write_json(self.settings, {
            "hooks": {"Stop": [{"hooks": [{"command": "echo done"}]}]},
        })
        rows = self._hooks()
        self.assertIsNone(rows[0]["file_path"])
        self.assertEqual(rows[0]["name"], "Stop (inline)")
        self.assertEqual(rows[0]["status"], "ok")

    def test_duplicate_script_across_events_deduped(self):
        script = self.tmp / "shared.sh"
        script.write_text("x", encoding="utf-8")
        _write_json(self.settings, {
            "hooks": {
                "PreToolUse": [{"hooks": [{"command": script.as_posix()}]}],
                "PostToolUse": [{"hooks": [{"command": script.as_posix()}]}],
            },
        })
        rows = self._hooks()
        self.assertEqual(len(rows), 1)

    def test_commands_from_user_dir(self):
        self.commands_dir.mkdir(parents=True)
        (self.commands_dir / "deploy.md").write_text("do the deploy", encoding="utf-8")
        with mock.patch.object(hooks_mod, "COMMANDS_DIR", self.commands_dir), \
             mock.patch.object(plugins_mod, "INSTALLED_JSON", self.tmp / "no-plugins.json"), \
             mock.patch.object(plugins_mod, "SETTINGS_JSON", self.tmp / "no-settings.json"):
            rows = scan_commands()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "deploy")
        self.assertEqual(rows[0]["source"], "user")

    def test_commands_and_agents_from_plugins(self):
        install = self.tmp / "plugin-a"
        (install / "commands").mkdir(parents=True)
        (install / "commands" / "review.md").write_text("x", encoding="utf-8")
        (install / "agents").mkdir(parents=True)
        (install / "agents" / "helper.md").write_text("x", encoding="utf-8")
        installed_json = self.tmp / "installed_plugins.json"
        _write_json(installed_json, {
            "plugins": {"plugin-a@marketplace": [{"installPath": str(install), "version": "1"}]},
        })
        with mock.patch.object(hooks_mod, "COMMANDS_DIR", self.tmp / "no-user-commands"), \
             mock.patch.object(plugins_mod, "INSTALLED_JSON", installed_json), \
             mock.patch.object(plugins_mod, "SETTINGS_JSON", self.tmp / "no-settings.json"):
            commands = scan_commands()
            agents = scan_agents()
        self.assertEqual([c["name"] for c in commands], ["review"])
        self.assertEqual(commands[0]["source"], "plugin-a")
        self.assertEqual([a["name"] for a in agents], ["helper"])


if __name__ == "__main__":
    unittest.main()
