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
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.env"
NWS_BASE = "https://api.weather.gov"

# Embed sidebar colors (priority: alert > freeze > rain > clear)
COLOR_ALERT = 0xE74C3C   # red
COLOR_FREEZE = 0x3498DB  # blue
COLOR_RAIN = 0xF1C40F    # yellow
COLOR_CLEAR = 0x2ECC71   # green

DEFAULTS = {
    "LOCATION_NAME": "New York, NY",
    "LATITUDE": "40.7128",
    "LONGITUDE": "-74.0060",
    "RAIN_THRESHOLD": "30",
    "RAIN_SHOW_THRESHOLD": "10",
    "FREEZE_THRESHOLD_F": "32",
    "CONTACT": "",
}


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config(path: Path) -> dict:
    """Parse a simple KEY=VALUE env file (bash-compatible, quotes optional)."""
    cfg = dict(DEFAULTS)
    if not path.exists():
        sys.exit(f"ERROR: config file not found: {path} (copy config.env.example to config.env)")
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        cfg[key] = value
    return cfg


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def http_get_json(url: str, user_agent: str, retries: int = 3) -> dict:
    """GET JSON with simple retry; the NWS API returns intermittent 5xx errors."""
    headers = {"User-Agent": user_agent, "Accept": "application/geo+json"}
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as err:
            last_err = err
            if attempt < retries:
                time.sleep(5 * attempt)
    raise RuntimeError(f"GET {url} failed after {retries} attempts: {last_err}")


def post_discord(webhook_url: str, payload: dict, user_agent: str) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": user_agent},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status not in (200, 204):
                raise RuntimeError(f"Discord returned HTTP {resp.status}")
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Discord returned HTTP {err.code}: {body}") from err


# --------------------------------------------------------------------------- #
# Forecast logic
# --------------------------------------------------------------------------- #
def period_date(period: dict, tz):
    return datetime.fromisoformat(period["startTime"]).astimezone(tz).date()


def pop(period) -> int:
    """Probability of precipitation as an int (NWS returns null for 0%)."""
    if not period:
        return 0
    value = (period.get("probabilityOfPrecipitation") or {}).get("value")
    return int(value) if value is not None else 0


def condition_emoji(short_forecast: str, is_day: bool = True) -> str:
    s = (short_forecast or "").lower()
    if "thunder" in s:
        return "⛈️"
    if any(w in s for w in ("snow", "sleet", "ice", "freezing")):
        return "🌨️"
    if any(w in s for w in ("rain", "shower", "drizzle")):
        return "🌧️"
    if "fog" in s:
        return "🌫️"
    if "partly" in s or "mostly sunny" in s or "mostly clear" in s:
        return "⛅" if is_day else "☁️"
    if "cloudy" in s or "overcast" in s:
        return "☁️"
    if "sunny" in s or "clear" in s:
        return "☀️" if is_day else "🌙"
    return "🌡️"


def analyze(periods: list, alerts: list, cfg: dict, now: datetime) -> dict:
    """Pull the values the report needs out of the NWS forecast periods."""
    tz = now.tzinfo
    today = now.date()
    tomorrow = today + timedelta(days=1)

    today_day = next((p for p in periods if p["isDaytime"] and period_date(p, tz) == today), None)
    tonight = next((p for p in periods if not p["isDaytime"] and period_date(p, tz) == today), None)
    tomorrow_periods = [p for p in periods if period_date(p, tz) == tomorrow]

    rain_threshold = int(cfg["RAIN_THRESHOLD"])
    freeze_threshold = int(cfg["FREEZE_THRESHOLD_F"])

    tomorrow_rain = max((pop(p) for p in tomorrow_periods), default=0)
    tomorrow_rain_period = max(tomorrow_periods, key=pop, default=None)
    tonight_low = tonight["temperature"] if tonight else None

    return {
        "today_day": today_day,
        "tonight": tonight,
        "high": today_day["temperature"] if today_day else None,
        "low": tonight_low,
        "rain_today": pop(today_day),
        "rain_tonight": pop(tonight),
        "tomorrow_rain": tomorrow_rain,
        "tomorrow_rain_period": tomorrow_rain_period,
        "rain_warning": tomorrow_rain >= rain_threshold,
        "freeze_warning": tonight_low is not None and tonight_low <= freeze_threshold,
        "alerts": alerts,
    }


def format_alert(props: dict, tz) -> str:
    event = props.get("event", "Weather Alert")
    ends = props.get("ends") or props.get("expires")
    if ends:
        try:
            end_dt = datetime.fromisoformat(ends).astimezone(tz)
            return f"🚨 **{event}** until {end_dt.strftime('%a %-I:%M %p')}"
        except ValueError:
            pass
    return f"🚨 **{event}**"


