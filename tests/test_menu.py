import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if importlib.util.find_spec("mutagen") is None or importlib.util.find_spec("PIL") is None:
    raise unittest.SkipTest("mutagen and Pillow are needed to run these tests (./setup.sh)")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import interactive  # noqa: E402


class RenderTests(unittest.TestCase):
    """A menu has to fit the window: long lines are cut, tall lists scroll with the cursor."""

    def test_a_short_line_is_left_alone_and_a_long_one_is_cut_with_an_ellipsis(self):
        self.assertEqual(interactive._fit_line("short", 20), "short")
        cut = interactive._fit_line("x" * 50, 20)
        self.assertEqual(interactive._shown_len(cut.replace("\x1b[0m", "")), 20)
        self.assertIn("…", cut)

    def test_colour_codes_do_not_count_and_survive_the_cut(self):
        line = interactive.green("[x]") + " " + interactive.dim("a long hint that will not fit on the line")
        cut = interactive._fit_line(line, 12)
        self.assertLessEqual(interactive._shown_len(cut), 12)
        self.assertTrue(cut.startswith(interactive.green("[x]")))
        self.assertEqual(interactive._fit_line(line, 200), line)

    def test_wide_characters_count_double(self):
        cut = interactive._fit_line("歌" * 20, 11)
        self.assertLessEqual(interactive._shown_len(cut.replace("\x1b[0m", "")), 11)

    def test_window_keeps_the_cursor_in_view(self):
        self.assertEqual(interactive._window(5, 4, 10), (0, 5))
        for cursor in range(30):
            first, last = interactive._window(30, cursor, 8)
            self.assertEqual(last - first, 8)
            self.assertTrue(first <= cursor < last, cursor)
        self.assertEqual(interactive._window(30, 0, 8), (0, 8))
        self.assertEqual(interactive._window(30, 29, 8), (22, 30))

    def render(self, rows, cursor, columns=80, lines=24, header=()):
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        size = mock.Mock(columns=columns, lines=lines)
        with mock.patch.object(interactive.shutil, "get_terminal_size", return_value=size), \
                mock.patch.object(interactive, "ANSI", False), redirect_stdout(out):
            interactive._render("Title", list(header), rows, cursor, "footer")
        return out.getvalue().split("\n")

    def test_every_rendered_line_fits_the_window_width(self):
        rows = [("A label that is rather long indeed", "and a hint that is far, far longer than the screen is wide " * 2)] * 3
        shown = self.render(rows, 0, columns=60, header=["Music folder  /Volumes/" + "x" * 90])
        self.assertTrue(all(interactive._shown_len(line) <= 59 for line in shown), shown)

    def test_a_tall_list_scrolls_and_says_how_many_are_hidden(self):
        rows = [(f"row {i}", "") for i in range(40)]
        top = self.render(rows, 0, lines=24)
        self.assertLessEqual(len(top), 24)
        self.assertTrue(any("row 0" in line for line in top))
        self.assertFalse(any("row 39" in line for line in top))
        self.assertTrue(any("↓" in line and "more" in line for line in top))
        bottom = self.render(rows, 39, lines=24)
        self.assertLessEqual(len(bottom), 24)
        self.assertTrue(any("row 39" in line for line in bottom))
        self.assertTrue(any("↑" in line and "more" in line for line in bottom))
        middle = self.render(rows, 20, lines=24)
        self.assertTrue(any("row 20" in line for line in middle))
        self.assertTrue(any("↑" in line for line in middle) and any("↓" in line for line in middle))

    def test_a_short_list_does_not_scroll(self):
        shown = self.render([(f"row {i}", "") for i in range(5)], 2)
        self.assertFalse(any("more" in line for line in shown))


