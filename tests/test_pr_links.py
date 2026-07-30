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
        self.assertEqual(P.build_label(info, pr), "widgets: #42 - Add the thing")

    def test_pr_without_title_omits_the_dash(self):
        info = {"repo": "widgets", "is_main": False, "branch": "f/x"}
        self.assertEqual(P.build_label(info, {"number": 7, "title": ""}), "widgets: #7")

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
        self.assertEqual(row["label"], "widgets: #9 - Fix it")
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


class BulkFetchTests(unittest.TestCase):
    PRS = ('[{"number": 10, "title": "Ten", "state": "MERGED", "url": "u10", "headRefName": "a"},'
           ' {"number": 11, "title": "Eleven", "state": "OPEN", "url": "u11", "headRefName": "b"},'
           ' {"number": 3,  "title": "Old",   "state": "CLOSED", "url": "u3", "headRefName": "a"}]')

    def test_indexes_by_head_branch(self):
        idx = P.fetch_repo_prs("a/b", gh_bin="gh", runner=fake_runner({"pr list": self.PRS}))
        self.assertEqual(set(idx), {"a", "b"})
        self.assertEqual(idx["b"]["number"], 11)

    def test_reused_branch_keeps_the_newest_pr(self):
        idx = P.fetch_repo_prs("a/b", gh_bin="gh", runner=fake_runner({"pr list": self.PRS}))
        self.assertEqual(idx["a"]["number"], 10, "highest PR number wins for a reused branch")

    def test_junk_and_missing_gh_degrade_to_empty(self):
        for out in ("not json", '{"a": 1}', ""):
            self.assertEqual(P.fetch_repo_prs("a/b", gh_bin="gh",
                                              runner=fake_runner({"pr list": out})), {})
        self.assertEqual(P.fetch_repo_prs("a/b", gh_bin=None, runner=fake_runner({})), {})
        self.assertEqual(P.fetch_repo_prs("", gh_bin="gh", runner=fake_runner({})), {})


class SiblingInferenceTests(unittest.TestCase):
    def test_unanimous_parent_yields_a_repo(self):
        live = {
            "/ws/bikeindex/alpha": "acme/bike_index",
            "/ws/bikeindex/beta": "acme/bike_index",
        }
        self.assertEqual(P.infer_repos_by_sibling(live), {"/ws/bikeindex": "acme/bike_index"})

    def test_a_parent_holding_several_repos_yields_nothing(self):
        """~/Sites holds unrelated projects — inferring from one would be a guess."""
        live = {"/Sites/projA": "me/a", "/Sites/projB": "me/b"}
        self.assertEqual(P.infer_repos_by_sibling(live), {})

    def test_unresolvable_siblings_are_ignored(self):
        live = {"/ws/x/alpha": "acme/r", "/ws/x/beta": None}
        self.assertEqual(P.infer_repos_by_sibling(live), {"/ws/x": "acme/r"})


