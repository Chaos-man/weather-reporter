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

Standard library only - no pip packages required.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from src import __version__
from src.config import (APP, CRON_DIR, DEFAULTS, LOG_DIR, LOGROTATE_DIR, SCRIPT, SCRIPT_DIR,
                        SetupError, parse_env_file, parse_int, parse_run_time, render_config,
                        valid_webhook)
from src.report import HttpError, build_payload, http_get_json, run_report, user_agent_for

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"


# --------------------------------------------------------------------------- #
# Setup (default when no mode flag is given)
# --------------------------------------------------------------------------- #
def instance_name(repo_dir: Path) -> str:
    """Folder name -> cron-safe instance name (cron.d only runs [A-Za-z0-9_-] files)."""
    name = re.sub(r"[^a-z0-9_-]", "-", repo_dir.name.lower()).strip("-")
    if not name:
        raise SetupError(f"can't build an instance name from folder '{repo_dir}'")
    return name if name.startswith(APP) else f"{APP}-{name}"

def write_file(path: Path, content: str, mode: int) -> None:
    """Write via a temp file + rename so a failed write never leaves half a file."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(content)
    os.chmod(tmp, mode)
    if os.geteuid() == 0:
        os.chown(tmp, 0, 0)
    os.replace(tmp, path)


MANAGED_RE = re.compile(r"^# Managed by (.+)/(?:setup\.sh|weather_report\.py) - ", re.MULTILINE)


def cron_owner(cron_file: Path):
    """Folder that wrote this cron file, or None if it isn't one of ours."""
    match = MANAGED_RE.search(cron_file.read_text()) if cron_file.exists() else None
    return Path(match.group(1)) if match else None


def render_cron(repo_dir: Path, script: Path, python: str, hour: int, minute: int, log_file: Path) -> str:
    return (
        f"# Managed by {repo_dir}/weather_report.py - re-run setup to change this file.\n"
        "SHELL=/bin/sh\n"
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
        f'{minute} {hour} * * * root "{python}" "{script}" --run >> "{log_file}" 2>&1\n'
    )


def render_logrotate(log_file: Path) -> str:
    return (
        f'"{log_file}" {{\n'
        "    monthly\n"
        "    rotate 6\n"
        "    compress\n"
        "    missingok\n"
        "    notifempty\n"
        "}\n"
    )


def install(values: dict, repo_dir: Path, script: Path, python: str,
            cron_dir: Path = CRON_DIR, logrotate_dir: Path = LOGROTATE_DIR,
            log_dir: Path = LOG_DIR) -> dict:
    """Write config.env, the cron job, and log rotation for this install.

    Only touches files; package and service handling is separate so this can be
    tested against temporary folders.
    """
    instance = instance_name(repo_dir)
    cron_file = cron_dir / instance
    log_file = log_dir / f"{instance}.log"
    for path in (repo_dir, script, log_file):
        if "%" in str(path) or "\n" in str(path) or '"' in str(path):
            raise SetupError(f"path '{path}' contains a character cron can't handle (%, quote, or newline)")

    owner = cron_owner(cron_file)
    if cron_file.exists() and owner != repo_dir:
        raise SetupError(
            f"{cron_file} already belongs to {owner or 'something else'}. "
            "Rename this folder and re-run setup."
        )

    hour, minute = parse_run_time(values["RUN_TIME"])
    write_file(repo_dir / "config.env", render_config(values), 0o600)

    log_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(log_dir, 0o755)
    if os.geteuid() == 0:
        os.chown(log_dir, 0, 0)  # older versions made this owned by a service user
    write_file(cron_file, render_cron(repo_dir, script, python, hour, minute, log_file), 0o644)

    logrotate_file = None
    if logrotate_dir.is_dir():
        logrotate_file = logrotate_dir / instance
        write_file(logrotate_file, render_logrotate(log_file), 0o644)

    return {"instance": instance, "cron_file": cron_file, "log_file": log_file,
            "logrotate_file": logrotate_file, "hour": hour, "minute": minute}


