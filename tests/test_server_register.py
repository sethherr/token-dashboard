"""Integration tests for the Register group endpoints: plugins, mcp, hooks, commands, agents."""
import http.server
import json
import socket
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from token_dashboard.db import init_db
from token_dashboard.server import build_handler, _cache_clear
from token_dashboard import plugins as plugins_mod
from token_dashboard import mcp_catalog as mcp_mod
from token_dashboard import hooks_catalog as hooks_mod


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")


class ServerRegisterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = str(self.tmp / "t.db")
        init_db(self.db)
        self.port = _free_port()
        H = build_handler(self.db)
        self.httpd = http.server.HTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        # server.py's response cache is a module-level dict keyed only by
        # path+query -- clear it so an earlier test's empty result for the
        # same endpoint can't leak into this one.
        _cache_clear()

        # Isolate every catalog module from the real machine's ~/.claude state
        # -- otherwise these tests depend on whatever happens to be installed.
        self.installed = self.tmp / "installed_plugins.json"
        self.plugin_settings = self.tmp / "plugins-settings.json"
        self.claude_json = self.tmp / ".claude.json"
        self.hook_settings = self.tmp / "hooks-settings.json"
        self.commands_dir = self.tmp / "commands"
        plugins_mod._cache["at"] = 0.0
        plugins_mod._cache["data"] = []
        self._patches = [
            mock.patch.object(plugins_mod, "INSTALLED_JSON", self.installed),
            mock.patch.object(plugins_mod, "SETTINGS_JSON", self.plugin_settings),
            mock.patch.object(mcp_mod, "CLAUDE_JSON", self.claude_json),
            mock.patch.object(hooks_mod, "SETTINGS_JSON", self.hook_settings),
            mock.patch.object(hooks_mod, "COMMANDS_DIR", self.commands_dir),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        for p in self._patches:
            p.stop()

    def _get(self, path):
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}").read()

    def test_plugins_empty_by_default(self):
        self.assertEqual(json.loads(self._get("/api/plugins")), [])

    def test_plugins_lists_installed(self):
        _write_json(self.installed, {
            "plugins": {"foo@bar": [{"installPath": str(self.tmp / "foo"), "version": "1"}]},
        })
        body = json.loads(self._get("/api/plugins"))
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["name"], "foo")

    def test_mcp_empty_by_default(self):
        self.assertEqual(json.loads(self._get("/api/mcp")), [])

    def test_mcp_lists_local_and_plugin(self):
        _write_json(self.claude_json, {"mcpServers": {"jina": {"url": "https://mcp.jina.ai/sse"}}})
        plug = self.tmp / "plug"
        _write_json(plug / ".mcp.json", {"dataforseo": {"command": "npx dataforseo-mcp-server"}})
        _write_json(self.installed, {
            "plugins": {"seo@mk": [{"installPath": str(plug), "version": "1"}]},
        })
        body = json.loads(self._get("/api/mcp"))
        self.assertEqual(sorted(r["name"] for r in body), ["dataforseo", "jina"])
        seo = next(r for r in body if r["name"] == "dataforseo")
        self.assertEqual(seo["kind"], "plugin")
        self.assertEqual(seo["source"], "seo")
        jina = next(r for r in body if r["name"] == "jina")
        self.assertIn("usage_calls", jina)

    def test_hooks_empty_by_default(self):
        self.assertEqual(json.loads(self._get("/api/hooks")), [])

    def test_hooks_lists_configured_hook(self):
        script = self.tmp / "hook.sh"
        script.write_text("x", encoding="utf-8")
        _write_json(self.hook_settings, {"hooks": {"Stop": [{"hooks": [{"command": script.as_posix()}]}]}})
        body = json.loads(self._get("/api/hooks"))
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["status"], "ok")

    def test_commands_empty_by_default(self):
        self.assertEqual(json.loads(self._get("/api/commands")), [])

    def test_commands_lists_user_command(self):
        self.commands_dir.mkdir(parents=True)
        (self.commands_dir / "deploy.md").write_text("x", encoding="utf-8")
        body = json.loads(self._get("/api/commands"))
        self.assertEqual([c["name"] for c in body], ["deploy"])

    def test_agents_empty_by_default(self):
        self.assertEqual(json.loads(self._get("/api/agents")), [])


if __name__ == "__main__":
    unittest.main()
