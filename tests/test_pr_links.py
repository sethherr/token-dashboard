"""Workspace -> GitHub PR association.

No real git/gh calls: a fake runner returns canned command output, so these
tests cover the logic (label precedence, parsing, guard rails) rather than the
local machine's checkout.
"""
import os
import shutil
import tempfile
import unittest

from token_dashboard import pr_links as P
from token_dashboard.db import (
    init_db, connect, save_workspace_prs, workspace_pr_map,
    workspace_root_paths, clear_workspace_prs,
)


def fake_runner(responses):
    """Runner keyed by a distinctive fragment of the command line."""
    def run(args, timeout):
        line = " ".join(args)
        for needle, out in responses.items():
            if needle in line:
                return out
        return None
    return run


MAIN = {
    "rev-parse --absolute-git-dir": "/repo/.git",
    "--git-common-dir": "/repo/.git",
    "rev-parse --abbrev-ref HEAD": "main",
    "remote get-url origin": "git@github.com:acme/widgets.git",
}
LINKED = {
    "rev-parse --absolute-git-dir": "/repo/.git/worktrees/dubai-v3",
    "--git-common-dir": "/repo/.git",
    "rev-parse --abbrev-ref HEAD": "feature/add-thing",
    "remote get-url origin": "https://github.com/acme/widgets.git",
}


class RepoSlugTests(unittest.TestCase):
    def test_parses_ssh_https_and_bare_forms(self):
        cases = {
            "git@github.com:acme/widgets.git": "acme/widgets",
            "https://github.com/acme/widgets.git": "acme/widgets",
            "https://github.com/acme/widgets": "acme/widgets",
            "ssh://git@github.com/acme/widgets.git": "acme/widgets",
            "git@github.com:acme/widgets": "acme/widgets",
            "https://github.com/acme/widgets/": "acme/widgets",
        }
        for url, want in cases.items():
            self.assertEqual(P.repo_slug(url), want, url)

    def test_returns_none_for_junk(self):
        for url in (None, "", "not a url", "localfile"):
            self.assertIsNone(P.repo_slug(url))

    def test_repo_display_is_the_bare_name(self):
        self.assertEqual(P.repo_display("acme/widgets"), "widgets")
        self.assertIsNone(P.repo_display(None))


class InspectWorkspaceTests(unittest.TestCase):
    def test_main_worktree_detected(self):
        info = P.inspect_workspace("/repo", git_bin="git", runner=fake_runner(MAIN))
        self.assertTrue(info["is_main"])
        self.assertEqual(info["repo"], "widgets")
        self.assertEqual(info["branch"], "main")

    def test_linked_worktree_detected(self):
        info = P.inspect_workspace("/wt", git_bin="git", runner=fake_runner(LINKED))
        self.assertFalse(info["is_main"])
        self.assertEqual(info["branch"], "feature/add-thing")

    def test_non_repo_returns_none(self):
        self.assertIsNone(P.inspect_workspace("/tmp/x", git_bin="git", runner=fake_runner({})))

    def test_detached_head_has_no_branch(self):
        r = dict(LINKED, **{"rev-parse --abbrev-ref HEAD": "HEAD"})
        self.assertIsNone(P.inspect_workspace("/wt", git_bin="git", runner=fake_runner(r))["branch"])

    def test_missing_git_binary_returns_none(self):
        self.assertIsNone(P.inspect_workspace("/wt", git_bin=None, runner=fake_runner(LINKED)))


class FindPrTests(unittest.TestCase):
    PR_JSON = '[{"number": 42, "title": "Add the thing", "state": "OPEN", "url": "https://x/42"}]'

    def test_parses_pr(self):
        pr = P.find_pr("acme/widgets", "feature/x", gh_bin="gh",
                       runner=fake_runner({"pr list": self.PR_JSON}))
        self.assertEqual(pr["number"], 42)
        self.assertEqual(pr["title"], "Add the thing")

    def test_blank_branch_never_calls_gh(self):
        """`gh pr list --head ''` drops the filter and returns an unrelated PR."""
        calls = []

        def spy(args, timeout):
            calls.append(args)
            return self.PR_JSON

        self.assertIsNone(P.find_pr("acme/widgets", "", gh_bin="gh", runner=spy))
        self.assertIsNone(P.find_pr("acme/widgets", None, gh_bin="gh", runner=spy))
        self.assertIsNone(P.find_pr(None, "branch", gh_bin="gh", runner=spy))
        self.assertEqual(calls, [], "gh must not run without both repo and branch")

    def test_empty_list_and_junk_are_none(self):
        for out in ("[]", "not json", '{"number": 1}', ""):
            self.assertIsNone(P.find_pr("a/b", "br", gh_bin="gh",
                                        runner=fake_runner({"pr list": out})))

    def test_missing_gh_binary_returns_none(self):
        self.assertIsNone(P.find_pr("a/b", "br", gh_bin=None,
                                    runner=fake_runner({"pr list": self.PR_JSON})))