class ResolveAllTests(unittest.TestCase):
    """The deleted-worktree path: branch from transcripts, repo from siblings."""

    LIVE = "/ws/repo/alive"
    DEAD = "/ws/repo/deleted"

    def _runner(self, extra=None):
        responses = {
            f"-C {self.LIVE} rev-parse --absolute-git-dir": "/main/.git/worktrees/alive",
            f"-C {self.LIVE} rev-parse --path-format=absolute --git-common-dir": "/main/.git",
            f"-C {self.LIVE} rev-parse --abbrev-ref HEAD": "feature/live",
            f"-C {self.LIVE} remote get-url origin": "git@github.com:acme/widgets.git",
        }
        responses.update(extra or {})

        def run(args, timeout):
            line = " ".join(args)
            for needle, out in responses.items():
                if needle in line:
                    return out
            return None
        return run

    def test_deleted_worktree_resolves_through_its_recorded_branch(self):
        prs = ('[{"number": 3850, "title": "Add redesigned registration flow",'
               '  "state": "MERGED", "url": "u", "headRefName": "sethherr/new-flow"},'
               ' {"number": 1, "title": "Live one", "state": "OPEN", "url": "u2",'
               '  "headRefName": "feature/live"}]')
        rows, stats = P.resolve_all(
            [self.LIVE, self.DEAD],
            recorded_branches={self.DEAD: "sethherr/new-flow", self.LIVE: "feature/live"},
            git_bin="git", gh_bin="gh",
            runner=self._runner({"pr list --repo acme/widgets --state all": prs}),
        )
        by_path = {r["path"]: r for r in rows}
        self.assertEqual(by_path[self.DEAD]["label"],
                         "widgets: #3850 - Add redesigned registration flow")
        self.assertEqual(by_path[self.DEAD]["pr_state"], "MERGED")
        self.assertEqual(by_path[self.DEAD]["inferred"], 1, "repo came from a sibling")
        self.assertEqual(by_path[self.LIVE]["inferred"], 0)
        self.assertEqual(stats["live"], 1)
        self.assertEqual(stats["inferred"], 1)
        self.assertEqual(stats["with_pr"], 2)

    def test_dead_workspace_without_a_pr_still_names_repo_and_branch(self):
        rows, _ = P.resolve_all(
            [self.LIVE, self.DEAD],
            recorded_branches={self.DEAD: "sethherr/never-pred"},
            git_bin="git", gh_bin="gh",
            runner=self._runner({"pr list": "[]"}),
        )
        dead = [r for r in rows if r["path"] == self.DEAD][0]
        self.assertEqual(dead["label"], "widgets: sethherr/never-pred")

    def test_no_sibling_means_no_guess(self):
        rows, _ = P.resolve_all(
            ["/elsewhere/orphan"],
            recorded_branches={"/elsewhere/orphan": "feature/x"},
            git_bin="git", gh_bin="gh", runner=self._runner(),
        )
        self.assertIsNone(rows[0]["label"])
        self.assertEqual(rows[0]["resolved"], 0)

    def test_generic_branch_names_are_not_attributed_by_inference(self):
        """`main` exists in every repo — inferring a repo for it proves nothing."""
        rows, _ = P.resolve_all(
            [self.LIVE, self.DEAD],
            recorded_branches={self.DEAD: "main"},
            git_bin="git", gh_bin="gh", runner=self._runner({"pr list": "[]"}),
        )
        dead = [r for r in rows if r["path"] == self.DEAD][0]
        self.assertIsNone(dead["label"])

    def test_bulk_fetch_is_one_call_per_repo(self):
        calls = []

        base = self._runner({"pr list": "[]"})

        def spy(args, timeout):
            line = " ".join(args)
            if "pr list" in line:
                calls.append(line)
            return base(args, timeout)

        P.resolve_all(
            [self.LIVE, self.DEAD, "/ws/repo/other"],
            recorded_branches={self.DEAD: "b1", "/ws/repo/other": "b2"},
            git_bin="git", gh_bin="gh", runner=spy,
        )
        bulk = [c for c in calls if "--state all --json number,title,state,url,headRefName" in c]
        self.assertEqual(len(bulk), 1, "one bulk fetch covers every workspace in the repo")

    def test_bulk_limit_zero_uses_targeted_queries_only(self):
        calls = []

        def spy(args, timeout):
            line = " ".join(args)
            if "pr list" in line:
                calls.append(line)
                return '[{"number": 4, "title": "T", "state": "OPEN", "url": "u"}]'
            return self._runner()(args, timeout)

        rows, stats = P.resolve_all(
            [self.LIVE, self.DEAD],
            recorded_branches={self.DEAD: "b1"},
            git_bin="git", gh_bin="gh", runner=spy, bulk_limit=0,
        )
        self.assertTrue(all("headRefName" not in c for c in calls), "no bulk paging")
        self.assertEqual(stats["pr_lookups"], 2)
        self.assertEqual(stats["with_pr"], 2)

    def test_targeted_lookups_are_budgeted(self):
        def runner(args, timeout):
            if "pr list" in " ".join(args):
                return '[{"number": 4, "title": "T", "state": "OPEN", "url": "u"}]'
            return self._runner()(args, timeout)

        paths = [f"/ws/repo/w{i}" for i in range(6)]
        rows, stats = P.resolve_all(
            [self.LIVE] + paths,
            recorded_branches={p: f"branch/{i}" for i, p in enumerate(paths)},
            git_bin="git", gh_bin="gh", runner=runner, bulk_limit=0, max_lookups=3,
        )
        self.assertEqual(stats["pr_lookups"], 3)
        self.assertEqual(stats["throttled"], 4)  # 6 dead + 1 live, minus 3 allowed


