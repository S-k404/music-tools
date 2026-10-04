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


class RunsAuto:
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


class AutoRunTests(RunsAuto, unittest.TestCase):
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


class DedupeStepTests(RunsAuto, unittest.TestCase):
    def steps(self, argv, **kw):
        _, calls = self.run_auto(argv, **kw)
        return calls

    def test_duplicates_are_off_unless_asked_for(self):
        self.assertNotIn("duplicates", [t for t, _ in self.steps(["--yes"])])

    def test_the_duplicate_step_sits_after_tags_and_before_art(self):
        calls = self.steps(["--yes", "--tags", "--dedupe", "--organize"])
        self.assertEqual([t for t, _ in calls], ["layout", "tags", "duplicates", "art", "artists", "lyrics", "organize"])

    def test_it_shows_its_list_and_asks_unless_the_whole_run_was_told_yes(self):
        _, args = next(c for c in self.steps(["--dedupe"], answer=True) if c[0] == "duplicates")
        self.assertEqual(args, ["--delete", "--apply"])
        _, args = next(c for c in self.steps(["--dedupe", "--yes"]) if c[0] == "duplicates")
        self.assertEqual(args, ["--delete", "--apply", "--yes"])

    def test_a_dry_run_only_previews_the_removal(self):
        _, args = next(c for c in self.steps(["--dedupe", "--dry-run"]) if c[0] == "duplicates")
        self.assertEqual(args, ["--delete"])

    def test_the_organize_step_files_the_kept_songs_so_the_duplicate_step_does_not(self):
        _, args = next(c for c in self.steps(["--dedupe", "--organize", "--yes"]) if c[0] == "duplicates")
        self.assertIn("--no-fix-albums", args)
        _, args = next(c for c in self.steps(["--dedupe", "--yes"]) if c[0] == "duplicates")
        self.assertNotIn("--no-fix-albums", args)

    def test_no_auto_album_reaches_the_duplicate_steps_own_album_fix(self):
        _, args = next(c for c in self.steps(["--dedupe", "--no-auto-album", "--yes"]) if c[0] == "duplicates")
        self.assertIn("--no-auto-album", args)

    def test_named_folders_and_the_config_reach_the_duplicate_step(self):
        _, args = next(c for c in self.steps(["--dedupe", "--yes", "--config", "my.toml", "Mixes"]) if c[0] == "duplicates")
        self.assertEqual(args, ["--config", "my.toml", "--delete", "--apply", "--yes", "Mixes"])

    def test_a_no_answer_still_changes_nothing_with_duplicates_on(self):
        code, calls = self.run_auto(["--dedupe"], answer=False)
        self.assertEqual((code, calls), (1, []))

    def test_everything_switches_on_every_optional_step(self):
        calls = self.steps(["--everything", "--yes"])
        self.assertEqual([t for t, _ in calls], ["layout", "tags", "duplicates", "art", "artists", "lyrics", "organize"])

    def test_everything_still_honours_the_no_options(self):
        calls = self.steps(["--everything", "--yes", "--no-art", "--no-lyrics", "--no-layout"])
        self.assertEqual([t for t, _ in calls], ["tags", "duplicates", "artists", "organize"])


class AllInOneMenuTests(unittest.TestCase):
    # the checklist rows, in pipeline order
    ROWS = ["tidy", "tags", "dedupe", "art", "artists", "lyrics", "organize", "preview"]

    def run_menu(self, **on):
        """The `mt all` options the menu builds when exactly these rows are ticked (the rest take their defaults)."""
        shown = {}

        def fake_checklist(title, opts, label, header=()):
            shown["rows"] = [o[0] for o in opts]
            return [on.get(name, default) for name, default in zip(self.ROWS, (o[1] for o in opts))]

        runs = []
        with mock.patch.object(interactive, "checklist", side_effect=fake_checklist), \
                mock.patch.object(interactive, "run_and_wait", side_effect=lambda n, a, c: runs.append((n, a))):
            interactive.App.run_all(mock.Mock(explicit=None, cfg={}, status_lines=lambda cfg: []))
        return shown["rows"], runs

    def test_every_step_is_a_row_in_pipeline_order(self):
        rows, _ = self.run_menu()
        self.assertEqual(len(rows), len(self.ROWS))
        self.assertTrue(rows[2].startswith("Remove duplicate songs"))
        self.assertTrue(rows[1].startswith("Fix misidentified tags"))

    def test_the_defaults_run_the_everyday_steps_only(self):
        _, runs = self.run_menu()
        self.assertEqual(runs, [("all", ["--organize"])])

    def test_ticking_duplicates_and_tags_adds_their_options(self):
        _, runs = self.run_menu(dedupe=True, tags=True)
        self.assertEqual(runs, [("all", ["--organize", "--tags", "--dedupe"])])

    def test_every_row_can_be_switched_off_or_previewed(self):
        _, runs = self.run_menu(tidy=False, art=False, artists=False, lyrics=False, organize=False, preview=True, dedupe=True)
        self.assertEqual(runs, [("all", ["--no-layout", "--dry-run", "--no-art", "--no-artists", "--no-lyrics", "--dedupe"])])

    def test_every_option_it_builds_is_one_the_runner_accepts(self):
        _, runs = self.run_menu(tidy=False, art=False, artists=False, lyrics=False, organize=True, preview=True,
                                dedupe=True, tags=True)
        with redirect_stdout(io.StringIO()), mock.patch.object(run_all, "run_tool", return_value=0), \
                mock.patch.object(run_all, "tidy_work", return_value=[]), mock.patch.object(run_all, "load_config", return_value=CFG):
            self.assertEqual(run_all.main(runs[0][1]), 0)   # argparse would exit(2) on an unknown flag


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
        self.assertEqual(self.pick(0, 1, 2, 3, 4, 5), [
            ("layout", []),
            ("layout", ["--clean", "--merge-artists", "--merge-albums", "--apply"]),
            ("layout", ["--merge-artists", "--apply"]),
            ("layout", ["--merge-albums", "--apply"]),
            ("layout", ["--clean", "--apply"]),
            ("layout", ["--undo", "--apply"]),
        ])

    def test_going_back_runs_nothing(self):
        self.assertEqual(self.pick(), [])


if __name__ == "__main__":
    unittest.main()
