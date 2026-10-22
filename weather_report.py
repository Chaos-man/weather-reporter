#!/usr/bin/env python3
"""
weather-reporter: daily NWS forecast summary posted to a Discord webhook.

Reads settings from config.env (next to this script), pulls the forecast and
active alerts from the National Weather Service API, and posts a short,
color-coded Discord embed.

Usage:
  weather_report.py              Set up or reconfigure this install (interactive)
  weather_report.py --run        Fetch the forecast and post to Discord (used by cron)
  weather_report.py --dry-run    Fetch the forecast and print the payload; do not post
  weather_report.py --test       Post to Discord with a [TEST] tag in the title

The code lives in src/; this file is the command line.
Standard library only - no pip packages required.
"""

import argparse
import sys
from datetime import datetime

from src import __version__
from src.config import SetupError
from src.report import run_report
from src.wizard import run_setup


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
class UsageError(Exception):
    """Bad combination of command-line options."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Daily NWS weather report for Discord. "
                    "With no mode flag, runs setup for this install.",
    )
    parser.add_argument("--version", action="version", version=f"weather-reporter {__version__}")
    modes = parser.add_argument_group("modes (pick one; default is setup)")
    exclusive = modes.add_mutually_exclusive_group()
    exclusive.add_argument("--run", action="store_true", help="fetch the forecast and post to Discord (used by cron)")
    exclusive.add_argument("--dry-run", action="store_true", help="fetch the forecast and print the payload; do not post")
    exclusive.add_argument("--test", action="store_true", help="post with a [TEST] tag in the title")

    setup = parser.add_argument_group("setup options")
    setup.add_argument("--yes", "-y", action="store_true",
                       help="don't ask questions; use flags, then existing config, then defaults")
    setup.add_argument("--webhook", metavar="URL", help="Discord webhook URL (note: saved in shell history)")
    setup.add_argument("--location", metavar='"CITY, ST"', help='location, e.g. "Evans, GA"')
    setup.add_argument("--lat", type=float, help="latitude (skips the city lookup)")
    setup.add_argument("--lon", type=float, help="longitude (skips the city lookup)")
    setup.add_argument("--time", metavar="HH:MM", help="daily run time, 24-hour, system time zone")
    setup.add_argument("--contact", metavar="EMAIL", help="contact email/URL sent to the NWS API")
    setup.add_argument("--rain-threshold", type=int, metavar="PCT", help="rain-tomorrow warning threshold (default 30)")
    setup.add_argument("--rain-show-threshold", type=int, metavar="PCT", help="minimum chance to show the rain line (default 10)")
    setup.add_argument("--freeze-threshold", type=int, metavar="F", help="freeze warning threshold in F (default 32)")
    return parser


SETUP_OPTIONS = ("yes", "webhook", "location", "lat", "lon", "time", "contact",
                 "rain_threshold", "rain_show_threshold", "freeze_threshold")


def select_mode(args, stdin_is_tty: bool) -> str:
    """Decide what to do: 'run', 'dry-run', 'test', or 'setup'."""
    for mode in ("run", "dry_run", "test"):
        if getattr(args, mode):
            used = [f"--{o.replace('_', '-')}" for o in SETUP_OPTIONS
                    if getattr(args, o) not in (None, False)]
            if used:
                raise UsageError(f"{', '.join(used)} can only be used with setup, not --{mode.replace('_', '-')}")
            return mode.replace("_", "-")
    if not args.yes and not stdin_is_tty:
        raise UsageError(
            "setup needs a terminal to ask questions. "
            "Use --yes for non-interactive setup, or --run to post the report "
            "(cron jobs created by older versions need re-running setup)."
        )
    return "setup"


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        mode = select_mode(args, sys.stdin.isatty())
    except UsageError as err:
        parser.error(str(err))
    if mode == "setup":
        try:
            return run_setup(args)
        except SetupError as err:
            print(f"ERROR: {err}", file=sys.stderr)
            return 1
        except (EOFError, KeyboardInterrupt):
            print("\nSetup cancelled. Nothing was changed.", file=sys.stderr)
            return 1
    return run_report(mode)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as err:  # log a single line for cron instead of a traceback
        print(f"{datetime.now().isoformat()} ERROR: {err}", file=sys.stderr)
        sys.exit(1)
