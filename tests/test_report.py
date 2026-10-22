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

import weather_report as wr  # noqa: E402

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


def report(periods, alerts=(), cfg=None, now=NOW):
    cfg = cfg or dict(wr.DEFAULTS)
    data = wr.analyze(periods, list(alerts), cfg, now)
    return data, wr.build_embed(data, cfg, now)


class TestConfig(unittest.TestCase):
    def write(self, text):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        tmp.write(text)
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink)
        return Path(tmp.name)

    def test_example_config_parses_cleanly(self):
        # Regression: inline comments in the example once broke int() parsing.
        cfg = wr.load_config(REPO_DIR / "config.env.example")
        for key in ("RAIN_THRESHOLD", "RAIN_SHOW_THRESHOLD", "FREEZE_THRESHOLD_F"):
            int(cfg[key])
        float(cfg["LATITUDE"])
        float(cfg["LONGITUDE"])
        self.assertEqual(cfg["LOCATION_NAME"], "New York, NY")

    def test_quotes_comments_and_export(self):
        cfg = wr.load_config(self.write(
            '# comment\n\nLOCATION_NAME="Evans, GA"\nRAIN_THRESHOLD=40\n'
            "export CONTACT='me@example.com'\nnot a setting\n"
        ))
        self.assertEqual(cfg["LOCATION_NAME"], "Evans, GA")
        self.assertEqual(cfg["RAIN_THRESHOLD"], "40")
        self.assertEqual(cfg["CONTACT"], "me@example.com")

    def test_missing_keys_fall_back_to_defaults(self):
        cfg = wr.load_config(self.write('DISCORD_WEBHOOK_URL="x"\n'))
        self.assertEqual(cfg["RAIN_SHOW_THRESHOLD"], "10")
        self.assertEqual(cfg["FREEZE_THRESHOLD_F"], "32")

    def test_missing_file_exits(self):
        with self.assertRaises(SystemExit):
            wr.load_config(REPO_DIR / "does-not-exist.env")


class TestPop(unittest.TestCase):
    def test_null_and_missing_are_zero(self):
        self.assertEqual(wr.pop(None), 0)
        self.assertEqual(wr.pop({"probabilityOfPrecipitation": {"value": None}}), 0)
        self.assertEqual(wr.pop({}), 0)
        self.assertEqual(wr.pop({"probabilityOfPrecipitation": {"value": 40}}), 40)


class TestAnalyze(unittest.TestCase):
    def test_picks_today_and_tonight(self):
        data, _ = report(forecast(tonight_low=48))
        self.assertEqual(data["high"], 72)
        self.assertEqual(data["low"], 48)

    def test_rain_warning_uses_tomorrow_only(self):
        # 90% on the day after tomorrow must not trigger the warning.
        data, _ = report(forecast(tomorrow_pop=20))
        self.assertFalse(data["rain_warning"])

    def test_rain_warning_threshold_edge(self):
        self.assertFalse(report(forecast(tomorrow_pop=29))[0]["rain_warning"])
        self.assertTrue(report(forecast(tomorrow_pop=30))[0]["rain_warning"])

    def test_rain_warning_from_tomorrow_night(self):
        data, _ = report(forecast(tomorrow_pop=10, tomorrow_night_pop=60))
        self.assertTrue(data["rain_warning"])
        self.assertEqual(data["tomorrow_rain"], 60)

    def test_freeze_threshold_edge(self):
        self.assertTrue(report(forecast(tonight_low=32))[0]["freeze_warning"])
        self.assertFalse(report(forecast(tonight_low=33))[0]["freeze_warning"])

    def test_custom_thresholds(self):
        cfg = dict(wr.DEFAULTS, RAIN_THRESHOLD="50", FREEZE_THRESHOLD_F="36")
        data, _ = report(forecast(tonight_low=35, tomorrow_pop=40), cfg=cfg)
        self.assertFalse(data["rain_warning"])
        self.assertTrue(data["freeze_warning"])

    def test_evening_run_has_no_daytime_period(self):
        evening = datetime(2026, 10, 22, 19, 0, tzinfo=EDT)
        data, embed = report(forecast()[1:], now=evening)
        self.assertIsNone(data["high"])
        self.assertIn("**High** —", embed["description"])


class TestEmbed(unittest.TestCase):
    def test_colors_by_priority(self):
        self.assertEqual(report(forecast())[1]["color"], wr.COLOR_CLEAR)
        self.assertEqual(report(forecast(tomorrow_pop=40))[1]["color"], wr.COLOR_RAIN)
        self.assertEqual(report(forecast(tonight_low=30, tomorrow_pop=40))[1]["color"], wr.COLOR_FREEZE)
        self.assertEqual(report(forecast(tonight_low=30, tomorrow_pop=40), [ALERT])[1]["color"], wr.COLOR_ALERT)

    def test_all_warnings_listed(self):
        desc = report(forecast(tonight_low=28, tomorrow_pop=40), [ALERT])[1]["description"]
        self.assertIn("🚨 **Freeze Warning** until Fri 9:00 AM", desc)
        self.assertIn("🥶 **Freeze tonight** — low of 28°F", desc)
        self.assertIn("☔ **Rain tomorrow** — 40% chance (Chance Showers)", desc)
        self.assertNotIn("No warnings", desc)

    def test_no_warnings(self):
        self.assertIn("✅ No warnings", report(forecast())[1]["description"])

    def test_rain_line_hidden_below_threshold(self):
        for today, tonight in ((None, None), (9, 5)):
            desc = report(forecast(today_pop=today, tonight_pop=tonight))[1]["description"]
            self.assertNotIn("**Rain**", desc, (today, tonight))

    def test_rain_line_shown_at_threshold(self):
        desc = report(forecast(today_pop=10))[1]["description"]
        self.assertIn("🌧️ **Rain** 10% today  ·  0% tonight", desc)
        desc = report(forecast(tonight_pop=40))[1]["description"]
        self.assertIn("🌧️ **Rain** 0% today  ·  40% tonight", desc)

    def test_title_and_test_tag(self):
        _, embed = report(forecast())
        self.assertEqual(embed["title"], "☀️ New York, NY — Thu, Oct 22")
        data = wr.analyze(forecast(), [], dict(wr.DEFAULTS), NOW)
        self.assertTrue(wr.build_embed(data, dict(wr.DEFAULTS), NOW, test=True)["title"].startswith("[TEST] "))


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
            self.assertEqual(wr.condition_emoji(text), emoji, text)
        self.assertEqual(wr.condition_emoji("Clear", is_day=False), "🌙")


if __name__ == "__main__":
    unittest.main()
