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

    def test_refresh_stores_rows_and_labels_apply(self):
        def resolver(paths, recorded_branches=None, **kw):
            rows = [{
                "path": "/live", "repo": "widgets", "repo_slug": "acme/widgets",
                "branch": "b", "is_main": 0, "pr_number": 5, "pr_title": "t",
                "pr_url": "u", "pr_state": "MERGED", "label": "widgets: PR #5 - t",
                "resolved": 1, "inferred": 0, "checked_at": 1.0,
            }]
            return rows, {"checked": len(paths), "resolved": 1, "with_pr": 1,
                          "live": 1, "inferred": 0, "repos": 1, "bulk_prs": 3,
                          "pr_lookups": 0, "throttled": 0}

        out = server._refresh_workspace_prs(self.db, paths=["/live", "/gone"], resolver=resolver)
        self.assertEqual(out["checked"], 2)
        self.assertEqual(out["with_pr"], 1)
        set_setting(self.db, server.WORKSPACE_PR_SETTING, "1")
        rows = [{"project_name": "x", "workspace_path": "/live"}]
        server._apply_workspace_labels(self.db, rows)
        self.assertEqual(rows[0]["project_name"], "widgets: PR #5 - t")

    def test_resolver_blowing_up_is_reported_not_raised(self):
        def resolver(paths, **kw):
            raise RuntimeError("gh exploded")

        out = server._refresh_workspace_prs(self.db, paths=["/a"], resolver=resolver)
        self.assertIn("error", out)
        self.assertEqual(out["with_pr"], 0)

    def test_recorded_branches_are_passed_through(self):
        """The transcript-derived branches are what make dead worktrees resolvable."""
        from token_dashboard.db import connect
        with connect(self.db) as c:
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, cwd, git_branch, type, timestamp) "
                "VALUES (?,?,?,?,?,?,?)",
                ("m1", "s1", "-home-x-proj", "/home/x/proj", "feature/x", "user",
                 "2026-05-01T00:00:00Z"),
            )
            c.commit()
        seen = {}

        def resolver(paths, recorded_branches=None, **kw):
            seen.update(recorded_branches or {})
            return [], {"checked": 0, "resolved": 0, "with_pr": 0}

        server._refresh_workspace_prs(self.db, paths=["/home/x/proj"], resolver=resolver)
        self.assertEqual(seen.get("/home/x/proj"), "feature/x")


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