class SearchPrTests(unittest.TestCase):
    HIT = ('[{"number": 108, "title": "Overhaul the port skill", "state": "merged",'
           '  "url": "u", "repository": {"nameWithOwner": "sethherr/rails_template"}}]')

    def test_finds_the_pr_and_reports_its_repo(self):
        pr = P.search_pr("sethherr/update-port", gh_bin="gh",
                         runner=fake_runner({"search prs": self.HIT}))
        self.assertEqual(pr["number"], 108)
        self.assertEqual(pr["repo_slug"], "sethherr/rails_template")

    def test_state_is_normalised_to_upper_case(self):
        """`gh search` returns "merged"; `gh pr list` returns "MERGED"."""
        pr = P.search_pr("b", gh_bin="gh", runner=fake_runner({"search prs": self.HIT}))
        self.assertEqual(pr["state"], "MERGED")

    def test_matches_in_two_repos_are_refused(self):
        both = ('[{"number": 1, "title": "A", "state": "merged", "url": "u",'
                '  "repository": {"nameWithOwner": "me/a"}},'
                ' {"number": 2, "title": "B", "state": "merged", "url": "u",'
                '  "repository": {"nameWithOwner": "me/b"}}]')
        self.assertIsNone(P.search_pr("shared-name", gh_bin="gh",
                                      runner=fake_runner({"search prs": both})))

    def test_newest_pr_wins_within_one_repo(self):
        same = ('[{"number": 5, "title": "Old", "state": "merged", "url": "u",'
                '  "repository": {"nameWithOwner": "me/a"}},'
                ' {"number": 9, "title": "New", "state": "open", "url": "u",'
                '  "repository": {"nameWithOwner": "me/a"}}]')
        self.assertEqual(P.search_pr("b", gh_bin="gh",
                                     runner=fake_runner({"search prs": same}))["number"], 9)

    def test_generic_and_blank_branches_never_search(self):
        calls = []

        def spy(args, timeout):
            calls.append(args)
            return self.HIT

        for branch in ("", None, "main", "master"):
            self.assertIsNone(P.search_pr(branch, gh_bin="gh", runner=spy))
        self.assertEqual(calls, [])

    def test_owner_scoping_is_passed_through(self):
        seen = []

        def spy(args, timeout):
            seen.append(args)
            return self.HIT

        P.search_pr("b", owners=["sethherr", "bikeindex"], gh_bin="gh", runner=spy)
        self.assertEqual(seen[0].count("--owner"), 2)

    def test_junk_output_is_none(self):
        for out in ("not json", "[]", '{"a":1}', ""):
            self.assertIsNone(P.search_pr("b", gh_bin="gh",
                                          runner=fake_runner({"search prs": out})))


class ResolveAllSearchTierTests(unittest.TestCase):
    """Workspaces whose repo can't be inferred locally fall back to search."""

    def test_orphan_workspace_is_resolved_by_search(self):
        hit = ('[{"number": 23, "title": "Bundle luxon", "state": "merged", "url": "u",'
               '  "repository": {"nameWithOwner": "bikeindex/binxtils"}}]')
        rows, stats = P.resolve_all(
            ["/ws/binxtils/lahore-v2"],
            recorded_branches={"/ws/binxtils/lahore-v2": "sethherr/bundle-luxon"},
            git_bin="git", gh_bin="gh",
            runner=fake_runner({"search prs": hit}),
        )
        self.assertEqual(rows[0]["label"], "binxtils: #23 - Bundle luxon")
        self.assertEqual(rows[0]["repo_slug"], "bikeindex/binxtils")
        self.assertEqual(rows[0]["inferred"], 1)
        self.assertEqual(stats["found_by_search"], 1)

    def test_search_result_is_cached_per_branch(self):
        hit = ('[{"number": 5, "title": "T", "state": "merged", "url": "u",'
               '  "repository": {"nameWithOwner": "me/r"}}]')
        calls = []

        def spy(args, timeout):
            if "search prs" in " ".join(args):
                calls.append(args)
                return hit
            return None

        P.resolve_all(
            ["/ws/x/a", "/ws/x/b"],
            recorded_branches={"/ws/x/a": "same/branch", "/ws/x/b": "same/branch"},
            git_bin="git", gh_bin="gh", runner=spy,
        )
        self.assertEqual(len(calls), 1, "one search serves both workspaces")

    def test_searches_are_budgeted(self):
        paths = [f"/ws/x/w{i}" for i in range(5)]
        rows, stats = P.resolve_all(
            paths,
            recorded_branches={p: f"b/{i}" for i, p in enumerate(paths)},
            git_bin="git", gh_bin="gh",
            runner=fake_runner({"search prs": '[]'}), max_searches=2,
        )
        self.assertEqual(stats["searches"], 2)
        self.assertEqual(stats["throttled"], 3)


