"""Tests for command-line mode selection."""

import contextlib
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import weather_report as wr  # noqa: E402


def mode(argv, tty=True):
    args = wr.build_parser().parse_args(argv)
    return wr.select_mode(args, stdin_is_tty=tty)


class TestModeSelection(unittest.TestCase):
    def test_report_modes(self):
        self.assertEqual(mode(["--run"]), "run")
        self.assertEqual(mode(["--dry-run"]), "dry-run")
        self.assertEqual(mode(["--test"]), "test")

    def test_report_modes_work_without_terminal(self):
        # cron has no terminal
        self.assertEqual(mode(["--run"], tty=False), "run")

    def test_no_flags_is_setup(self):
        self.assertEqual(mode([]), "setup")

    def test_yes_is_setup_without_terminal(self):
        self.assertEqual(mode(["--yes"], tty=False), "setup")
        self.assertEqual(mode(["-y"], tty=False), "setup")

    def test_interactive_setup_needs_terminal(self):
        # e.g. an old cron line that runs the script with no flags
        with self.assertRaises(wr.UsageError) as ctx:
            mode([], tty=False)
        self.assertIn("--run", str(ctx.exception))

    def test_setup_options_rejected_with_report_modes(self):
        with self.assertRaises(wr.UsageError):
            mode(["--run", "--yes"])

    def test_version(self):
        self.assertRegex(wr.__version__, r"^\d+\.\d+$")  # matches release tags like v1.0
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            wr.build_parser().parse_args(["--version"])
        self.assertEqual(out.getvalue().strip(), f"weather-reporter {wr.__version__}")

    def test_report_modes_are_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            wr.build_parser().parse_args(["--run", "--test"])


if __name__ == "__main__":
    unittest.main()
