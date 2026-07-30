"""Applying workspace->PR labels to API payloads, and the setting that gates it."""
import os
import shutil
import tempfile
import unittest

from token_dashboard import server
from token_dashboard.db import (init_db, save_workspace_prs, get_setting, set_setting,
                                workspace_pr_map)


class LabelApplicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "w.db")
        init_db(self.db)
        save_workspace_prs(self.db, [{
            "path": "/wt/dubai-v3", "repo": "widgets", "repo_slug": "acme/widgets",
            "branch": "f/x", "is_main": 0, "pr_number": 42, "pr_title": "Add the thing",
            "pr_url": "https://x/42", "pr_state": "OPEN",
            "label": "widgets: #42 - Add the thing", "resolved": 1, "checked_at": 1.0,
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
        self.assertEqual(rows[0]["project_name"], "widgets: #42 - Add the thing")
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
        self.assertEqual(rows[0]["source"], "widgets: #42 - Add the thing")

    def test_handles_a_bare_dict_and_junk_entries(self):
        set_setting(self.db, server.WORKSPACE_PR_SETTING, "1")
        row = {"project_name": "dubai-v3", "workspace_path": "/wt/dubai-v3"}
        server._apply_workspace_labels(self.db, row)
        self.assertTrue(row["project_name"].startswith("widgets: #42"))
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
            "label": "widgets: #7 - Fix", "resolved": 1, "checked_at": 1.0,
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
        self.assertEqual(names[0], "widgets: #7 - Fix (agent)")
        self.assertEqual(names[1], "widgets: #7 - Fix (files)")
        self.assertEqual(names[2], "z (files)")  # unlinked node untouched
        self.assertEqual(out["links"][0]["source"], "widgets: #7 - Fix (agent)")
        self.assertEqual(out["links"][0]["target"], "widgets: #7 - Fix (files)")
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
                "pr_url": "u", "pr_state": "MERGED", "label": "widgets: #5 - t",
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
        self.assertEqual(rows[0]["project_name"], "widgets: #5 - t")

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
                "pr_state": "OPEN", "label": "widgets: #1 - t", "resolved": 1,
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


class LeakRowFieldTests(unittest.TestCase):
    """A leak row names two workspaces; their PR data must not collide."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "w.db")
        init_db(self.db)
        set_setting(self.db, server.WORKSPACE_PR_SETTING, "1")
        save_workspace_prs(self.db, [
            {"path": "/a", "repo": "widgets", "repo_slug": "acme/widgets", "branch": "b",
             "is_main": 0, "pr_number": 1, "pr_title": "One", "pr_url": "u1",
             "pr_state": "OPEN", "label": "widgets: #1 - One", "resolved": 1,
             "inferred": 0, "checked_at": 1.0},
            {"path": "/b", "repo": "gadgets", "repo_slug": "acme/gadgets", "branch": "c",
             "is_main": 0, "pr_number": 2, "pr_title": "Two", "pr_url": "u2",
             "pr_state": "MERGED", "label": "gadgets: #2 - Two", "resolved": 1,
             "inferred": 0, "checked_at": 1.0},
        ])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_source_and_target_keep_separate_pr_fields(self):
        rows = [{"source": "a", "source_path": "/a", "target": "b", "target_path": "/b"}]
        server._apply_workspace_labels(self.db, rows, ("source",), "source_path", "source_")
        server._apply_workspace_labels(self.db, rows, ("target",), "target_path", "target_")
        r = rows[0]
        self.assertEqual(r["source"], "widgets: #1 - One")
        self.assertEqual(r["target"], "gadgets: #2 - Two")
        self.assertEqual(r["source_pr_number"], 1)
        self.assertEqual(r["target_pr_number"], 2)
        self.assertEqual(r["source_pr_url"], "u1")
        self.assertEqual(r["target_pr_url"], "u2")


class StoredLabelFormatTests(unittest.TestCase):
    """The display string is rebuilt from parts, not read back verbatim."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "w.db")
        init_db(self.db)
        set_setting(self.db, server.WORKSPACE_PR_SETTING, "1")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_label_written_in_an_older_format_is_re_rendered(self):
        save_workspace_prs(self.db, [{
            "path": "/a", "repo": "widgets", "repo_slug": "acme/widgets", "branch": "b",
            "is_main": 0, "pr_number": 42, "pr_title": "Add the thing", "pr_url": "u",
            "pr_state": "OPEN",
            "label": "widgets: PR #42 - Add the thing",  # the old format
            "resolved": 1, "inferred": 0, "checked_at": 1.0,
        }])
        rows = [{"project_name": "dubai-v3", "workspace_path": "/a"}]
        server._apply_workspace_labels(self.db, rows)
        self.assertEqual(rows[0]["project_name"], "widgets: #42 - Add the thing")

    def test_main_worktree_label_survives_the_rebuild(self):
        save_workspace_prs(self.db, [{
            "path": "/m", "repo": "widgets", "repo_slug": "acme/widgets", "branch": "main",
            "is_main": 1, "pr_number": None, "pr_title": None, "pr_url": None,
            "pr_state": None, "label": "widgets: main worktree", "resolved": 1,
            "inferred": 0, "checked_at": 1.0,
        }])
        rows = [{"project_name": "bikeindex", "workspace_path": "/m"}]
        server._apply_workspace_labels(self.db, rows)
        self.assertEqual(rows[0]["project_name"], "widgets: main worktree")


class RefreshProgressTests(unittest.TestCase):
    """The full refresh runs in the background and reports progress."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "w.db")
        init_db(self.db)
        set_setting(self.db, server.WORKSPACE_PR_SETTING, "1")
        server._set_workspace_pr_progress(running=False, phase=None, done=0, total=0)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_resolve_all_reports_each_phase(self):
        from token_dashboard import pr_links as P
        seen = []
        P.resolve_all(["/a", "/b"], recorded_branches={},
                      git_bin="git", gh_bin="gh",
                      runner=lambda args, timeout: None,
                      on_progress=seen.append)
        phases = [p["phase"] for p in seen]
        self.assertIn("inspect", phases)
        self.assertIn("match", phases)
        last = [p for p in seen if p["phase"] == "match"][-1]
        self.assertEqual(last["done"], last["total"], "ends at 100%")

    def test_a_failing_progress_callback_never_aborts_the_refresh(self):
        from token_dashboard import pr_links as P

        def boom(_):
            raise RuntimeError("ui exploded")

        rows, stats = P.resolve_all(["/a"], recorded_branches={}, git_bin="git",
                                    gh_bin="gh", runner=lambda a, t: None,
                                    on_progress=boom)
        self.assertEqual(stats["checked"], 1)

    def test_async_refresh_publishes_events_and_finishes(self):
        import time as _time
        events = []
        unsub = server._EVENT_SUBS
        q = server._subscribe()

        def resolver(paths, recorded_branches=None, on_progress=None, **kw):
            if on_progress:
                on_progress({"phase": "inspect", "done": 1, "total": 1, "detail": "/a"})
            return [], {"checked": 1, "resolved": 0, "with_pr": 0}

        out = server._refresh_workspace_prs_async(self.db, resolver=resolver)
        self.assertTrue(out["started"])
        deadline = _time.time() + 5
        while _time.time() < deadline:
            if not server.workspace_pr_progress().get("running"):
                break
            _time.sleep(0.05)
        server._unsubscribe(q)
        while not q.empty():
            events.append(q.get_nowait())
        kinds = [e.get("phase") for e in events if e.get("type") == "workspace-prs"]
        self.assertIn("done", kinds)
        self.assertFalse(server.workspace_pr_progress()["running"])

    def test_second_refresh_is_refused_while_one_runs(self):
        server.WORKSPACE_PR_LOCK.acquire()
        try:
            out = server._refresh_workspace_prs_async(self.db)
            self.assertFalse(out["started"])
            self.assertEqual(out["reason"], "already-running")
        finally:
            server.WORKSPACE_PR_LOCK.release()

    def test_status_is_readable_for_a_client_that_reconnects(self):
        server._set_workspace_pr_progress(running=True, phase="repos", done=1, total=3)
        st = server.workspace_pr_progress()
        self.assertTrue(st["running"])
        self.assertEqual(st["phase"], "repos")
        server._set_workspace_pr_progress(running=False)


class EveryWorkspaceSurfaceTests(unittest.TestCase):
    """Each query that returns a workspace name must also return its path.

    The label swap keys on `workspace_path`; a query that omits it silently
    opts out of PR relabelling, which is invisible until someone notices a
    stale directory name on one page.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "w.db")
        init_db(self.db)
        from token_dashboard.db import connect
        with connect(self.db) as c:
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, cwd, type, "
                "timestamp, is_sidechain, model, input_tokens, output_tokens) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("m1", "s1", "-home-x-proj", "/home/x/proj", "assistant",
                 "2026-05-01T00:00:00Z", 0, "claude-opus-5", 10, 5),
            )
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, cwd, type, "
                "timestamp, is_sidechain, model, input_tokens, output_tokens) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("m2", "s1", "-home-x-proj", "/home/x/proj", "assistant",
                 "2026-05-01T00:01:00Z", 1, "claude-sonnet-5", 20, 5),
            )
            c.commit()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_queries_that_name_a_workspace_also_carry_its_path(self):
        from token_dashboard import db as D
        checks = [
            ("project_summary", D.project_summary(self.db), "project_name"),
            ("recent_sessions", D.recent_sessions(self.db, limit=5), "project_name"),
            ("top_subagent_sessions", D.top_subagent_sessions(self.db, limit=5), "project_name"),
            ("dispatch_tree", D.dispatch_tree(self.db, limit=5), "project_name"),
        ]
        for name, rows, key in checks:
            for r in rows:
                if r.get(key):
                    self.assertIn("workspace_path", r,
                                  f"{name} rows name a workspace but carry no path")

    def test_sdk_runs_carry_a_path_too(self):
        from token_dashboard import db as D
        for r in D.orchestration_breakdown(self.db).get("sdk_runs") or []:
            if r.get("workspace"):
                self.assertIn("workspace_path", r)


