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

CFG = {"music_dir": "/music", "lyrics": {"backup_dir": ""}, "workers": 4}


class RunAuto:
    def run_auto(self, argv, work=("2 junk files",), answer=None, chooser=False):
        """Run main() with the tools faked; returns (exit code, [(tool, args)]). The questions `mt auto` asks in
        a terminal are off unless `chooser` says what they should do (a function that sets the args)."""
        calls = []
        self.questions = []
        patches = [mock.patch.object(run_all, "run_tool", side_effect=lambda t, a: calls.append((t, a)) or 0),
                   mock.patch.object(run_all, "tidy_work", return_value=list(work)),
                   mock.patch.object(run_all, "load_config", return_value=CFG),
                   mock.patch.object(run_all, "confirm",
                                     side_effect=lambda q: self.questions.append(q) or (answer(q) if callable(answer) else bool(answer))),
                   mock.patch.object(run_all, "wants_chooser", return_value=bool(chooser))]
        if chooser:
            patches.append(mock.patch("auto_choose.choose", side_effect=lambda cfg, args, w, tidy: chooser(args) or True))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with redirect_stdout(io.StringIO()):
            return run_all.main(argv), calls


class AutoRunTests(RunAuto, unittest.TestCase):
    def test_the_tidy_step_comes_first_and_applies_after_one_question(self):
        code, calls = self.run_auto([], answer=True)
        self.assertEqual(code, 0)
        self.assertEqual([t for t, _ in calls], ["layout", "art", "artists", "lyrics"])
        self.assertEqual(calls[0][1], ["--clean", "--merge-albums", "--apply", "--yes"])

    def test_a_no_answer_changes_nothing(self):
        code, calls = self.run_auto(["--no-layout"], answer=False)   # only steps that add files: one question
        self.assertEqual((code, calls), (1, []))
        self.assertEqual(len(self.questions), 1)

    def test_a_step_that_changes_files_asks_on_its_own_and_no_skips_just_that_step(self):
        code, calls = self.run_auto([], answer=False)
        self.assertEqual(code, 0)
        self.assertEqual([t for t, _ in calls], ["art", "artists", "lyrics"])   # the tidy was declined, the rest added files
        self.assertEqual(len(self.questions), 1)
        self.assertIn("tidy the folders", self.questions[0])

    def test_each_of_the_four_steps_that_change_files_gets_its_own_question(self):
        _, calls = self.run_auto(["--tags", "--delete-strays", "--organize"], answer=False)
        self.assertEqual([t for t, _ in calls], ["art", "artists", "lyrics"])
        asked = " | ".join(self.questions)
        self.assertEqual(len(self.questions), 4, asked)
        for word in ("tidy the folders", "rewrite", "loose copies", "Artist/Album"):
            self.assertIn(word, asked)

    def test_you_can_say_yes_to_some_and_no_to_others(self):
        _, calls = self.run_auto(["--tags", "--delete-strays", "--organize"], answer=lambda q: "rewrite" in q or "Artist/Album" in q)
        self.assertEqual([t for t, _ in calls], ["tags", "art", "artists", "lyrics", "organize"])

    def test_saying_no_to_everything_that_is_left_stops_the_run(self):
        code, calls = self.run_auto(["--tags", "--no-layout", "--no-art", "--no-artists", "--no-lyrics"], answer=False)
        self.assertEqual((code, calls), (1, []))

    def test_yes_and_dry_run_ask_nothing_even_with_every_step_on(self):
        for flag in ("--yes", "--dry-run"):
            _, calls = self.run_auto([flag, "--tags", "--delete-strays", "--organize"], answer=False)
            self.assertEqual(self.questions, [], flag)
            self.assertEqual([t for t, _ in calls], ["layout", "tags", "duplicates", "art", "artists", "lyrics", "organize"])

    def test_the_questions_only_cover_steps_that_are_going_to_run(self):
        self.run_auto(["--no-layout", "--tags"], answer=True)
        self.assertEqual(len(self.questions), 1)
        self.assertIn("rewrite", self.questions[0])

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


    def test_stray_songs_are_removed_after_the_tidy_and_tags_and_before_any_art(self):
        _, calls = self.run_auto(["--yes", "--tags", "--delete-strays"])
        self.assertEqual([t for t, _ in calls], ["layout", "tags", "duplicates", "art", "artists", "lyrics"])
        self.assertEqual(calls[2][1], ["--delete-strays", "--apply", "--yes"])

    def test_stray_songs_are_only_previewed_in_a_dry_run(self):
        _, calls = self.run_auto(["--dry-run", "--delete-strays"])
        self.assertIn(("duplicates", ["--delete-strays"]), calls)

    def test_removing_songs_says_where_they_go(self):
        self.run_auto(["--no-layout", "--delete-strays"])
        self.assertTrue(any(word in " ".join(self.questions) for word in ("Trash", "can't be brought back")))