def build_embed(data: dict, cfg: dict, now: datetime, test: bool = False) -> dict:
    tz = now.tzinfo
    day = data["today_day"]
    icon = condition_emoji(day["shortForecast"]) if day else "🌡️"
    high = f"{data['high']}°F" if data["high"] is not None else "—"
    low = f"{data['low']}°F" if data["low"] is not None else "—"

    lines = [f"🌡️ **High** {high}  ·  **Low** {low}"]
    # Only show today's rain line when today or tonight reaches RAIN_SHOW_THRESHOLD
    show_rain = int(cfg["RAIN_SHOW_THRESHOLD"])
    if max(data["rain_today"], data["rain_tonight"]) >= show_rain:
        lines.append(f"🌧️ **Rain** {data['rain_today']}% today  ·  {data['rain_tonight']}% tonight")
    if day:
        lines.append(f"{icon} {day['shortForecast']}")

    warnings = []
    for alert in data["alerts"]:
        warnings.append(format_alert(alert.get("properties", {}), tz))
    if data["freeze_warning"]:
        warnings.append(f"🥶 **Freeze tonight** — low of {data['low']}°F")
    if data["rain_warning"]:
        period = data["tomorrow_rain_period"]
        detail = f" ({period['shortForecast']})" if period else ""
        warnings.append(f"☔ **Rain tomorrow** — {data['tomorrow_rain']}% chance{detail}")

    if data["alerts"]:
        color = COLOR_ALERT
    elif data["freeze_warning"]:
        color = COLOR_FREEZE
    elif data["rain_warning"]:
        color = COLOR_RAIN
    else:
        color = COLOR_CLEAR

    if warnings:
        lines.append("")
        lines.append("**⚠️ Heads up**")
        lines.extend(warnings)
    else:
        lines.append("")
        lines.append("✅ No warnings")

    title = f"{icon} {cfg['LOCATION_NAME']} — {now.strftime('%a, %b %-d')}"
    if test:
        title = "[TEST] " + title

    return {
        "title": title,
        "description": "\n".join(lines),
        "color": color,
        "footer": {"text": f"National Weather Service · {now.strftime('%-I:%M %p %Z')}"},
    }


# --------------------------------------------------------------------------- #
# Report (--run / --dry-run / --test)
# --------------------------------------------------------------------------- #
def run_report(mode: str) -> int:
    cfg = load_config(CONFIG_PATH)
    # Use the system's local time zone (whatever the container is set to).
    now = datetime.now().astimezone()

    contact = cfg.get("CONTACT") or "no-contact-set"
    user_agent = f"weather-reporter/1.0 ({contact})"

    lat = f"{float(cfg['LATITUDE']):.4f}"
    lon = f"{float(cfg['LONGITUDE']):.4f}"

    points = http_get_json(f"{NWS_BASE}/points/{lat},{lon}", user_agent)
    forecast = http_get_json(points["properties"]["forecast"], user_agent)
    alerts = http_get_json(f"{NWS_BASE}/alerts/active?point={lat},{lon}", user_agent)

    data = analyze(forecast["properties"]["periods"], alerts.get("features", []), cfg, now)
    payload = {"embeds": [build_embed(data, cfg, now, test=(mode == "test"))]}

    if mode == "dry-run":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    webhook = cfg.get("DISCORD_WEBHOOK_URL", "")
    if not webhook.startswith("https://discord.com/api/webhooks/"):
        sys.exit("ERROR: DISCORD_WEBHOOK_URL is missing or invalid in config.env")

    post_discord(webhook, payload, user_agent)
    print(f"{now.isoformat()} posted report (color={payload['embeds'][0]['color']:#08x})")
    return 0


# --------------------------------------------------------------------------- #
# Setup (default when no mode flag is given)
# --------------------------------------------------------------------------- #
def run_setup(args) -> int:
    print("Setup is not implemented yet.")
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
    modes = parser.add_argument_group("modes (pick one; default is setup)")
    exclusive = modes.add_mutually_exclusive_group()
    exclusive.add_argument("--run", action="store_true", help="fetch the forecast and post to Discord (used by cron)")
    exclusive.add_argument("--dry-run", action="store_true", help="fetch the forecast and print the payload; do not post")
    exclusive.add_argument("--test", action="store_true", help="post with a [TEST] tag in the title")

    setup = parser.add_argument_group("setup options")
    setup.add_argument("--yes", "-y", action="store_true",
                       help="don't ask questions; use flags, then existing config, then defaults")
    return parser


SETUP_OPTIONS = ("yes",)


def select_mode(args, stdin_is_tty: bool) -> str:
    """Decide what to do: 'run', 'dry-run', 'test', or 'setup'."""
    for mode in ("run", "dry_run", "test"):
        if getattr(args, mode):
            used = [f"--{o.replace('_', '-')}" for o in SETUP_OPTIONS if getattr(args, o)]
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
        return run_setup(args)
    return run_report(mode)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as err:  # log a single line for cron instead of a traceback
        print(f"{datetime.now().isoformat()} ERROR: {err}", file=sys.stderr)
        sys.exit(1)
