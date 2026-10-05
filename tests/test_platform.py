"""Behaviour that differs between Windows, macOS and Linux. Each system's branch is driven here by faking the
flags and the programs involved, so every branch is tested on every system (and the real one on each CI runner)."""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if importlib.util.find_spec("mutagen") is None or importlib.util.find_spec("PIL") is None:
    raise unittest.SkipTest("mutagen and Pillow are needed to run these tests (./setup.sh)")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import common  # noqa: E402
import find_artist_art  # noqa: E402
import interactive  # noqa: E402
import organize_music  # noqa: E402


class YtDlpCommandTests(unittest.TestCase):
    def test_a_yt_dlp_on_the_path_is_used(self):
        with mock.patch.object(common.shutil, "which", return_value="/opt/bin/yt-dlp"):
            self.assertEqual(common.ytdlp_command(), ["/opt/bin/yt-dlp"])

    def test_the_installed_package_is_run_as_a_module_when_nothing_is_on_the_path(self):
        # what ./setup.sh gives you: yt-dlp inside .venv, whose bin / Scripts folder isn't on PATH
        with mock.patch.object(common.shutil, "which", return_value=None), \
                mock.patch.object(common.importlib.util, "find_spec", return_value=object()):
            self.assertEqual(common.ytdlp_command(), [sys.executable, "-m", "yt_dlp"])

    def test_none_when_it_isnt_installed_at_all(self):
        with mock.patch.object(common.shutil, "which", return_value=None), \
                mock.patch.object(common.importlib.util, "find_spec", return_value=None):
            self.assertIsNone(common.ytdlp_command())

    def test_install_hints_name_the_package_manager_of_the_system(self):
        for mac, windows, expected in [(True, False, "brew install yt-dlp"),
                                       (False, True, "winget install yt-dlp.yt-dlp"),
                                       (False, False, "pip install yt-dlp")]:
            with mock.patch.object(common, "IS_MAC", mac), mock.patch.object(common, "IS_WINDOWS", windows):
                self.assertEqual(common.install_hint("yt-dlp"), expected)


class PathTextTests(unittest.TestCase):
    def test_windows_paths_keep_their_backslashes(self):
        with mock.patch.object(common, "IS_WINDOWS", True):
            self.assertEqual(common.clean_path(r"C:\Users\me\Music"), r"C:\Users\me\Music")
            self.assertEqual(common.clean_path('"C:\\Users\\me\\My Music"  '), r"C:\Users\me\My Music")
            self.assertEqual(common.clean_path(r"\\nas\share\Music"), r"\\nas\share\Music")

    def test_dragged_paths_are_unescaped_on_macos_and_linux(self):
        with mock.patch.object(common, "IS_WINDOWS", False):
            self.assertEqual(common.clean_path("/Volumes/My\\ Drive/Music\\ Mix "), "/Volumes/My Drive/Music Mix")
            self.assertEqual(interactive.clean_path("'/mnt/My Drive/Mix'"), "/mnt/My Drive/Mix")

    def test_quoting_for_the_shell_the_user_will_paste_into(self):
        with mock.patch.object(common, "IS_WINDOWS", True):
            self.assertEqual(common.shell_quote("Daft Punk"), '"Daft Punk"')
            self.assertEqual(common.shell_quote("plain"), "plain")
        with mock.patch.object(common, "IS_WINDOWS", False):
            self.assertEqual(common.shell_quote("Daft Punk"), "'Daft Punk'")


class WindowsNameTests(unittest.TestCase):
    def test_reserved_names_get_an_underscore_on_windows_only(self):
        with mock.patch.object(common, "IS_WINDOWS", True):
            self.assertEqual(common.windows_safe("CON"), "CON_")
            self.assertEqual(common.windows_safe("aux.jpg"), "aux_.jpg")
            self.assertEqual(common.windows_safe("Com1"), "Com1_")
            for fine in ("Console", "COM10", "Nullable", "Auxiliary.jpg"):
                self.assertEqual(common.windows_safe(fine), fine)
        with mock.patch.object(common, "IS_WINDOWS", False):
            self.assertEqual(common.windows_safe("CON"), "CON")

    def test_the_tools_use_it_for_the_names_they_create(self):
        with mock.patch.object(common, "IS_WINDOWS", True):
            self.assertEqual(find_artist_art.safe_filename("AUX"), "AUX_")
            self.assertEqual(organize_music.sanitize_name("Nul"), "Nul_")
            self.assertEqual(organize_music.sanitize_name("Normal Name"), "Normal Name")


class ConfigLocationTests(unittest.TestCase):
    def test_windows_also_looks_in_appdata(self):
        with tempfile.TemporaryDirectory() as appdata, mock.patch.object(common, "IS_WINDOWS", True), \
                mock.patch.dict(os.environ, {"APPDATA": appdata}):
            places = list(common.config_candidates())
        wanted = Path(appdata) / "music-tools" / "config.toml"
        self.assertIn(wanted, places)
        self.assertLess(places.index(common.HERE / "config.toml"), places.index(wanted))

    def test_other_systems_do_not(self):
        with tempfile.TemporaryDirectory() as appdata, mock.patch.object(common, "IS_WINDOWS", False), \
                mock.patch.dict(os.environ, {"APPDATA": appdata}):
            self.assertNotIn(Path(appdata) / "music-tools" / "config.toml", list(common.config_candidates()))


