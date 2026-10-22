"""Tests for config loading and the forecast -> Discord embed logic.

Run from the repo root:  python3 -m unittest discover tests
"""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR))

from src import config, report  # noqa: E402

EDT = timezone(timedelta(hours=-4))
NOW = datetime(2026, 10, 22, 8, 0, tzinfo=EDT)


def period(start, is_day, temp, pop=None, short="Sunny"):
    return {
        "startTime": start,
        "isDaytime": is_day,
        "temperature": temp,
        "probabilityOfPrecipitation": {"value": pop},
        "shortForecast": short,
    }


def forecast(today_pop=None, tonight_pop=None, tonight_low=50,
             tomorrow_pop=None, tomorrow_night_pop=None):
    """Five NWS-style periods starting this morning (2026-10-22)."""
    return [
        period("2026-10-22T08:00:00-04:00", True, 72, today_pop, "Sunny"),
        period("2026-10-22T18:00:00-04:00", False, tonight_low, tonight_pop, "Clear"),
        period("2026-10-23T06:00:00-04:00", True, 70, tomorrow_pop, "Chance Showers"),
        period("2026-10-23T18:00:00-04:00", False, 55, tomorrow_night_pop, "Showers Likely"),
        period("2026-10-24T06:00:00-04:00", True, 68, 90, "Rain"),  # day after tomorrow
    ]


ALERT = {"properties": {"event": "Freeze Warning", "ends": "2026-10-23T09:00:00-04:00"}}


def make_report(periods, alerts=(), cfg=None, now=NOW):
    cfg = cfg or dict(config.DEFAULTS)
    data = report.analyze(periods, list(alerts), cfg, now)
    return data, report.build_embed(data, cfg, now)


class TestConfig(unittest.TestCase):
    def write(self, text):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        tmp.write(text)
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink)
        return Path(tmp.name)

    def test_config_written_by_setup_parses_cleanly(self):
        # Regression: inline comments once broke int() parsing of thresholds.
        cfg = config.load_config(self.write(config.render_config(dict(config.DEFAULTS))))
        for key in ("RAIN_THRESHOLD", "RAIN_SHOW_THRESHOLD", "FREEZE_THRESHOLD_F"):
            int(cfg[key])
        float(cfg["LATITUDE"])
        float(cfg["LONGITUDE"])
        self.assertEqual(cfg["LOCATION_NAME"], "New York, NY")

    def test_quotes_comments_and_export(self):
        cfg = config.load_config(self.write(
            '# comment\n\nLOCATION_NAME="Evans, GA"\nRAIN_THRESHOLD=40\n'
            "export CONTACT='me@example.com'\nnot a setting\n"
        ))
        self.assertEqual(cfg["LOCATION_NAME"], "Evans, GA")
        self.assertEqual(cfg["RAIN_THRESHOLD"], "40")
        self.assertEqual(cfg["CONTACT"], "me@example.com")

    def test_missing_keys_fall_back_to_defaults(self):
        cfg = config.load_config(self.write('DISCORD_WEBHOOK_URL="x"\n'))
        self.assertEqual(cfg["RAIN_SHOW_THRESHOLD"], "10")
        self.assertEqual(cfg["FREEZE_THRESHOLD_F"], "32")

    def test_missing_file_exits(self):
        with self.assertRaises(SystemExit):
            config.load_config(REPO_DIR / "does-not-exist.env")

    def test_paths_point_at_repo_folder(self):
        # Regression guard: the modules live in src/, but config.env and the cron
        # script path must stay next to weather_report.py for existing installs.
        self.assertEqual(config.SCRIPT_DIR, REPO_DIR)
        self.assertEqual(config.SCRIPT, REPO_DIR / "weather_report.py")
        self.assertEqual(config.CONFIG_PATH, REPO_DIR / "config.env")
        self.assertTrue(config.SCRIPT.is_file())


class TestPop(unittest.TestCase):
    def test_null_and_missing_are_zero(self):
        self.assertEqual(report.pop(None), 0)
        self.assertEqual(report.pop({"probabilityOfPrecipitation": {"value": None}}), 0)
        self.assertEqual(report.pop({}), 0)
        self.assertEqual(report.pop({"probabilityOfPrecipitation": {"value": 40}}), 40)


