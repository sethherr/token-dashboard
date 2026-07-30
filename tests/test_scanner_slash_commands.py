"""Integration tests for slash-command Skill synthesis.

Covers both paths: (a) ingest-time synthesis via ``scan_dir`` emitting a
Skill row when a user record carries ``<command-name>/<slug></command-name>``,
and (b) one-shot ``rescan_slash_commands`` backfilling existing DBs whose
user messages were ingested before the extractor existed.
"""
import json
import os
import sqlite3
import tempfile
import time
import unittest

from token_dashboard.db import connect, init_db
from token_dashboard.scanner import rescan_slash_commands, scan_dir


def _write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _slash_user(uuid, ts, slug, ordering="name-first"):
    if ordering == "name-first":
        content = (
            f"<command-name>/{slug}</command-name>\n"
            f"<command-message>{slug}</command-message>\n"
            f"<command-args></command-args>"
        )
    else:
        content = (
            f"<command-message>{slug}</command-message>\n"
            f"<command-name>/{slug}</command-name>"
        )
    return {
        "type":        "user",
        "uuid":        uuid,
        "sessionId":   "s1",
        "timestamp":   ts,
        "isSidechain": False,
        "message":     {"role": "user", "content": content},
    }


class SlashCommandIngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        self.proj_root = os.path.join(self.tmp, "projects")
        self.proj_dir = os.path.join(self.proj_root, "C--work-sample")
        os.makedirs(self.proj_dir)
        init_db(self.db)

    def _path(self):
        return os.path.join(self.proj_dir, "s1.jsonl")

    def _count_target(self, target):
        with sqlite3.connect(self.db) as c:
            return c.execute(
                "SELECT COUNT(*) FROM tool_calls WHERE tool_name='Skill' AND target=?",
                (target,),
            ).fetchone()[0]

    def test_scan_emits_skill_row_for_slash_command(self):
        _write_jsonl(self._path(), [
            _slash_user("u1", "2026-04-24T07:12:56Z", "demo-cmd"),
        ])
        scan_dir(self.proj_root, self.db)
        self.assertEqual(self._count_target("demo-cmd"), 1)

    def test_rescan_without_content_change_does_not_duplicate(self):
        """Forced rescan (mtime bumped, content identical) must not double-count
        the synthetic Skill row — relies on scan_file's per-uuid DELETE."""
        _write_jsonl(self._path(), [
            _slash_user("u1", "2026-04-24T07:12:56Z", "demo-cmd"),
        ])
        scan_dir(self.proj_root, self.db)
        self.assertEqual(self._count_target("demo-cmd"), 1)

        future = time.time() + 10
        os.utime(self._path(), (future, future))
        scan_dir(self.proj_root, self.db)
        self.assertEqual(self._count_target("demo-cmd"), 1)

    def test_plugin_namespaced_slug_round_trips_through_db(self):
        _write_jsonl(self._path(), [
            _slash_user("u1", "2026-04-24T07:00:00Z", "codex:review"),
        ])
        scan_dir(self.proj_root, self.db)
        self.assertEqual(self._count_target("codex:review"), 1)


class SlashCommandBackfillTests(unittest.TestCase):
    """Verify rescan_slash_commands synthesizes rows from existing messages."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)

    def _seed_user_message(self, c, *, uuid, session, ts, content):
        c.execute(
            "INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, "
            "prompt_text, prompt_chars) "
            "VALUES (?, ?, 'p', 'user', ?, ?, ?)",
            (uuid, session, ts, content, len(content)),
        )

    def test_backfill_synthesizes_row_from_existing_message(self):
        slash = "<command-name>/demo-cmd</command-name>"
        with connect(self.db) as c:
            self._seed_user_message(
                c, uuid="u1", session="s1",
                ts="2026-04-24T07:12:56Z", content=slash,
            )
            c.commit()

        result = rescan_slash_commands(self.db)
        self.assertEqual(result["slash_commands_synthesized"], 1)

        with sqlite3.connect(self.db) as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT tool_name, target, session_id, timestamp "
                "FROM tool_calls WHERE message_uuid='u1'"
            ).fetchone()
        self.assertEqual(row["tool_name"], "Skill")
        self.assertEqual(row["target"], "demo-cmd")
        self.assertEqual(row["session_id"], "s1")
        self.assertEqual(row["timestamp"], "2026-04-24T07:12:56Z")

    def test_backfill_is_idempotent(self):
        slash = (
            "<command-message>demo-cmd</command-message>\n"
            "<command-name>/demo-cmd</command-name>"
        )
        with connect(self.db) as c:
            self._seed_user_message(
                c, uuid="u1", session="s1",
                ts="2026-04-24T07:12:56Z", content=slash,
            )
            c.commit()
        rescan_slash_commands(self.db)
        rescan_slash_commands(self.db)
        with sqlite3.connect(self.db) as c:
            cnt = c.execute(
                "SELECT COUNT(*) FROM tool_calls WHERE message_uuid='u1'"
            ).fetchone()[0]
        self.assertEqual(cnt, 1, "two backfill calls must leave one row, not two")

    def test_backfill_skips_non_slash_user_messages(self):
        with connect(self.db) as c:
            self._seed_user_message(
                c, uuid="u1", session="s1",
                ts="2026-04-24T07:00:00Z", content="normal user prompt",
            )
            self._seed_user_message(
                c, uuid="u2", session="s1",
                ts="2026-04-24T07:01:00Z",
                content="<command-name>/demo-skill</command-name>",
            )
            c.commit()
        result = rescan_slash_commands(self.db)
        self.assertEqual(result["slash_commands_synthesized"], 1)
        with sqlite3.connect(self.db) as c:
            targets = [
                r[0] for r in c.execute(
                    "SELECT target FROM tool_calls WHERE tool_name='Skill'"
                )
            ]
        self.assertEqual(targets, ["demo-skill"])


if __name__ == "__main__":
    unittest.main()