class KeyReadingTests(unittest.TestCase):
    """The Windows menu keys, read through a stand-in for the msvcrt module."""

    class FakeMsvcrt:
        def __init__(self, *keys):
            self.keys = list(keys)

        def kbhit(self):
            return bool(self.keys)

        def getwch(self):
            return self.keys.pop(0)

    def read(self, *keys, timeout=None):
        with mock.patch.object(interactive, "msvcrt", self.FakeMsvcrt(*keys)), \
                mock.patch.object(interactive, "_TERMIOS_AVAILABLE", False):
            return interactive.read_key(timeout)

    def test_arrows_come_as_a_prefix_and_a_code(self):
        self.assertEqual(self.read("\xe0", "H"), "up")
        self.assertEqual(self.read("\xe0", "P"), "down")
        self.assertEqual(self.read("\x00", "M"), "right")
        self.assertEqual(self.read("\xe0", "K"), "left")

    def test_other_keys(self):
        self.assertEqual(self.read("\r"), "enter")
        self.assertEqual(self.read(" "), "space")
        self.assertEqual(self.read("\x1b"), "esc")
        self.assertEqual(self.read("q"), "q")
        self.assertEqual(self.read("7"), "7")

    def test_a_key_the_menu_doesnt_know_is_ignored_not_taken_for_escape(self):
        self.assertEqual(self.read("\xe0", "S"), "")  # Delete

    def test_ctrl_c_interrupts(self):
        with self.assertRaises(KeyboardInterrupt):
            self.read("\x03")

    def test_waiting_gives_up_after_the_timeout(self):
        self.assertIsNone(self.read(timeout=0.03))

    def test_the_arrow_menu_is_on_when_either_kind_of_terminal_input_exists(self):
        with mock.patch.object(interactive, "_TERMIOS_AVAILABLE", False), \
                mock.patch.object(interactive, "msvcrt", None), \
                mock.patch.object(sys.stdin, "isatty", return_value=True), \
                mock.patch.object(sys.stdout, "isatty", return_value=True):
            self.assertFalse(interactive.raw_mode())
            with mock.patch.object(interactive, "msvcrt", self.FakeMsvcrt()):
                self.assertTrue(interactive.raw_mode())


class RunToolTests(unittest.TestCase):
    def run_tool(self, windows):
        seen = {}
        with mock.patch.object(interactive, "IS_WINDOWS", windows), \
                mock.patch.object(interactive.subprocess, "call", side_effect=lambda cmd, **kw: seen.update(kw) or 0):
            interactive.run_tool("stats", ["--help"])
        return seen

    def test_windows_has_no_preexec_fn(self):
        self.assertNotIn("preexec_fn", self.run_tool(windows=True))  # subprocess raises ValueError if it's passed

    def test_macos_and_linux_reset_ctrl_c_in_the_child(self):
        self.assertIn("preexec_fn", self.run_tool(windows=False))


class OpenPathTests(unittest.TestCase):
    def open(self, path, *, windows=False, mac=False, edit=False, call=None):
        call = call or mock.Mock(return_value=0)
        with mock.patch.object(common, "IS_WINDOWS", windows), mock.patch.object(common, "IS_MAC", mac), \
                mock.patch.object(common.subprocess, "call", call):
            return common.open_path(path, edit=edit), call

    def test_macos(self):
        self.assertEqual(self.open("f.toml", mac=True, edit=True)[1].call_args.args[0], ["open", "-t", "f.toml"])
        self.assertEqual(self.open("logs", mac=True)[1].call_args.args[0], ["open", "logs"])

    def test_linux(self):
        ok, call = self.open("logs")
        self.assertTrue(ok)
        self.assertEqual(call.call_args.args[0], ["xdg-open", "logs"])

    def test_a_machine_without_a_desktop_says_no_instead_of_crashing(self):
        ok, _ = self.open("logs", call=mock.Mock(side_effect=FileNotFoundError("xdg-open")))
        self.assertFalse(ok)

    def test_windows_edits_in_notepad_and_opens_folders_in_explorer(self):
        ok, call = self.open("config.toml", windows=True, edit=True)
        self.assertTrue(ok)
        self.assertEqual(call.call_args.args[0], ["notepad", "config.toml"])
        with mock.patch.object(os, "startfile", create=True) as startfile:
            ok, _ = self.open("logs", windows=True)
        self.assertTrue(ok)
        startfile.assert_called_once_with("logs")


class TextEncodingTests(unittest.TestCase):
    def test_program_output_is_read_as_utf8(self):
        r = subprocess.run([sys.executable, "-c", r"import sys; sys.stdout.buffer.write('\u266b \u96e8\u826f \xe9'.encode('utf-8'))"],
                           **common.subprocess_text())
        self.assertEqual(r.stdout, "\u266b \u96e8\u826f \xe9")

    def test_bytes_that_are_not_text_are_replaced_not_fatal(self):
        r = subprocess.run([sys.executable, "-c", r"import sys; sys.stdout.buffer.write(b'ok \xff')"], **common.subprocess_text())
        self.assertEqual(r.stdout, "ok \ufffd")

    def test_printing_a_title_the_terminal_cant_show_does_not_crash(self):
        env = {**os.environ, "PYTHONIOENCODING": "ascii"}  # like a Windows pipe, or a terminal with an old code page
        r = subprocess.run([sys.executable, "-c", r"import common; print('\u266b \u96e8\u826f')"], cwd=ROOT, env=env,
                           capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.strip())

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_turning_on_console_colours_works_when_output_is_not_a_console(self):
        self.assertTrue(common._enable_windows_ansi())


if __name__ == "__main__":
    unittest.main()