class TestAnalyze(unittest.TestCase):
    def test_picks_today_and_tonight(self):
        data, _ = make_report(forecast(tonight_low=48))
        self.assertEqual(data["high"], 72)
        self.assertEqual(data["low"], 48)

    def test_rain_warning_uses_tomorrow_only(self):
        # 90% on the day after tomorrow must not trigger the warning.
        data, _ = make_report(forecast(tomorrow_pop=20))
        self.assertFalse(data["rain_warning"])

    def test_rain_warning_threshold_edge(self):
        self.assertFalse(make_report(forecast(tomorrow_pop=29))[0]["rain_warning"])
        self.assertTrue(make_report(forecast(tomorrow_pop=30))[0]["rain_warning"])

    def test_rain_warning_from_tomorrow_night(self):
        data, _ = make_report(forecast(tomorrow_pop=10, tomorrow_night_pop=60))
        self.assertTrue(data["rain_warning"])
        self.assertEqual(data["tomorrow_rain"], 60)

    def test_freeze_threshold_edge(self):
        self.assertTrue(make_report(forecast(tonight_low=32))[0]["freeze_warning"])
        self.assertFalse(make_report(forecast(tonight_low=33))[0]["freeze_warning"])

    def test_custom_thresholds(self):
        cfg = dict(config.DEFAULTS, RAIN_THRESHOLD="50", FREEZE_THRESHOLD_F="36")
        data, _ = make_report(forecast(tonight_low=35, tomorrow_pop=40), cfg=cfg)
        self.assertFalse(data["rain_warning"])
        self.assertTrue(data["freeze_warning"])

    def test_evening_run_has_no_daytime_period(self):
        evening = datetime(2026, 10, 22, 19, 0, tzinfo=EDT)
        data, embed = make_report(forecast()[1:], now=evening)
        self.assertIsNone(data["high"])
        self.assertIn("**High** —", embed["description"])


class TestEmbed(unittest.TestCase):
    def test_colors_by_priority(self):
        self.assertEqual(make_report(forecast())[1]["color"], report.COLOR_CLEAR)
        self.assertEqual(make_report(forecast(tomorrow_pop=40))[1]["color"], report.COLOR_RAIN)
        self.assertEqual(make_report(forecast(tonight_low=30, tomorrow_pop=40))[1]["color"], report.COLOR_FREEZE)
        self.assertEqual(make_report(forecast(tonight_low=30, tomorrow_pop=40), [ALERT])[1]["color"], report.COLOR_ALERT)

    def test_all_warnings_listed(self):
        desc = make_report(forecast(tonight_low=28, tomorrow_pop=40), [ALERT])[1]["description"]
        self.assertIn("🚨 **Freeze Warning** until Fri 9:00 AM", desc)
        self.assertIn("🥶 **Freeze tonight** — low of 28°F", desc)
        self.assertIn("☔ **Rain tomorrow** — 40% chance (Chance Showers)", desc)
        self.assertNotIn("No warnings", desc)

    def test_no_warnings(self):
        self.assertIn("✅ No warnings", make_report(forecast())[1]["description"])

    def test_rain_line_hidden_below_threshold(self):
        for today, tonight in ((None, None), (9, 5)):
            desc = make_report(forecast(today_pop=today, tonight_pop=tonight))[1]["description"]
            self.assertNotIn("**Rain**", desc, (today, tonight))

    def test_rain_line_shown_at_threshold(self):
        desc = make_report(forecast(today_pop=10))[1]["description"]
        self.assertIn("🌧️ **Rain** 10% today  ·  0% tonight", desc)
        desc = make_report(forecast(tonight_pop=40))[1]["description"]
        self.assertIn("🌧️ **Rain** 0% today  ·  40% tonight", desc)

    def test_title_and_test_tag(self):
        _, embed = make_report(forecast())
        self.assertEqual(embed["title"], "☀️ New York, NY — Thu, Oct 22")
        data = report.analyze(forecast(), [], dict(config.DEFAULTS), NOW)
        self.assertTrue(report.build_embed(data, dict(config.DEFAULTS), NOW, test=True)["title"].startswith("[TEST] "))


class TestConditionEmoji(unittest.TestCase):
    def test_mapping(self):
        cases = {
            "Chance Showers And Thunderstorms": "⛈️",
            "Light Snow": "🌨️",
            "Rain Likely": "🌧️",
            "Patchy Fog": "🌫️",
            "Partly Sunny": "⛅",
            "Cloudy": "☁️",
            "Sunny": "☀️",
        }
        for text, emoji in cases.items():
            self.assertEqual(report.condition_emoji(text), emoji, text)
        self.assertEqual(report.condition_emoji("Clear", is_day=False), "🌙")


if __name__ == "__main__":
    unittest.main()
