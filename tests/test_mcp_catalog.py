import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from token_dashboard import mcp_catalog as mcp_mod
from token_dashboard import plugins as plugins_mod
from token_dashboard.mcp_catalog import scan_mcp


def _write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")


class McpCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.claude_json = self.tmp / ".claude.json"

    def _scan(self, plugins=None):
        # _plugin_servers() calls plugins.cached_plugins() lazily; patch it so
        # these tests don't depend on the real machine's installed plugins.
        with mock.patch.object(mcp_mod, "CLAUDE_JSON", self.claude_json), \
             mock.patch.object(plugins_mod, "cached_plugins", lambda: plugins or []):
            return scan_mcp()

    def _plugin(self, name, servers, enabled=True):
        pdir = self.tmp / name
        _write_json(pdir / ".mcp.json", servers)
        return {"name": name, "install_path": pdir.as_posix(), "enabled": enabled}

    def test_no_files_returns_empty(self):
        self.assertEqual(self._scan(), [])

    def test_local_http_server(self):
        _write_json(self.claude_json, {"mcpServers": {"jina": {"url": "https://mcp.jina.ai/sse"}}})
        rows = self._scan()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "jina")
        self.assertEqual(rows[0]["kind"], "local")
        self.assertEqual(rows[0]["source"], "user")
        self.assertEqual(rows[0]["transport"], "http")
        self.assertEqual(rows[0]["command_or_url"], "https://mcp.jina.ai/sse")
        self.assertEqual(rows[0]["file_path"], self.claude_json.as_posix())

    def test_local_stdio_server(self):
        _write_json(self.claude_json, {"mcpServers": {"fs": {"command": "npx fs-mcp"}}})
        rows = self._scan()
        self.assertEqual(rows[0]["transport"], "stdio")

    def test_project_scoped_servers_merged(self):
        _write_json(self.claude_json, {
            "projects": {"/repo": {"mcpServers": {"local-tool": {"command": "x"}}}},
        })
        rows = self._scan()
        self.assertEqual([r["name"] for r in rows], ["local-tool"])

    def test_top_level_wins_over_project_dup(self):
        _write_json(self.claude_json, {
            "mcpServers": {"dup": {"command": "top-level"}},
            "projects": {"/repo": {"mcpServers": {"dup": {"command": "project-level"}}}},
        })
        rows = self._scan()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["command_or_url"], "top-level")

    def test_plugin_servers_scanned(self):
        plug = self._plugin("plugin-x", {"dataforseo": {"command": "npx dataforseo-mcp-server"}})
        rows = self._scan(plugins=[plug])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "dataforseo")
        self.assertEqual(rows[0]["kind"], "plugin")
        self.assertEqual(rows[0]["source"], "plugin-x")
        self.assertEqual(rows[0]["transport"], "stdio")
        self.assertEqual(rows[0]["file_path"], (self.tmp / "plugin-x" / ".mcp.json").as_posix())

    def test_plugin_mcpservers_wrapper_form(self):
        plug = self._plugin("plugin-w", {"mcpServers": {"srv": {"url": "https://x"}}})
        rows = self._scan(plugins=[plug])
        self.assertEqual([r["name"] for r in rows], ["srv"])
        self.assertEqual(rows[0]["transport"], "http")

    def test_disabled_plugin_skipped(self):
        plug = self._plugin("plugin-off", {"srv": {"url": "https://x"}}, enabled=False)
        self.assertEqual(self._scan(plugins=[plug]), [])

    def test_plugin_without_install_path_skipped(self):
        plug = {"name": "no-path", "install_path": None, "enabled": True}
        self.assertEqual(self._scan(plugins=[plug]), [])

    def test_local_wins_over_plugin_dup(self):
        _write_json(self.claude_json, {"mcpServers": {"clickup": {"command": "local"}}})
        plug = self._plugin("bw", {"clickup": {"url": "https://mcp.clickup.com/mcp"}})
        rows = self._scan(plugins=[plug])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "local")
        self.assertEqual(rows[0]["command_or_url"], "local")

    def test_combined_and_sorted(self):
        _write_json(self.claude_json, {"mcpServers": {"zeta": {"command": "z"}}})
        plug = self._plugin("bw", {"alpha": {"command": "a"}})
        rows = self._scan(plugins=[plug])
        self.assertEqual([r["name"] for r in rows], ["alpha", "zeta"])


if __name__ == "__main__":
    unittest.main()
