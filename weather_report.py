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
import os
import re
import shutil
import subprocess
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

APP = "weather-reporter"
CRON_DIR = Path("/etc/cron.d")
LOGROTATE_DIR = Path("/etc/logrotate.d")
LOG_DIR = Path("/var/log") / APP

WEBHOOK_RE = re.compile(r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$")
RUN_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

DEFAULTS = {
    "RUN_TIME": "08:00",
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
    """Defaults overlaid with config.env. Exits if the file is missing."""
    if not path.exists():
        sys.exit(f"ERROR: config file not found: {path} (run setup: {SCRIPT_DIR / 'weather_report.py'})")
    return {**DEFAULTS, **parse_env_file(path)}


def parse_env_file(path: Path) -> dict:
    """Parse a simple KEY=VALUE env file (quotes optional, # comments on their own line)."""
    cfg = {}
    if not path.exists():
        return cfg
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
def user_agent_for(cfg: dict) -> str:
    return f"weather-reporter/1.0 ({cfg.get('CONTACT') or 'no-contact-set'})"


def build_payload(cfg: dict, now: datetime, test: bool = False) -> dict:
    """Fetch the forecast and alerts from NWS and build the Discord payload."""
    user_agent = user_agent_for(cfg)
    lat = f"{float(cfg['LATITUDE']):.4f}"
    lon = f"{float(cfg['LONGITUDE']):.4f}"

    points = http_get_json(f"{NWS_BASE}/points/{lat},{lon}", user_agent)
    forecast = http_get_json(points["properties"]["forecast"], user_agent)
    alerts = http_get_json(f"{NWS_BASE}/alerts/active?point={lat},{lon}", user_agent)

    data = analyze(forecast["properties"]["periods"], alerts.get("features", []), cfg, now)
    return {"embeds": [build_embed(data, cfg, now, test=test)]}


def run_report(mode: str) -> int:
    cfg = load_config(CONFIG_PATH)
    # Use the system's local time zone (whatever the container is set to).
    now = datetime.now().astimezone()
    payload = build_payload(cfg, now, test=(mode == "test"))
    user_agent = user_agent_for(cfg)

    if mode == "dry-run":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    webhook = cfg.get("DISCORD_WEBHOOK_URL", "")
    if not valid_webhook(webhook):
        sys.exit("ERROR: DISCORD_WEBHOOK_URL is missing or invalid in config.env")

    post_discord(webhook, payload, user_agent)
    print(f"{now.isoformat()} posted report (color={payload['embeds'][0]['color']:#08x})")
    return 0


# --------------------------------------------------------------------------- #
# Setup (default when no mode flag is given)
# --------------------------------------------------------------------------- #
class SetupError(Exception):
    """Setup can't continue; the message says why."""


def valid_webhook(url: str) -> bool:
    return bool(WEBHOOK_RE.match(url or ""))


def parse_run_time(value: str):
    """'7:30' -> (7, 30). Raises SetupError for anything that isn't 24-hour HH:MM."""
    match = RUN_TIME_RE.match((value or "").strip())
    if not match:
        raise SetupError(f"run time '{value}' is not a valid 24-hour HH:MM time")
    return int(match.group(1)), int(match.group(2))


def parse_int(value, name: str, low: int, high: int) -> int:
    try:
        number = int(str(value).strip())
    except ValueError:
        raise SetupError(f"{name} must be a whole number, got '{value}'") from None
    if not low <= number <= high:
        raise SetupError(f"{name} must be between {low} and {high}, got {number}")
    return number


def instance_name(repo_dir: Path) -> str:
    """Folder name -> cron-safe instance name (cron.d only runs [A-Za-z0-9_-] files)."""
    name = re.sub(r"[^a-z0-9_-]", "-", repo_dir.name.lower()).strip("-")
    if not name:
        raise SetupError(f"can't build an instance name from folder '{repo_dir}'")
    return name if name.startswith(APP) else f"{APP}-{name}"


# config.env layout: (key, comment lines written above it)
CONFIG_LAYOUT = [
    ("DISCORD_WEBHOOK_URL", ["Discord webhook (Channel Settings > Integrations > Webhooks)"]),
    ("LOCATION_NAME", ["Location (set by setup from a City, ST lookup)"]),
    ("LATITUDE", []),
    ("LONGITUDE", []),
    ("RUN_TIME", ["Daily run time (24-hour HH:MM) in the container's system time zone"]),
    ("RAIN_THRESHOLD", ["Warn if any period tomorrow has >= this % chance of rain"]),
    ("RAIN_SHOW_THRESHOLD", ["Show the rain line only if today or tonight has >= this % chance of rain"]),
    ("FREEZE_THRESHOLD_F", ["Warn if tonight's low is <= this temperature (F)"]),
    ("CONTACT", ["Contact info for the NWS API User-Agent header (email or URL)"]),
]


def render_config(values: dict) -> str:
    lines = [
        "# weather-reporter configuration - written by setup.",
        "# Edit by hand or re-run setup. Thresholds take effect on the next run;",
        "# re-run setup after changing RUN_TIME.",
    ]
    known = {key for key, _ in CONFIG_LAYOUT}
    for key, comments in CONFIG_LAYOUT:
        if comments:
            lines.append("")
            lines.extend(f"# {c}" for c in comments)
        lines.append(f'{key}="{values.get(key, "")}"')
    extra = {k: v for k, v in values.items() if k not in known}
    if extra:
        lines += ["", "# Other settings (kept from the previous config)"]
        lines += [f'{k}="{v}"' for k, v in extra.items()]
    return "\n".join(lines) + "\n"


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


def collect_values(args, existing: dict) -> dict:
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
        raise SetupError("city lookup isn't available yet; pass --lat and --lon with --location")

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


def run_setup(args) -> int:
    if os.geteuid() != 0:
        raise SetupError("setup needs root (it writes to /etc/cron.d)")
    config_path = SCRIPT_DIR / "config.env"
    existing = parse_env_file(config_path)
    values = collect_values(args, existing)

    ensure_packages()
    script = Path(__file__).resolve()
    result = install(values, SCRIPT_DIR, script, sys.executable)
    ensure_cron_running()

    print(f"==> Saved {config_path}")
    print(f"==> Cron job {result['cron_file']}: daily at {values['RUN_TIME']} "
          f"({datetime.now().astimezone().strftime('%Z')} system time)")

    print("==> Checking the forecast from NWS (nothing is posted)...")
    try:
        payload = build_payload({**DEFAULTS, **values}, datetime.now().astimezone())
        print(f"    OK: {payload['embeds'][0]['title']}")
    except Exception as err:
        print(f"WARN: forecast check failed: {err}")
        print(f"      Re-check with: {script} --dry-run")

    note = old_user_note()
    if note:
        print(f"NOTE: {note}")
    print(f"\nSetup complete. Log: {result['log_file']}")
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
    return run_report(mode)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as err:  # log a single line for cron instead of a traceback
        print(f"{datetime.now().isoformat()} ERROR: {err}", file=sys.stderr)
        sys.exit(1)
