"""Settings: paths, defaults, reading and writing config.env, and validating values."""

import re
import sys
from pathlib import Path

# The repo folder (one level above src/), where weather_report.py and config.env live.
SCRIPT_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPT_DIR / "weather_report.py"
CONFIG_PATH = SCRIPT_DIR / "config.env"

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


def load_config(path: Path) -> dict:
    """Defaults overlaid with config.env. Exits if the file is missing."""
    if not path.exists():
        sys.exit(f"ERROR: config file not found: {path} (run setup: {SCRIPT})")
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
