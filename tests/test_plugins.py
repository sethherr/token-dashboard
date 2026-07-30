import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from token_dashboard import plugins as plugins_mod
from token_dashboard.plugins import scan_plugins


def _write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")


class PluginsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.installed = self.tmp / "installed_plugins.json"
        self.settings = self.tmp / "settings.json"

    def _scan(self):
        with mock.patch.object(plugins_mod, "INSTALLED_JSON", self.installed), \
             mock.patch.object(plugins_mod, "SETTINGS_JSON", self.settings):
            return scan_plugins()

    def test_no_manifest_returns_empty(self):
        self.assertEqual(self._scan(), [])

    def test_reads_name_source_version(self):
        _write_json(self.installed, {
            "plugins": {
                "foo@bar-marketplace": [
                    {"installPath": str(self.tmp / "foo"), "version": "1.2.3", "scope": "user"},
                ],
            },
        })
        rows = self._scan()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "foo")
        self.assertEqual(rows[0]["source"], "bar-marketplace")
        self.assertEqual(rows[0]["version"], "1.2.3")
        self.assertTrue(rows[0]["enabled"])  # no enabledPlugins entry -> defaults enabled

    def test_disabled_plugin_flagged(self):
        _write_json(self.installed, {
            "plugins": {"foo@bar": [{"installPath": str(self.tmp / "foo"), "version": "1"}]},
        })
        _write_json(self.settings, {"enabledPlugins": {"foo@bar": False}})
        rows = self._scan()
        self.assertFalse(rows[0]["enabled"])

    def test_component_counts(self):
        install = self.tmp / "foo"
        (install / "skills" / "a").mkdir(parents=True)
        (install / "skills" / "a" / "SKILL.md").write_text("x" * 40, encoding="utf-8")
        (install / "agents").mkdir(parents=True)
        (install / "agents" / "one.md").write_text("a", encoding="utf-8")
        (install / "commands").mkdir(parents=True)
        (install / "commands" / "cmd.md").write_text("c", encoding="utf-8")
        _write_json(self.installed, {
            "plugins": {"foo@bar": [{"installPath": str(install), "version": "1"}]},
        })
        comp = self._scan()[0]["components"]
        self.assertEqual(comp["skills"], 1)
        self.assertEqual(comp["agents"], 1)
        self.assertEqual(comp["commands"], 1)
        self.assertEqual(comp["tokens"], 10)

    def test_description_from_plugin_json(self):
        install = self.tmp / "foo"
        (install / ".claude-plugin").mkdir(parents=True)
        _write_json(install / ".claude-plugin" / "plugin.json", {"description": "does foo things"})
        _write_json(self.installed, {
            "plugins": {"foo@bar": [{"installPath": str(install), "version": "1"}]},
        })
        rows = self._scan()
        self.assertEqual(rows[0]["description"], "does foo things")

    def test_sorted_by_name(self):
        _write_json(self.installed, {
            "plugins": {
                "zeta@m": [{"installPath": str(self.tmp / "z"), "version": "1"}],
                "alpha@m": [{"installPath": str(self.tmp / "a"), "version": "1"}],
            },
        })
        rows = self._scan()
        self.assertEqual([r["name"] for r in rows], ["alpha", "zeta"])


if __name__ == "__main__":
    unittest.main()