class MenuCase(unittest.TestCase):
    """A real App on a temporary config, with the things a screen runs recorded instead of run."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.config = self.dir / "c.toml"
        self.config.write_text(f"music_dir = {json.dumps(str(self.dir))}\n", encoding="utf-8")
        self.app = interactive.App(str(self.config))
        self.runs = []
        for name in ("run_and_wait", "flash"):
            patcher = mock.patch.object(interactive, name, side_effect=self.record(name))
            patcher.start()
            self.addCleanup(patcher.stop)

    def record(self, name):
        return lambda *a, **k: self.runs.append((name, *a)) or None

    def drive(self, method, *choices):
        """Run a screen, answering its menu with these row numbers (then 'back'); returns the rows it showed."""
        answers = iter([*choices, None])
        shown = []

        def fake_menu(title, rows, header=(), start=0, back_label="back", big=False):
            shown.append((title, [row[0] for row in rows], [row[1] for row in rows]))
            return next(answers)

        with mock.patch.object(interactive, "menu", side_effect=fake_menu):
            method()
        return shown


class MenuTests(MenuCase):
    """The goal screens: what each row leads to. The screens themselves are replaced by mocks."""

    def test_the_main_menu_is_goal_first_and_each_row_opens_its_screen(self):
        names = ("run_all", "tidy", "add_missing", "check", "folders", "settings", "logs", "help")
        for name in names:
            setattr(self.app, name, mock.Mock(name=name))
        shown = self.drive(self.app.main, *range(len(names)))
        self.assertEqual(shown[0][1], ["Do it all for me", "Tidy my files", "Add what's missing", "Check my library",
                                       "Folders", "Settings", "Logs", "Help", "Quit"])
        for name in names:
            getattr(self.app, name).assert_called_once_with()

    def test_choosing_quit_leaves(self):
        with mock.patch.object(interactive, "clear"):
            self.drive(self.app.main, 8)

    def test_do_it_all_hands_over_to_the_tool_that_asks(self):
        self.app.run_all()
        self.assertEqual(self.runs, [("run_and_wait", "all", [], str(self.config))])

    def test_tidy_screen_rows(self):
        for name in ("layout", "duplicates", "organize", "fix_tags"):
            setattr(self.app, name, mock.Mock(name=name))
        shown = self.drive(self.app.tidy, 0, 1, 2, 3, 4)
        self.assertEqual(shown[0][1], ["Clean up junk and duplicate folders", "Remove duplicate songs",
                                       "Sort songs into Artist/Album folders", "Fix wrong tags", "Undo the last tidy"])
        for name in ("layout", "duplicates", "organize", "fix_tags"):
            getattr(self.app, name).assert_called_once_with()
        self.assertEqual(self.runs, [("run_and_wait", "layout", ["--undo", "--apply"], str(self.config))])

    def test_add_screen_rows_and_the_retry_count(self):
        for name in ("add_art", "artists", "lyrics", "retry"):
            setattr(self.app, name, mock.Mock(name=name))
        with mock.patch.object(self.app, "failed_count", return_value=0):
            shown = self.drive(self.app.add_missing, 0, 1, 2, 3)
        self.assertEqual(shown[0][2][3], "nothing to retry")
        for name in ("add_art", "artists", "lyrics", "retry"):
            getattr(self.app, name).assert_called_once_with()
        with mock.patch.object(self.app, "failed_count", return_value=7):
            shown = self.drive(self.app.add_missing)
        self.assertEqual(shown[0][1][3], "Retry songs that failed (7)")

    def test_check_screen_lists_change_nothing(self):
        self.app.stats, self.app.find_missing = mock.Mock(), mock.Mock()
        with mock.patch.object(self.app, "pick_scope", return_value=[]):
            self.drive(self.app.check, 0, 1, 2, 3, 4)
        self.app.stats.assert_called_once_with()
        self.app.find_missing.assert_called_once_with()
        self.assertEqual([r[1:3] for r in self.runs],
                         [("artists", ["--list-missing"]), ("lyrics", ["--list-missing"]), ("duplicates", [])])

    def test_check_screen_passes_a_chosen_folder_on(self):
        with mock.patch.object(self.app, "pick_scope", return_value=["/Music/Mixes"]):
            self.drive(self.app.check, 2)
        self.assertEqual(self.runs[0][1:3], ("artists", ["--list-missing", "/Music/Mixes"]))

    def test_going_back_from_a_scope_question_runs_nothing(self):
        with mock.patch.object(self.app, "pick_scope", return_value=None):
            self.drive(self.app.check, 2, 3, 4)
        self.assertEqual(self.runs, [])


class DuplicatesScreenTests(MenuCase):
    def run_screen(self, *replies, scope=()):
        """Answer the options checklist with these replies in turn (a list of three booleans, or None)."""
        answers = iter(replies)
        shown = []

        def fake_checklist(title, options, run_label, header=()):
            shown.append([list(o) for o in options])
            return next(answers)

        with mock.patch.object(self.app, "pick_scope", return_value=None if scope is None else list(scope)), \
                mock.patch.object(interactive, "checklist", side_effect=fake_checklist):
            self.app.duplicates()
        return shown

    def runs_of(self, kind):
        return [r[1:3] for r in self.runs if r[0] == kind]

    def test_only_listing_is_the_default(self):
        self.run_screen([False, False, False])
        self.assertEqual(self.runs_of("run_and_wait"), [("duplicates", [])])

    def test_remove_alone_previews(self):
        self.run_screen([True, False, False])
        self.assertEqual(self.runs_of("run_and_wait"), [("duplicates", ["--delete-strays"])])

    def test_preview_then_apply_are_separate_boxes(self):
        self.run_screen([True, True, False])
        self.assertEqual(self.runs_of("run_and_wait"), [("duplicates", ["--delete-strays", "--dry-run"])])
        self.runs.clear()
        self.run_screen([True, False, True], scope=["/Music/Mixes"])
        self.assertEqual(self.runs_of("run_and_wait"), [("duplicates", ["/Music/Mixes", "--delete-strays", "--apply"])])

    def test_preview_and_apply_together_ask_again_and_remember_the_ticks(self):
        shown = self.run_screen([True, True, True], [True, False, True])
        self.assertEqual(len(self.runs_of("flash")), 1)
        self.assertEqual([row[1] for row in shown[1]], [True, True, True])
        self.assertEqual(self.runs_of("run_and_wait"), [("duplicates", ["--delete-strays", "--apply"])])

    def test_apply_without_remove_is_refused_and_asked_again(self):
        self.run_screen([False, False, True], None)
        self.assertEqual(len(self.runs_of("flash")), 1)
        self.assertEqual(self.runs_of("run_and_wait"), [])

    def test_backing_out_runs_nothing(self):
        self.run_screen(None)
        self.run_screen(None, scope=None)
        self.assertEqual(self.runs_of("run_and_wait"), [])

if __name__ == "__main__":
    unittest.main()
