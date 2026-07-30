"""Applying workspace->PR labels to API payloads, and the setting that gates it."""
import os
import shutil
import tempfile
import unittest

from token_dashboard import server
from token_dashboard.db import init_db, save_workspace_prs, get_setting, set_setting


class LabelApplicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "w.db")
        init_db(self.db)
        save_workspace_prs(self.db, [{
            "path": "/wt/dubai-v3", "repo": "widgets", "repo_slug": "acme/widgets",
            "branch": "f/x", "is_main": 0, "pr_number": 42, "pr_title": "Add the thing",
            "pr_url": "https://x/42", "pr_state": "OPEN",
            "label": "widgets: PR #42 - Add the thing", "resolved": 1, "checked_at": 1.0,
        }])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rows(self):
        return [
            {"project_name": "dubai-v3", "workspace_path": "/wt/dubai-v3"},
            {"project_name": "other", "workspace_path": "/wt/unknown"},
        ]

    def test_disabled_by_default(self):
        self.assertFalse(server.workspace_pr_links_enabled(self.db))
        rows = server._apply_workspace_labels(self.db, self._rows())
        self.assertEqual(rows[0]["project_name"], "dubai-v3")

    def test_enabled_swaps_the_name_and_keeps_the_original(self):
        set_setting(self.db, server.WORKSPACE_PR_SETTING, "1")
        rows = server._apply_workspace_labels(self.db, self._rows())
        self.assertEqual(rows[0]["project_name"], "widgets: PR #42 - Add the thing")
        self.assertEqual(rows[0]["project_name_original"], "dubai-v3")
        self.assertEqual(rows[0]["pr_number"], 42)

    def test_unlinked_workspace_keeps_its_directory_name(self):
        """A deleted worktree can't resolve — it must not go blank or 'unknown'."""
        set_setting(self.db, server.WORKSPACE_PR_SETTING, "1")
        rows = server._apply_workspace_labels(self.db, self._rows())
        self.assertEqual(rows[1]["project_name"], "other")
        self.assertNotIn("pr_number", rows[1])

    def test_alternate_name_and_path_keys(self):
        set_setting(self.db, server.WORKSPACE_PR_SETTING, "1")
        rows = [{"source": "dubai-v3", "source_path": "/wt/dubai-v3"}]
        server._apply_workspace_labels(self.db, rows, ("source",), "source_path")
        self.assertEqual(rows[0]["source"], "widgets: PR #42 - Add the thing")

    def test_handles_a_bare_dict_and_junk_entries(self):
        set_setting(self.db, server.WORKSPACE_PR_SETTING, "1")
        row = {"project_name": "dubai-v3", "workspace_path": "/wt/dubai-v3"}
        server._apply_workspace_labels(self.db, row)
        self.assertTrue(row["project_name"].startswith("widgets: PR #42"))
        server._apply_workspace_labels(self.db, ["not a dict", None])  # must not raise


class SankeyLabelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "w.db")
        init_db(self.db)
        set_setting(self.db, server.WORKSPACE_PR_SETTING, "1")
        save_workspace_prs(self.db, [{
            "path": "/wt/a", "repo": "widgets", "repo_slug": "acme/widgets", "branch": "b",
            "is_main": 0, "pr_number": 7, "pr_title": "Fix", "pr_url": "u", "pr_state": "OPEN",
            "label": "widgets: PR #7 - Fix", "resolved": 1, "checked_at": 1.0,
        }])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_relabels_nodes_and_rewrites_link_endpoints(self):
        """Links reference nodes by name — miss one side and ECharts drops the link."""
        matrix = {
            "nodes": [
                {"name": "a (agent)", "workspace_path": "/wt/a"},
                {"name": "a (files)", "workspace_path": "/wt/a"},
                {"name": "z (files)", "workspace_path": "/wt/z"},
            ],
            "links": [
                {"source": "a (agent)", "target": "a (files)", "value": 3},
                {"source": "a (agent)", "target": "z (files)", "value": 1},
            ],
        }
        out = server._apply_sankey_labels(self.db, matrix)
        names = [n["name"] for n in out["nodes"]]
        self.assertEqual(names[0], "widgets: PR #7 - Fix (agent)")
        self.assertEqual(names[1], "widgets: PR #7 - Fix (files)")
        self.assertEqual(names[2], "z (files)")  # unlinked node untouched
        self.assertEqual(out["links"][0]["source"], "widgets: PR #7 - Fix (agent)")
        self.assertEqual(out["links"][0]["target"], "widgets: PR #7 - Fix (files)")
        self.assertEqual(out["links"][1]["target"], "z (files)")
        node_names = set(names)
        for link in out["links"]:
            self.assertIn(link["source"], node_names)
            self.assertIn(link["target"], node_names)


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "w.db")
        init_db(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_refresh_stores_rows_and_counts(self):
        def resolver(path, git_bin=None, gh_bin=None, allow_network=True):
            live = path == "/live"
            return {
                "path": path, "repo": "widgets" if live else None, "repo_slug": None,
                "branch": "b", "is_main": 0, "pr_number": 5 if live else None,
                "pr_title": "t", "pr_url": "u", "pr_state": "OPEN",
                "label": "widgets: PR #5 - t" if live else None,
                "resolved": 1 if live else 0, "checked_at": 1.0,
            }

        out = server._refresh_workspace_prs(self.db, paths=["/live", "/gone"], resolver=resolver)
        self.assertEqual(out["checked"], 2)
        self.assertEqual(out["resolved"], 1)
        self.assertEqual(out["with_pr"], 1)
        set_setting(self.db, server.WORKSPACE_PR_SETTING, "1")
        rows = [{"project_name": "x", "workspace_path": "/live"}]
        server._apply_workspace_labels(self.db, rows)
        self.assertEqual(rows[0]["project_name"], "widgets: PR #5 - t")

    def test_one_exploding_workspace_does_not_abort_the_rest(self):
        def resolver(path, git_bin=None, gh_bin=None, allow_network=True):
            if path == "/boom":
                raise RuntimeError("git blew up")
            return {"path": path, "label": "ok", "resolved": 1, "pr_number": 1,
                    "repo": "r", "repo_slug": None, "branch": "b", "is_main": 0,
                    "pr_title": "t", "pr_url": "u", "pr_state": "OPEN", "checked_at": 1.0}

        out = server._refresh_workspace_prs(self.db, paths=["/boom", "/fine"], resolver=resolver)
        self.assertEqual(out["resolved"], 1)

    def test_dead_paths_are_never_skipped_by_the_network_budget(self):
        """Only PR lookups are budgeted — a deleted worktree costs nothing."""
        paths = [f"/w{i}" for i in range(server.MAX_WORKSPACE_PR_LOOKUPS + 50)]
        seen = []

        def resolver(path, git_bin=None, gh_bin=None, allow_network=True):
            seen.append(path)
            return {"path": path, "label": None, "resolved": 0, "pr_number": None,
                    "repo": None, "repo_slug": None, "branch": None, "is_main": 0,
                    "pr_title": None, "pr_url": None, "pr_state": None, "checked_at": 1.0}

        out = server._refresh_workspace_prs(self.db, paths=paths, resolver=resolver)
        self.assertEqual(len(seen), len(paths), "every path inspected")
        self.assertEqual(out["skipped"], 0)
        self.assertEqual(out["throttled"], 0)

    def test_network_budget_throttles_live_worktrees_and_reports_it(self):
        n = server.MAX_WORKSPACE_PR_LOOKUPS + 7
        allowed = []

        def resolver(path, git_bin=None, gh_bin=None, allow_network=True):
            allowed.append(allow_network)
            return {"path": path, "label": "r: b", "resolved": 1,
                    "pr_number": 1 if allow_network else None,
                    "repo": "r", "repo_slug": "o/r", "branch": "b", "is_main": 0,
                    "pr_title": "t", "pr_url": "u", "pr_state": "OPEN", "checked_at": 1.0}

        out = server._refresh_workspace_prs(
            self.db, paths=[f"/w{i}" for i in range(n)], resolver=resolver)
        self.assertEqual(sum(1 for a in allowed if a), server.MAX_WORKSPACE_PR_LOOKUPS)
        self.assertEqual(out["pr_lookups"], server.MAX_WORKSPACE_PR_LOOKUPS)
        self.assertEqual(out["throttled"], 7)
        # Throttled workspaces still get a local label, just no PR number.
        self.assertEqual(out["resolved"], n)


class SettingsPayloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "w.db")
        init_db(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_payload_exposes_status_and_defaults_off(self):
        payload = server._settings_payload(self.db)
        self.assertIn("workspace_prs", payload)
        self.assertFalse(payload["workspace_prs"]["enabled"])
        self.assertEqual(payload["workspace_prs"]["linked"], 0)

    def test_setting_roundtrip(self):
        set_setting(self.db, server.WORKSPACE_PR_SETTING, "1")
        self.assertTrue(server.workspace_pr_links_enabled(self.db))
        set_setting(self.db, server.WORKSPACE_PR_SETTING, "0")
        self.assertFalse(server.workspace_pr_links_enabled(self.db))


if __name__ == "__main__":
    unittest.main()
