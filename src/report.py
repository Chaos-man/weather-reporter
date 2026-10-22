"""Fetch the NWS forecast and alerts, and build and post the Discord message."""

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from src import __version__
from src.config import CONFIG_PATH, load_config, valid_webhook

NWS_BASE = "https://api.weather.gov"

# Embed sidebar colors (priority: alert > freeze > rain > clear)
COLOR_ALERT = 0xE74C3C   # red
COLOR_FREEZE = 0x3498DB  # blue
COLOR_RAIN = 0xF1C40F    # yellow
COLOR_CLEAR = 0x2ECC71   # green


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
class HttpError(RuntimeError):
    """An HTTP 4xx response (not retried)."""

    def __init__(self, url: str, code: int):
        super().__init__(f"GET {url} returned HTTP {code}")
        self.code = code


def http_get_json(url: str, user_agent: str, retries: int = 3,
                  accept: str = "application/geo+json") -> dict:
    """GET JSON. Retries network errors and 5xx (NWS has intermittent 5xx); 4xx fails at once."""
    headers = {"User-Agent": user_agent, "Accept": accept}
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as err:
            if 400 <= err.code < 500:
                raise HttpError(url, err.code) from None
            last_err = err
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
    return f"weather-reporter/{__version__} ({cfg.get('CONTACT') or 'no-contact-set'})"


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