class ChooserTests(RunAuto, unittest.TestCase):
    """`mt auto` on its own in a terminal asks what to do; with options, or without a terminal, it doesn't."""

    def parse(self, argv):
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("paths", nargs="*")
        for flag in ("--dry-run", "--yes", "--no-layout", "--organize", "--tags", "--no-art", "--no-artists",
                     "--no-lyrics", "--interactive", "--no-auto-album", "--delete-strays"):
            parser.add_argument(flag, action="store_true")
        parser.add_argument("--config")
        return parser, parser.parse_args(argv)

    def wants(self, argv, tty=True):
        parser, args = self.parse(argv)
        with mock.patch.object(run_all.sys.stdin, "isatty", return_value=tty), \
                mock.patch.object(run_all.sys.stdout, "isatty", return_value=tty):
            return run_all.wants_chooser(args, parser)

    def test_only_a_bare_command_in_a_terminal_asks(self):
        self.assertTrue(self.wants([]))
        self.assertTrue(self.wants(["--config", "x.toml"]))
        self.assertFalse(self.wants([], tty=False))
        for argv in (["--yes"], ["--dry-run"], ["--no-art"], ["--organize"], ["--delete-strays"], ["Mixes"]):
            self.assertFalse(self.wants(argv), argv)

    def test_what_you_pick_becomes_the_steps_that_run(self):
        def pick(args):
            args.no_lyrics, args.no_layout, args.delete_strays, args.yes = True, True, True, True
        code, calls = self.run_auto([], chooser=pick)
        self.assertEqual(code, 0)
        self.assertEqual([t for t, _ in calls], ["duplicates", "art", "artists"])

    def test_picking_preview_runs_the_steps_without_changing_anything(self):
        def pick(args):
            args.dry_run = True
            args.delete_strays = True
        _, calls = self.run_auto([], chooser=pick)
        self.assertTrue(all("--apply" not in a for _, a in calls))

    def test_backing_out_changes_nothing(self):
        with mock.patch("auto_choose.choose", return_value=False), \
                mock.patch.object(run_all, "wants_chooser", return_value=True), \
                mock.patch.object(run_all, "load_config", return_value=CFG), \
                mock.patch.object(run_all, "tidy_work", return_value=[]), \
                mock.patch.object(run_all, "run_tool") as run, redirect_stdout(io.StringIO()):
            self.assertEqual(run_all.main([]), 1)
        run.assert_not_called()


