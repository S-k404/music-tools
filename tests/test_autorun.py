import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

if importlib.util.find_spec("mutagen") is None or importlib.util.find_spec("PIL") is None:
    raise unittest.SkipTest("mutagen and Pillow are needed to run these tests (./setup.sh)")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import interactive  # noqa: E402
import run_all  # noqa: E402

CFG = {"music_dir": "/music", "lyrics": {"backup_dir": ""}}


class AutoRunTests(unittest.TestCase):
    def run_auto(self, argv, work=("2 junk files",), answer=None):
        """Run main() with the tools faked; returns (exit code, [(tool, args)])."""
        calls = []
        patches = [mock.patch.object(run_all, "run_tool", side_effect=lambda t, a: calls.append((t, a)) or 0),
                   mock.patch.object(run_all, "tidy_work", return_value=list(work)),
                   mock.patch.object(run_all, "load_config", return_value=CFG),
                   mock.patch.object(run_all, "confirm", return_value=bool(answer))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with redirect_stdout(io.StringIO()):
            return run_all.main(argv), calls

    def test_the_tidy_step_comes_first_and_applies_after_one_question(self):
        code, calls = self.run_auto([], answer=True)
        self.assertEqual(code, 0)
        self.assertEqual([t for t, _ in calls], ["layout", "art", "artists", "lyrics"])
        self.assertEqual(calls[0][1], ["--clean", "--merge-albums", "--apply", "--yes"])

    def test_a_no_answer_changes_nothing(self):
        code, calls = self.run_auto([], answer=False)
        self.assertEqual((code, calls), (1, []))

    def test_dry_run_never_asks_and_never_applies(self):
        code, calls = self.run_auto(["--dry-run"], answer=False)
        self.assertEqual(code, 0)
        self.assertEqual(calls[0], ("layout", ["--clean", "--merge-albums"]))
        self.assertTrue(all("--apply" not in a for _, a in calls))

    def test_yes_skips_the_question(self):
        _, calls = self.run_auto(["--yes"], answer=False)
        self.assertEqual(len(calls), 4)

    def test_nothing_to_tidy_means_no_tidy_step(self):
        _, calls = self.run_auto(["--yes"], work=())
        self.assertEqual([t for t, _ in calls], ["art", "artists", "lyrics"])

    def test_naming_folders_or_opting_out_skips_the_tidy_step(self):
        for argv in (["--yes", "YouTube"], ["--yes", "--no-layout"]):
            _, calls = self.run_auto(argv)
            self.assertNotIn("layout", [t for t, _ in calls], argv)

    def test_tags_follow_the_tidy_step_and_organize_comes_last(self):
        _, calls = self.run_auto(["--yes", "--tags", "--organize"])
        self.assertEqual([t for t, _ in calls], ["layout", "tags", "art", "artists", "lyrics", "organize"])


class LayoutMenuTests(unittest.TestCase):
    def pick(self, *choices):
        """Drive the submenu with these row numbers, then back; returns the layout runs."""
        runs = []
        answers = iter([*choices, None])
        with mock.patch.object(interactive, "menu", side_effect=lambda *a, **k: next(answers)), \
                mock.patch.object(interactive, "run_and_wait", side_effect=lambda n, a, c: runs.append((n, a))):
            interactive.App.layout(mock.Mock(LAYOUT_ACTIONS=interactive.App.LAYOUT_ACTIONS, explicit=None,
                                             cfg={}, status_lines=lambda cfg: []))
        return runs

    def test_each_row_runs_the_matching_layout_command(self):
        self.assertEqual(self.pick(0, 1, 2, 3, 4), [
            ("layout", []),
            ("layout", ["--clean", "--merge-albums", "--apply"]),
            ("layout", ["--merge-albums", "--apply"]),
            ("layout", ["--clean", "--apply"]),
            ("layout", ["--undo", "--apply"]),
        ])

    def test_going_back_runs_nothing(self):
        self.assertEqual(self.pick(), [])


if __name__ == "__main__":
    unittest.main()