US_STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA",
    "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE", "District of Columbia": "DC",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL",
    "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA",
    "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR",
    "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD",
    "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA",
    "Washington": "WA", "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
    "Puerto Rico": "PR", "Guam": "GU", "U.S. Virgin Islands": "VI", "American Samoa": "AS",
    "Northern Mariana Islands": "MP",
}


def geocode(query: str, user_agent: str, fetch=None) -> list:
    """'Evans, GA' -> US matches from Open-Meteo (GeoNames data), best match first."""
    fetch = fetch or http_get_json
    params = urllib.parse.urlencode({
        "name": query.strip(), "count": 10, "language": "en", "countryCode": "US", "format": "json",
    })
    data = fetch(f"{GEOCODE_URL}?{params}", user_agent, accept="application/json")
    places = []
    for r in data.get("results") or []:
        if r.get("country_code") != "US" or "latitude" not in r:
            continue
        state = r.get("admin1", "")
        abbr = US_STATES.get(state, state)
        places.append({
            "name": f"{r['name']}, {abbr}" if abbr else r["name"],
            "detail": ", ".join(x for x in (r.get("admin2"), state) if x),
            "lat": round(float(r["latitude"]), 4),
            "lon": round(float(r["longitude"]), 4),
        })
    return places


def describe_place(place: dict) -> str:
    detail = f" ({place['detail']})" if place["detail"] else ""
    return f"{place['name']}{detail}  {place['lat']:.4f}, {place['lon']:.4f}"


def first_place(places: list) -> dict:
    """Non-interactive choice: the best (first) match."""
    return places[0]


def collect_values(args, existing: dict, lookup=None, choose=first_place) -> dict:
    """Merge flags > existing config.env > defaults, and validate."""
    values = {**DEFAULTS, **existing}

    def pick(flag, key):
        value = getattr(args, flag, None)
        if value is not None:
            values[key] = str(value).strip()

    pick("webhook", "DISCORD_WEBHOOK_URL")
    pick("time", "RUN_TIME")
    pick("contact", "CONTACT")
    pick("rain_threshold", "RAIN_THRESHOLD")
    pick("rain_show_threshold", "RAIN_SHOW_THRESHOLD")
    pick("freeze_threshold", "FREEZE_THRESHOLD_F")

    if (args.lat is None) != (args.lon is None):
        raise SetupError("--lat and --lon must be used together")
    if args.lat is not None:
        values["LATITUDE"], values["LONGITUDE"] = f"{args.lat:.4f}", f"{args.lon:.4f}"
        if args.location:
            values["LOCATION_NAME"] = args.location.strip()
    elif args.location:
        lookup = lookup or (lambda q: geocode(q, user_agent_for(values)))
        try:
            places = lookup(args.location)
        except (RuntimeError, OSError) as err:
            raise SetupError(f"couldn't reach the city lookup service ({err}). "
                             "Check the network, or use --lat and --lon instead.") from None
        if not places:
            raise SetupError(f"no US place found for '{args.location}'. "
                             "Check the spelling, try 'City, ST', or use --lat and --lon.")
        place = choose(places)
        print(f"==> Location: {describe_place(place)}")
        values["LOCATION_NAME"] = place["name"]
        values["LATITUDE"], values["LONGITUDE"] = f"{place['lat']:.4f}", f"{place['lon']:.4f}"

    if not valid_webhook(values.get("DISCORD_WEBHOOK_URL", "")):
        raise SetupError("a Discord webhook URL is required (--webhook URL, or run setup interactively)")
    values["RUN_TIME"] = "{:02d}:{:02d}".format(*parse_run_time(values["RUN_TIME"]))
    parse_int(values["RAIN_THRESHOLD"], "rain threshold", 0, 100)
    parse_int(values["RAIN_SHOW_THRESHOLD"], "rain show threshold", 0, 100)
    parse_int(values["FREEZE_THRESHOLD_F"], "freeze threshold", -60, 80)
    return values