class LabelTests(unittest.TestCase):
    def test_main_worktree_label(self):
        info = {"repo": "widgets", "is_main": True, "branch": "main"}
        self.assertEqual(P.build_label(info, None), "widgets: main worktree")

    def test_pr_label(self):
        info = {"repo": "widgets", "is_main": False, "branch": "f/x"}
        pr = {"number": 42, "title": "Add the thing"}
        self.assertEqual(P.build_label(info, pr), "widgets: PR #42 - Add the thing")

    def test_pr_without_title_omits_the_dash(self):
        info = {"repo": "widgets", "is_main": False, "branch": "f/x"}
        self.assertEqual(P.build_label(info, {"number": 7, "title": ""}), "widgets: PR #7")

    def test_worktree_without_pr_falls_back_to_branch(self):
        info = {"repo": "widgets", "is_main": False, "branch": "f/x"}
        self.assertEqual(P.build_label(info, None), "widgets: f/x")

    def test_no_repo_means_no_label(self):
        self.assertIsNone(P.build_label({"repo": None, "is_main": False}, None))
        self.assertIsNone(P.build_label(None, None))


class ResolveWorkspaceTests(unittest.TestCase):
    def test_main_worktree_skips_the_network_call(self):
        calls = []

        def spy(args, timeout):
            calls.append(" ".join(args))
            for needle, out in MAIN.items():
                if needle in " ".join(args):
                    return out
            return None

        row = P.resolve_workspace("/repo", git_bin="git", gh_bin="gh", runner=spy)
        self.assertEqual(row["label"], "widgets: main worktree")
        self.assertTrue(row["is_main"])
        self.assertFalse(any("pr list" in c for c in calls), "no gh call for main worktree")

    def test_linked_worktree_resolves_to_a_pr(self):
        responses = dict(LINKED)
        responses["pr list"] = '[{"number": 9, "title": "Fix it", "state": "OPEN", "url": "u"}]'
        row = P.resolve_workspace("/wt", git_bin="git", gh_bin="gh", runner=fake_runner(responses))
        self.assertEqual(row["label"], "widgets: PR #9 - Fix it")
        self.assertEqual(row["pr_number"], 9)

    def test_vanished_workspace_yields_no_label(self):
        row = P.resolve_workspace("/gone", git_bin="git", gh_bin="gh", runner=fake_runner({}))
        self.assertIsNone(row["label"])
        self.assertEqual(row["resolved"], 0)
        self.assertEqual(row["path"], "/gone")


class WorkspacePrStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "w.db")
        init_db(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _row(self, path, label, pr=None):
        return {"path": path, "repo": "widgets", "repo_slug": "acme/widgets",
                "branch": "b", "is_main": 0, "pr_number": pr, "pr_title": "t",
                "pr_url": "u", "pr_state": "OPEN", "label": label,
                "resolved": 1, "checked_at": 1.0}

    def test_roundtrip_and_label_filter(self):
        save_workspace_prs(self.db, [
            self._row("/a", "widgets: PR #1 - x", 1),
            self._row("/b", None),          # unresolved: excluded from the map
            self._row("/c", ""),            # empty label: also excluded
        ])
        m = workspace_pr_map(self.db)
        self.assertEqual(set(m), {"/a"})
        self.assertEqual(m["/a"]["pr_number"], 1)

    def test_upsert_replaces_rather_than_duplicating(self):
        save_workspace_prs(self.db, [self._row("/a", "old", 1)])
        save_workspace_prs(self.db, [self._row("/a", "new", 2)])
        m = workspace_pr_map(self.db)
        self.assertEqual(len(m), 1)
        self.assertEqual(m["/a"]["label"], "new")

    def test_empty_and_pathless_rows_are_ignored(self):
        self.assertEqual(save_workspace_prs(self.db, []), 0)
        self.assertEqual(save_workspace_prs(self.db, [{"label": "x"}]), 0)

    def test_clear(self):
        save_workspace_prs(self.db, [self._row("/a", "x", 1)])
        clear_workspace_prs(self.db)
        self.assertEqual(workspace_pr_map(self.db), {})

    def test_workspace_root_paths_are_roots_busiest_first(self):
        slug_a = "-home-x-projA"
        slug_b = "-home-x-projB"
        with connect(self.db) as c:
            for i in range(3):
                c.execute(
                    "INSERT INTO messages (uuid, session_id, project_slug, cwd, type, timestamp) "
                    "VALUES (?,?,?,?,?,?)",
                    (f"a{i}", "s1", slug_a, "/home/x/projA/sub", "user", "2026-05-01T00:00:00Z"),
                )
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, cwd, type, timestamp) "
                "VALUES (?,?,?,?,?,?)",
                ("b0", "s2", slug_b, "/home/x/projB", "user", "2026-05-01T00:00:00Z"),
            )
            c.commit()
        paths = workspace_root_paths(self.db)
        # Deeper cwds collapse to the workspace root, busiest first.
        self.assertEqual(paths, ["/home/x/projA", "/home/x/projB"])


if __name__ == "__main__":
    unittest.main()
