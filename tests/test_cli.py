import importlib.machinery
import importlib.util
import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

if importlib.util.find_spec("mutagen") is None or importlib.util.find_spec("PIL") is None:
    raise unittest.SkipTest("mutagen and Pillow are needed to run these tests (./setup.sh)")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["MUSIC_TOOLS_NO_VENV"] = "1"   # the launcher would otherwise re-run itself under .venv

_loader = importlib.machinery.SourceFileLoader("launcher", str(ROOT / "music-tools"))
launcher = importlib.util.module_from_spec(importlib.util.spec_from_loader("launcher", _loader))
_loader.exec_module(launcher)


class CliTests(unittest.TestCase):
    def run_cli(self, *argv):
        calls = []
        with mock.patch.object(launcher, "run_tool", side_effect=lambda n, a: calls.append((n, a)) or 0), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            code = launcher.main(list(argv))
        return code, calls, err.getvalue()

    def test_easy_names_run_the_original_tools(self):
        for easy, tool in (("auto", "all"), ("covers", "art"), ("pics", "artists"), ("fix", "tags"),
                           ("dupes", "duplicates"), ("check", "stats"), ("lyrics", "lyrics"), ("art", "art")):
            _, calls, _ = self.run_cli(easy)
            self.assertEqual([n for n, _ in calls], [tool], easy)

    def test_tidy_and_undo_are_layout_with_the_long_options(self):
        _, calls, _ = self.run_cli("tidy")
        self.assertEqual(calls, [("layout", ["--clean", "--merge-artists", "--merge-albums", "--apply"])])
        _, calls, _ = self.run_cli("tidy", "--dry-run")
        self.assertEqual(calls, [("layout", ["--clean", "--merge-artists", "--merge-albums"])])
        _, calls, _ = self.run_cli("tidy", "--yes")
        self.assertEqual(calls[0][1][-2:], ["--apply", "--yes"])
        _, calls, _ = self.run_cli("undo")
        self.assertEqual(calls, [("layout", ["--undo", "--apply"])])
        _, calls, _ = self.run_cli("undo", "-n")
        self.assertEqual(calls, [("layout", ["--undo"])])

    def test_flag_slips_are_forgiven(self):
        for slip in ("--dryrun", "--dry_run", "--Dry-Run", "-n"):
            _, calls, _ = self.run_cli("auto", slip)
            self.assertEqual(calls, [("all", ["--dry-run"])], slip)
        _, calls, _ = self.run_cli("auto", "--no-art", "--yes")
        self.assertEqual(calls[0][1], ["--no-art", "--yes"])          # right flags are left alone
        _, calls, _ = self.run_cli("auto", "--dry")                    # argparse's own abbreviation
        self.assertEqual(calls[0][1], ["--dry"])
        _, calls, _ = self.run_cli("auto", "--banana")
        self.assertEqual(calls[0][1], ["--banana"])                   # not guessed: the tool reports it
        _, calls, _ = self.run_cli("auto", "--config=x.toml", "--dryrun")
        self.assertEqual(calls[0][1], ["--config=x.toml", "--dry-run"])

    def test_values_after_a_double_dash_are_not_touched(self):
        _, calls, _ = self.run_cli("auto", "--", "--dryrun")
        self.assertEqual(calls[0][1], ["--", "--dryrun"])

    def test_an_unknown_command_suggests_the_closest_one(self):
        code, calls, err = self.run_cli("lirycs")
        self.assertEqual((code, calls), (2, []))
        self.assertIn("Did you mean", err)
        self.assertIn("lyrics", err)
        self.assertIn("EVERYDAY COMMANDS", err)

    def test_help_works_for_easy_names(self):
        _, calls, _ = self.run_cli("help", "covers")
        self.assertEqual(calls, [("art", ["--help"])])
        _, calls, _ = self.run_cli("help", "tidy")
        self.assertEqual(calls, [("layout", ["--help"])])


if __name__ == "__main__":
    unittest.main()