class SearchDisambiguationTests(unittest.TestCase):
    """A branch name can exist in two repos; the parent directory names one."""

    BOTH = ('[{"number": 101, "title": "Sync pr skill", "state": "merged", "url": "u1",'
            '  "repository": {"nameWithOwner": "sethherr/rails_template"}},'
            ' {"number": 3782, "title": "Trigger pr skill", "state": "merged", "url": "u2",'
            '  "repository": {"nameWithOwner": "bikeindex/bike_index"}}]')

    def test_ambiguous_without_a_hint(self):
        self.assertIsNone(P.search_pr("sethherr/update-pr-skill", gh_bin="gh",
                                      runner=fake_runner({"search prs": self.BOTH})))

    def test_hint_picks_the_matching_repo(self):
        pr = P.search_pr("sethherr/update-pr-skill", gh_bin="gh",
                         runner=fake_runner({"search prs": self.BOTH}),
                         prefer_repo="rails_template")
        self.assertEqual(pr["number"], 101)
        self.assertEqual(pr["repo_slug"], "sethherr/rails_template")

    def test_hint_is_case_insensitive(self):
        pr = P.search_pr("b", gh_bin="gh", runner=fake_runner({"search prs": self.BOTH}),
                         prefer_repo="Rails_Template")
        self.assertEqual(pr["repo_slug"], "sethherr/rails_template")

    def test_a_hint_matching_nothing_still_refuses(self):
        self.assertIsNone(P.search_pr("b", gh_bin="gh",
                                      runner=fake_runner({"search prs": self.BOTH}),
                                      prefer_repo="some-other-repo"))

    def test_hint_cannot_invent_a_repo_github_did_not_return(self):
        one = ('[{"number": 5, "title": "T", "state": "open", "url": "u",'
               '  "repository": {"nameWithOwner": "me/only"}}]')
        pr = P.search_pr("b", gh_bin="gh", runner=fake_runner({"search prs": one}),
                         prefer_repo="something-else")
        self.assertEqual(pr["repo_slug"], "me/only", "unambiguous result is unaffected")


class ParentDirNameTests(unittest.TestCase):
    def test_posix_and_windows(self):
        self.assertEqual(P.parent_dir_name("/ws/conductor/rails_template/lisbon"), "rails_template")
        self.assertEqual(P.parent_dir_name(r"C:\ws\rails_template\lisbon"), "rails_template")

    def test_trailing_separator_and_shallow_paths(self):
        self.assertEqual(P.parent_dir_name("/ws/repo/alpha/"), "repo")
        self.assertIsNone(P.parent_dir_name("/alpha"))
        self.assertIsNone(P.parent_dir_name(""))


class ResolveAllHintTests(unittest.TestCase):
    def test_orphan_workspace_resolves_via_the_directory_hint(self):
        both = SearchDisambiguationTests.BOTH
        rows, stats = P.resolve_all(
            ["/ws/conductor/rails_template/lisbon"],
            recorded_branches={"/ws/conductor/rails_template/lisbon": "sethherr/update-pr-skill"},
            git_bin="git", gh_bin="gh",
            runner=fake_runner({"search prs": both}),
        )
        self.assertEqual(rows[0]["label"], "rails_template: #101 - Sync pr skill")
        self.assertEqual(stats["found_by_search"], 1)

    def test_same_branch_under_two_parents_resolves_separately(self):
        """The search cache must key on the hint, not the branch alone."""
        both = SearchDisambiguationTests.BOTH
        paths = ["/ws/rails_template/a", "/ws/bike_index/b"]
        rows, _ = P.resolve_all(
            paths,
            recorded_branches={p: "shared/branch" for p in paths},
            git_bin="git", gh_bin="gh",
            runner=fake_runner({"search prs": both}),
        )
        by_path = {r["path"]: r for r in rows}
        self.assertEqual(by_path["/ws/rails_template/a"]["repo"], "rails_template")
        self.assertEqual(by_path["/ws/bike_index/b"]["repo"], "bike_index")