class TopUpTests(unittest.TestCase):
    """A normal refresh fills in PR links without pressing 'Refresh PR links'."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "w.db")
        init_db(self.db)
        set_setting(self.db, server.WORKSPACE_PR_SETTING, "1")
        from token_dashboard.db import connect
        with connect(self.db) as c:
            for i, (slug, cwd, branch) in enumerate([
                ("-ws-repo-alpha", "/ws/repo/alpha", "feature/a"),
                ("-ws-repo-beta", "/ws/repo/beta", "feature/b"),
            ]):
                c.execute(
                    "INSERT INTO messages (uuid, session_id, project_slug, cwd, git_branch, "
                    "type, timestamp) VALUES (?,?,?,?,?,?,?)",
                    (f"m{i}", f"s{i}", slug, cwd, branch, "user", "2026-05-01T00:00:00Z"),
                )
            c.commit()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _resolver(self, seen):
        def resolver(paths, recorded_branches=None, **kw):
            seen.extend(paths)
            rows = [{
                "path": p, "repo": "widgets", "repo_slug": "acme/widgets", "branch": "b",
                "is_main": 0, "pr_number": 1, "pr_title": "t", "pr_url": "u",
                "pr_state": "OPEN", "label": "widgets: PR #1 - t", "resolved": 1,
                "inferred": 0, "checked_at": 1000.0,
            } for p in paths]
            return rows, {"checked": len(paths), "resolved": len(rows), "with_pr": len(rows)}
        return resolver

    def test_never_checked_workspaces_are_resolved(self):
        seen = []
        out = server._top_up_workspace_prs(self.db, resolver=self._resolver(seen))
        self.assertEqual(sorted(seen), ["/ws/repo/alpha", "/ws/repo/beta"])
        self.assertEqual(out["with_pr"], 2)

    def test_steady_state_costs_nothing(self):
        seen = []
        server._top_up_workspace_prs(self.db, resolver=self._resolver(seen))
        seen.clear()
        out = server._top_up_workspace_prs(self.db, resolver=self._resolver(seen))
        self.assertEqual(seen, [], "already linked — no further lookups")
        self.assertEqual(out["checked"], 0)

    def test_disabled_setting_is_a_no_op(self):
        set_setting(self.db, server.WORKSPACE_PR_SETTING, "0")
        seen = []
        out = server._top_up_workspace_prs(self.db, resolver=self._resolver(seen))
        self.assertEqual(seen, [])
        self.assertEqual(out["skipped_reason"], "disabled")

    def test_pr_less_workspace_is_rechecked_after_the_ttl(self):
        """You often open the PR after starting work in the worktree."""
        save_workspace_prs(self.db, [{
            "path": "/ws/repo/alpha", "repo": "widgets", "repo_slug": "acme/widgets",
            "branch": "feature/a", "is_main": 0, "pr_number": None, "pr_title": None,
            "pr_url": None, "pr_state": None, "label": "widgets: feature/a",
            "resolved": 1, "inferred": 0, "checked_at": 1000.0,
        }])
        seen = []
        server._top_up_workspace_prs(self.db, resolver=self._resolver(seen), now=1001.0)
        self.assertNotIn("/ws/repo/alpha", seen, "inside the recheck window")

        seen.clear()
        later = 1000.0 + server.WORKSPACE_PR_RECHECK_SECONDS + 1
        server._top_up_workspace_prs(self.db, resolver=self._resolver(seen), now=later)
        self.assertIn("/ws/repo/alpha", seen, "past the recheck window")

    def test_unresolvable_workspaces_are_not_retried_forever(self):
        save_workspace_prs(self.db, [{
            "path": "/ws/repo/alpha", "repo": None, "repo_slug": None, "branch": None,
            "is_main": 0, "pr_number": None, "pr_title": None, "pr_url": None,
            "pr_state": None, "label": None, "resolved": 0, "inferred": 0,
            "checked_at": 1.0,
        }])
        seen = []
        server._top_up_workspace_prs(self.db, resolver=self._resolver(seen), now=1e9)
        self.assertNotIn("/ws/repo/alpha", seen)

    def test_main_worktrees_are_not_rechecked(self):
        save_workspace_prs(self.db, [{
            "path": "/ws/repo/alpha", "repo": "widgets", "repo_slug": "acme/widgets",
            "branch": "main", "is_main": 1, "pr_number": None, "pr_title": None,
            "pr_url": None, "pr_state": None, "label": "widgets: main worktree",
            "resolved": 1, "inferred": 0, "checked_at": 1.0,
        }])
        seen = []
        server._top_up_workspace_prs(self.db, resolver=self._resolver(seen), now=1e9)
        self.assertNotIn("/ws/repo/alpha", seen)

    def test_batch_is_capped_and_remainder_reported(self):
        from token_dashboard.db import connect
        with connect(self.db) as c:
            for i in range(server.MAX_INCREMENTAL_PR_LOOKUPS + 4):
                c.execute(
                    "INSERT INTO messages (uuid, session_id, project_slug, cwd, git_branch, "
                    "type, timestamp) VALUES (?,?,?,?,?,?,?)",
                    (f"x{i}", "s", f"-ws-repo-w{i}", f"/ws/repo/w{i}", "b", "user",
                     "2026-05-01T00:00:00Z"),
                )
            c.commit()
        seen = []
        out = server._top_up_workspace_prs(self.db, resolver=self._resolver(seen))
        self.assertEqual(len(seen), server.MAX_INCREMENTAL_PR_LOOKUPS)
        self.assertEqual(out["pending"], 6)  # 2 original + 29 new - 25 done
