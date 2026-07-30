"""Progress reporting for long scans: scan_dir's callback + the terminal bar."""
import io
import os
import shutil
import tempfile
import unittest

from token_dashboard.db import init_db
from token_dashboard.progress import TerminalProgress
from token_dashboard.scanner import scan_dir

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


class _FakeTTY(io.StringIO):
    encoding = "utf-8"

    def isatty(self):
        return True


class ScanProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        self.proj_root = os.path.join(self.tmp, "projects")
        for slug in ("C--work-a", "C--work-b"):
            d = os.path.join(self.proj_root, slug)
            os.makedirs(d)
            shutil.copy(
                os.path.join(FIXTURE_DIR, "sample_session.jsonl"),
                os.path.join(d, "s1.jsonl"),
            )
        init_db(self.db)
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _progress(self, done_files, total_files, done_bytes, total_bytes):
        self.calls.append((done_files, total_files, done_bytes, total_bytes))

    def test_progress_starts_at_zero_and_reaches_total(self):
        scan_dir(self.proj_root, self.db, progress=self._progress)
        self.assertEqual(self.calls[0][0], 0)
        self.assertEqual(self.calls[0][2], 0)
        last = self.calls[-1]
        self.assertEqual(last[0], last[1])          # every file accounted for
        self.assertEqual(last[2], last[3])          # bytes sum to the total
        self.assertEqual(last[1], 2)
        self.assertGreater(last[3], 0)

    def test_progress_is_monotonic(self):
        scan_dir(self.proj_root, self.db, progress=self._progress)
        files = [c[0] for c in self.calls]
        byts = [c[2] for c in self.calls]
        self.assertEqual(files, sorted(files))
        self.assertEqual(byts, sorted(byts))

    def test_unchanged_files_still_reach_100_percent(self):
        scan_dir(self.proj_root, self.db)
        self.calls.clear()
        n = scan_dir(self.proj_root, self.db, progress=self._progress)
        self.assertEqual(n["files"], 0)             # nothing re-ingested...
        last = self.calls[-1]
        self.assertEqual(last[0], last[1])          # ...but the bar still completes
        self.assertEqual(last[2], last[3])

    def test_missing_projects_dir_reports_empty(self):
        scan_dir(os.path.join(self.tmp, "nope"), self.db, progress=self._progress)
        self.assertEqual(self.calls, [(0, 0, 0, 0)])


class TerminalProgressTests(unittest.TestCase):
    def test_writes_percentage_to_a_tty(self):
        out = _FakeTTY()
        bar = TerminalProgress(stream=out, min_interval=0)
        bar(0, 4, 0, 1000)
        bar(2, 4, 500, 1000)
        bar(4, 4, 1000, 1000)
        text = out.getvalue()
        self.assertIn("  0%", text)
        self.assertIn(" 50%", text)
        self.assertIn("100%", text)
        self.assertIn("4/4 files", text)

    def test_silent_when_not_a_tty(self):
        out = io.StringIO()
        bar = TerminalProgress(stream=out, min_interval=0)
        bar(1, 2, 50, 100)
        bar.finish()
        self.assertEqual(out.getvalue(), "")

    def test_throttles_repeat_updates_at_the_same_percent(self):
        out = _FakeTTY()
        bar = TerminalProgress(stream=out, min_interval=1000)
        bar(1, 100, 10, 1000)
        first = out.getvalue()
        bar(2, 100, 11, 1000)   # still 1% and inside the interval — suppressed
        self.assertEqual(out.getvalue(), first)
        bar(50, 100, 500, 1000)  # percent changed — emitted
        self.assertIn(" 50%", out.getvalue())

    def test_final_update_is_never_throttled(self):
        out = _FakeTTY()
        bar = TerminalProgress(stream=out, min_interval=1000)
        bar(99, 100, 990, 1000)
        bar(100, 100, 1000, 1000)
        self.assertIn("100%", out.getvalue())

    def test_finish_erases_the_line(self):
        out = _FakeTTY()
        bar = TerminalProgress(stream=out, min_interval=0)
        bar(1, 2, 50, 100)
        bar.finish()
        self.assertTrue(out.getvalue().endswith("\r"))

    def test_ascii_fallback_for_non_utf8_streams(self):
        class _AsciiTTY(_FakeTTY):
            encoding = "cp1252"

        out = _AsciiTTY()
        TerminalProgress(stream=out, min_interval=0)(1, 2, 50, 100)
        self.assertNotIn("█", out.getvalue())
        self.assertIn("#", out.getvalue())


if __name__ == "__main__":
    unittest.main()