class SankeyMergeTests(unittest.TestCase):
    """Relabelling is many-to-one; ECharts Sankey throws on duplicate node names."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "w.db")
        init_db(self.db)
        set_setting(self.db, server.WORKSPACE_PR_SETTING, "1")
        # Two different directories, same PR — the real case: several
        # worktrees created for one pull request.
        save_workspace_prs(self.db, [
            {"path": "/wt/a", "repo": "widgets", "repo_slug": "acme/widgets", "branch": "b",
             "is_main": 0, "pr_number": 9, "pr_title": "Fix", "pr_url": "u",
             "pr_state": "MERGED", "label": "widgets: #9 - Fix", "resolved": 1,
             "inferred": 0, "checked_at": 1.0},
            {"path": "/wt/b", "repo": "widgets", "repo_slug": "acme/widgets", "branch": "b",
             "is_main": 0, "pr_number": 9, "pr_title": "Fix", "pr_url": "u",
             "pr_state": "MERGED", "label": "widgets: #9 - Fix", "resolved": 1,
             "inferred": 0, "checked_at": 1.0},
        ])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _matrix(self):
        return {
            "nodes": [
                {"name": "a (agent)", "workspace_path": "/wt/a"},
                {"name": "b (agent)", "workspace_path": "/wt/b"},
                {"name": "z (files)", "workspace_path": "/wt/z"},
            ],
            "links": [
                {"source": "a (agent)", "target": "z (files)", "value": 3},
                {"source": "b (agent)", "target": "z (files)", "value": 4},
            ],
        }

    def test_node_names_are_unique_after_relabelling(self):
        out = server._apply_sankey_labels(self.db, self._matrix())
        names = [n["name"] for n in out["nodes"]]
        self.assertEqual(len(names), len(set(names)), f"duplicate node names: {names}")

    def test_merged_nodes_keep_every_directory_they_stand_for(self):
        out = server._apply_sankey_labels(self.db, self._matrix())
        merged = [n for n in out["nodes"] if n["name"] == "widgets: #9 - Fix (agent)"][0]
        self.assertEqual(sorted(merged["workspace_paths"]), ["/wt/a", "/wt/b"])

    def test_links_into_a_merged_node_are_summed_not_duplicated(self):
        out = server._apply_sankey_labels(self.db, self._matrix())
        pairs = [(l["source"], l["target"]) for l in out["links"]]
        self.assertEqual(len(pairs), len(set(pairs)), "duplicate link pairs")
        self.assertEqual(out["links"][0]["value"], 7, "3 + 4 calls preserved")

    def test_every_link_endpoint_still_names_a_node(self):
        out = server._apply_sankey_labels(self.db, self._matrix())
        names = {n["name"] for n in out["nodes"]}
        for link in out["links"]:
            self.assertIn(link["source"], names)
            self.assertIn(link["target"], names)

    def test_no_self_loops_are_introduced(self):
        """Sankey needs a DAG; a node pointing at itself would crash it."""
        out = server._apply_sankey_labels(self.db, self._matrix())
        for link in out["links"]:
            self.assertNotEqual(link["source"], link["target"])


class TopUpMustNotDegradeTests(unittest.TestCase):
    """An incremental top-up sees only a few paths; it must not lose attribution.

    Regression: the scan-loop top-up resolved a subset of workspaces, so
    sibling inference had no live sibling to learn from, concluded the repo
    was unknown, and wrote an empty label over a good one — silently
    un-labelling workspaces on every scan.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "w.db")
        init_db(self.db)
        set_setting(self.db, server.WORKSPACE_PR_SETTING, "1")
        from token_dashboard.db import connect
        with connect(self.db) as c:
            for i, cwd in enumerate(["/ws/repo/alive", "/ws/repo/dead"]):
                c.execute(
                    "INSERT INTO messages (uuid, session_id, project_slug, cwd, git_branch, "
                    "type, timestamp) VALUES (?,?,?,?,?,?,?)",
                    (f"m{i}", f"s{i}", f"-ws-repo-{cwd.split('/')[-1]}", cwd,
                     f"feature/{i}", "user", "2026-05-01T00:00:00Z"),
                )
            c.commit()
        # A previous full pass resolved both; the dead one only via inference.
        save_workspace_prs(self.db, [
            {"path": "/ws/repo/alive", "repo": "widgets", "repo_slug": "acme/widgets",
             "branch": "feature/0", "is_main": 0, "pr_number": 1, "pr_title": "One",
             "pr_url": "u", "pr_state": "OPEN", "label": "widgets: #1 - One",
             "resolved": 1, "inferred": 0, "checked_at": 1.0},
            {"path": "/ws/repo/dead", "repo": "widgets", "repo_slug": "acme/widgets",
             "branch": "feature/1", "is_main": 0, "pr_number": None, "pr_title": None,
             "pr_url": None, "pr_state": None, "label": "widgets: feature/1",
             "resolved": 1, "inferred": 1, "checked_at": 1.0},
        ])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_existing_label_survives_a_failed_re_resolution(self):
        def resolver(paths, **kw):
            rows = [{"path": p, "repo": None, "repo_slug": None, "branch": None,
                     "is_main": 0, "pr_number": None, "pr_title": None, "pr_url": None,
                     "pr_state": None, "label": None, "resolved": 0, "inferred": 0,
                     "checked_at": 2.0} for p in paths]
            return rows, {"checked": len(paths), "resolved": 0, "with_pr": 0}

        out = server._top_up_workspace_prs(self.db, resolver=resolver, now=1e9)
        self.assertGreaterEqual(out["kept_existing"], 1)
        labels = workspace_pr_map(self.db)
        self.assertEqual(labels["/ws/repo/dead"]["label"], "widgets: feature/1")

    def test_repo_hints_are_passed_so_inference_still_works(self):
        seen = {}

        def resolver(paths, repo_hints=None, **kw):
            seen['hints'] = repo_hints or {}
            return [], {"checked": 0, "resolved": 0, "with_pr": 0}

        server._top_up_workspace_prs(self.db, resolver=resolver, now=1e9)
        # Both known workspaces live under /ws/repo and agree on the repo.
        self.assertEqual(seen['hints'].get('/ws/repo'), 'acme/widgets')

    def test_a_real_improvement_is_still_written(self):
        def resolver(paths, **kw):
            rows = [{"path": p, "repo": "widgets", "repo_slug": "acme/widgets",
                     "branch": "feature/1", "is_main": 0, "pr_number": 42,
                     "pr_title": "Now merged", "pr_url": "u", "pr_state": "MERGED",
                     "label": "widgets: #42 - Now merged", "resolved": 1,
                     "inferred": 1, "checked_at": 2.0} for p in paths]
            return rows, {"checked": len(paths), "resolved": len(paths), "with_pr": len(paths)}

        server._top_up_workspace_prs(self.db, resolver=resolver, now=1e9)
        self.assertEqual(workspace_pr_map(self.db)["/ws/repo/dead"]["pr_number"], 42)