def run_cmd(cmd: list) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"})


def ensure_packages() -> None:
    """Install cron and CA certificates if they're missing (Debian/Ubuntu)."""
    missing = []
    if not shutil.which("cron"):
        missing.append("cron")
    if not Path("/etc/ssl/certs/ca-certificates.crt").exists():
        missing.append("ca-certificates")
    if not missing:
        return
    if not shutil.which("apt-get"):
        raise SetupError(f"please install {' and '.join(missing)}, then re-run setup")
    print(f"==> Installing {' '.join(missing)}")
    run_cmd(["apt-get", "update", "-qq"])
    run_cmd(["apt-get", "install", "-y", "-qq", *missing])


def ensure_cron_running() -> None:
    if Path("/run/systemd/system").is_dir() and shutil.which("systemctl"):
        run_cmd(["systemctl", "enable", "--now", "cron"])
    elif shutil.which("service"):
        run_cmd(["service", "cron", "start"])


def old_user_note(cron_dir: Path = CRON_DIR) -> str:
    """Older versions ran as a 'weather-reporter' user. Suggest removing it once unused."""
    try:
        import pwd
        pwd.getpwnam(APP)
    except (ImportError, KeyError):
        return ""
    for cron_file in cron_dir.glob(f"{APP}*"):
        if re.search(rf"^\S+ \S+ \S+ \S+ \S+ {APP} ", cron_file.read_text(), re.MULTILINE):
            return ""  # another install still uses it
    return f"The '{APP}' user from older versions is no longer used. Remove it with: userdel {APP}"


def check_forecast(values: dict, script: Path, build=None) -> None:
    """Fetch the forecast once before saving. NWS 404 = location not covered (fatal);
    network problems only warn, so setup still works while offline."""
    build = build or build_payload
    print("==> Checking the forecast from NWS (nothing is posted)...")
    try:
        payload = build({**DEFAULTS, **values}, datetime.now().astimezone())
        print(f"    OK: {payload['embeds'][0]['title']}")
    except HttpError as err:
        if err.code == 404:
            raise SetupError(f"NWS has no forecast for {values['LATITUDE']}, {values['LONGITUDE']} "
                             "(NWS covers the US and its territories)") from None
        print(f"WARN: forecast check failed: {err}")
    except Exception as err:
        print(f"WARN: forecast check failed: {err}")
        print(f"      Setup will continue. Re-check later with: {script} --dry-run")


# --- Interactive questions ---------------------------------------------------
def mask_webhook(url: str) -> str:
    """Show enough of a webhook to recognize it without exposing the token."""
    match = re.match(r"^(https://\S+/api/webhooks/\d+/)(\S+)$", url or "")
    if not match:
        return url or ""
    return f"{match.group(1)}****{match.group(2)[-4:]}"


def ask(question: str, default: str = "", ask_fn=input) -> str:
    suffix = f" [{default}]" if default else ""
    return ask_fn(f"{question}{suffix}: ").strip()


def ask_yes_no(question: str, default: bool = True, ask_fn=input) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        answer = ask_fn(f"{question} [{hint}]: ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  Please answer y or n.")


def choose_place_interactive(places: list, ask_fn=input):
    """Return the chosen place, or None to search again."""
    if len(places) == 1:
        return places[0] if ask_yes_no(f"  Found {describe_place(places[0])}. Use it?", ask_fn=ask_fn) else None
    print("  Matches:")
    for i, place in enumerate(places, 1):
        print(f"    {i}) {describe_place(place)}")
    while True:
        answer = ask_fn("  Pick a number, or 0 to search again [1]: ").strip() or "1"
        if answer.isdigit() and 0 <= int(answer) <= len(places):
            return places[int(answer) - 1] if answer != "0" else None
        print(f"  Enter a number from 0 to {len(places)}.")