class AutoChooseTests(unittest.TestCase):
    """auto_choose.choose: what is ticked to start with, and how the answers turn into options."""

    def setUp(self):
        import argparse
        import auto_choose
        self.ac = auto_choose
        self.args = argparse.Namespace(no_layout=False, delete_strays=False, tags=False, no_art=False, no_artists=False,
                                       no_lyrics=False, organize=False, dry_run=False, yes=False)
        self.cfg = {"music_dir": "/music"}

    def run_choose(self, found, answers, songs=100, tidy=("3 junk files",)):
        """answers: successive replies of the checklist (a list of booleans, or None for 'back')."""
        shown = []

        def fake_checklist(title, options, run_label, header=()):
            shown.append([list(o) for o in options])
            return next(answers_iter)

        answers_iter = iter(answers)
        with mock.patch.object(self.ac, "look", return_value=(found, songs)), \
                mock.patch.object(self.ac, "folder_problem", return_value=""), \
                mock.patch.object(self.ac, "heading"), mock.patch.object(self.ac, "flash") as flash, \
                mock.patch.object(self.ac, "checklist", side_effect=fake_checklist):
            ok = self.ac.choose(self.cfg, self.args, 4, list(tidy))
        return ok, shown, flash

    def Found(self, hint, todo):
        return self.ac.Found(hint, todo)

    def test_steps_with_something_to_do_start_ticked_and_the_rest_do_not(self):
        found = {"layout": self.Found("3 junk files", True), "art": self.Found("every song has art", False),
                 "artists": self.Found("37 of 410 artists have none", True), "lyrics": self.Found("done", False),
                 "strays": self.Found("12 loose copies", True)}
        _, shown, _ = self.run_choose(found, [None])
        ticked = {row[0]: row[1] for row in shown[0]}
        self.assertEqual(ticked["Tidy folders *"], True)
        self.assertEqual(ticked["Add missing cover art"], False)
        self.assertEqual(ticked["Find artist pictures"], True)
        self.assertEqual(ticked["Find and translate lyrics"], False)
        # songs are only removed or moved when asked for, even though loose copies were found
        self.assertEqual(ticked["Remove loose duplicate songs *"], False)
        self.assertEqual(ticked["Fix wrong tags from filenames *"], False)
        self.assertEqual(ticked["Sort into Artist/Album folders *"], False)
        self.assertEqual(ticked["Preview only (dry run)"], False)
        self.assertEqual(ticked["Apply without asking again"], False)
        self.assertIn("37 of 410 artists have none", [row[2] for row in shown[0]])

    def test_removing_songs_tags_and_sorting_are_never_ticked_for_you(self):
        found = {k: self.Found("lots", True) for k, _, _ in self.ac.STEPS}
        ticked = self.ac.defaults_for(found)
        self.assertEqual([k for k, on in ticked.items() if on], ["layout", "art", "artists", "lyrics"])

    def test_the_defaults_when_nothing_was_looked_up(self):
        defaults = self.ac.defaults_for({})
        self.assertEqual(defaults, {"layout": True, "strays": False, "tags": False, "art": True, "artists": True,
                                    "lyrics": True, "organize": False})

    def test_the_answers_set_the_options(self):
        n = len(self.ac.STEPS)
        # layout off, strays on, tags on, art off, artists on, lyrics off, organize on; no preview, apply
        answer = [False, True, True, False, True, False, True, False, True]
        ok, _, _ = self.run_choose({}, [answer])
        self.assertTrue(ok)
        self.assertEqual(len(answer), n + 2)
        self.assertEqual((self.args.no_layout, self.args.delete_strays, self.args.tags, self.args.no_art,
                          self.args.no_artists, self.args.no_lyrics, self.args.organize, self.args.dry_run,
                          self.args.yes), (True, True, True, True, False, True, True, False, True))

    def test_preview_and_apply_together_ask_again_and_remember_the_other_ticks(self):
        both = [True, False, False, True, True, True, False, True, True]
        fine = [True, False, False, True, True, True, False, True, False]
        ok, shown, flash = self.run_choose({}, [both, fine])
        self.assertTrue(ok)
        flash.assert_called_once()
        self.assertEqual([row[1] for row in shown[1]], both)   # the second time shows what was ticked before
        self.assertEqual((self.args.dry_run, self.args.yes), (True, False))

    def test_backing_out_returns_false_and_changes_no_option(self):
        ok, _, _ = self.run_choose({}, [None])
        self.assertFalse(ok)
        self.assertEqual((self.args.no_layout, self.args.yes, self.args.dry_run), (False, False, False))

    def test_an_unreachable_music_folder_stops_before_looking(self):
        with mock.patch.object(self.ac, "folder_problem", return_value="not mounted"), \
                mock.patch.object(self.ac, "look") as look, redirect_stdout(io.StringIO()):
            self.assertFalse(self.ac.choose(self.cfg, self.args, 4, []))
        look.assert_not_called()


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