def ask_setup_questions(args, existing: dict, ask_fn=input, lookup=None):
    """Fill in the basic settings the flags didn't give. Returns updated args."""
    current = {**DEFAULTS, **existing}
    print("weather-reporter setup. Press Enter to keep the value in [brackets].\n")

    if args.webhook is None:
        have = current.get("DISCORD_WEBHOOK_URL", "")
        while True:
            answer = ask("Discord webhook URL", mask_webhook(have) if valid_webhook(have) else "", ask_fn)
            if not answer and valid_webhook(have):
                break
            if valid_webhook(answer):
                args.webhook = answer
                break
            print("  That doesn't look like a Discord webhook URL "
                  "(https://discord.com/api/webhooks/<id>/<token>).")

    if args.location is None and args.lat is None:
        lookup = lookup or (lambda q: geocode(q, user_agent_for(current)))
        while True:
            answer = ask("Location (City, ST)", current["LOCATION_NAME"], ask_fn)
            if not answer:
                break  # keep the current coordinates
            try:
                places = lookup(answer)
            except (RuntimeError, OSError) as err:
                print(f"  Couldn't reach the city lookup service: {err}")
                continue
            if not places:
                print(f"  No US place found for '{answer}'. Try 'City, ST'.")
                continue
            place = choose_place_interactive(places, ask_fn)
            if place:
                args.location, args.lat, args.lon = place["name"], place["lat"], place["lon"]
                break

    if args.time is None:
        tz = datetime.now().astimezone().strftime("%Z")
        while True:
            answer = ask(f"Daily run time, 24-hour HH:MM ({tz})", current["RUN_TIME"], ask_fn)
            try:
                if answer:
                    parse_run_time(answer)
                    args.time = answer
                break
            except SetupError as err:
                print(f"  {err}")

    if args.contact is None:
        answer = ask("Contact email for the NWS API (optional)", current.get("CONTACT", ""), ask_fn)
        if answer:
            args.contact = answer
    return args


def print_summary(values: dict) -> None:
    print("\nSettings:")
    rows = [
        ("Webhook", mask_webhook(values["DISCORD_WEBHOOK_URL"])),
        ("Location", f"{values['LOCATION_NAME']}  ({values['LATITUDE']}, {values['LONGITUDE']})"),
        ("Run time", values["RUN_TIME"]),
        ("Contact", values.get("CONTACT") or "(none)"),
        ("Thresholds", f"rain tomorrow {values['RAIN_THRESHOLD']}%, rain line {values['RAIN_SHOW_THRESHOLD']}%, "
                       f"freeze {values['FREEZE_THRESHOLD_F']}F"),
    ]
    for label, value in rows:
        print(f"  {label:<11}{value}")
    print()


# --- Setup entry point --------------------------------------------------------
def run_setup(args) -> int:
    if os.geteuid() != 0:
        raise SetupError("setup needs root (it writes to /etc/cron.d)")
    config_path = SCRIPT_DIR / "config.env"
    existing = parse_env_file(config_path)
    interactive = not args.yes
    if interactive:
        args = ask_setup_questions(args, existing)
    values = collect_values(args, existing)

    if interactive:
        print_summary(values)
        if not ask_yes_no("Save and install?"):
            print("Nothing changed.")
            return 0

    script = SCRIPT
    check_forecast(values, script)

    ensure_packages()
    result = install(values, SCRIPT_DIR, script, sys.executable)
    ensure_cron_running()

    print(f"==> Saved {config_path}")
    print(f"==> Cron job {result['cron_file']}: daily at {values['RUN_TIME']} "
          f"({datetime.now().astimezone().strftime('%Z')} system time)")

    note = old_user_note()
    if note:
        print(f"NOTE: {note}")
    print(f"\nSetup complete. Log: {result['log_file']}")

    try:
        send_test = interactive and ask_yes_no("Send a test message to Discord now?")
    except (EOFError, KeyboardInterrupt):
        send_test = False
        print()
    if send_test:
        try:
            run_report("test")
        except Exception as err:
            print(f"WARN: test message failed: {err}")
    else:
        print(f"Send a test message: {script} --test")
    return 0


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
