"""Tests for the City, ST lookup, HTTP retry rules, and the pre-save forecast check.

All network calls are mocked.
"""

import contextlib
import io
import sys
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import weather_report as wr  # noqa: E402
from src import config, location, report, wizard  # noqa: E402

WEBHOOK = "https://discord.com/api/webhooks/123456/abc"

# Shape from the Open-Meteo geocoding docs (https://open-meteo.com/en/docs/geocoding-api)
NEW_YORK = {
    "results": [
        {"name": "New York", "latitude": 40.71427, "longitude": -74.00597, "country_code": "US",
         "admin1": "New York", "admin2": "New York County"},
        {"name": "New York", "latitude": 32.16793, "longitude": -95.66913, "country_code": "US",
         "admin1": "Texas", "admin2": "Henderson County"},
        {"name": "New York", "latitude": 1.0, "longitude": 1.0, "country_code": "CA", "admin1": "Ontario"},
    ]
}


class FakeFetch:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, url, user_agent, **kwargs):
        self.calls.append((url, user_agent, kwargs))
        return self.response


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class TestGeocode(unittest.TestCase):
    def test_parses_us_results_only(self):
        fetch = FakeFetch(NEW_YORK)
        places = location.geocode("New York, NY", "ua", fetch=fetch)
        self.assertEqual(len(places), 2)  # Canadian result dropped
        self.assertEqual(places[0], {"name": "New York, NY", "detail": "New York County, New York",
                                     "lat": 40.7143, "lon": -74.0060})
        self.assertEqual(places[1]["name"], "New York, TX")

    def test_request(self):
        fetch = FakeFetch({})
        location.geocode("  New York, NY ", "weather-reporter/1.0 (me)", fetch=fetch)
        url, ua, kwargs = fetch.calls[0]
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertTrue(url.startswith(location.GEOCODE_URL))
        self.assertEqual(query["name"], ["New York, NY"])
        self.assertEqual(query["countryCode"], ["US"])
        self.assertEqual(ua, "weather-reporter/1.0 (me)")
        self.assertEqual(kwargs["accept"], "application/json")

    def test_no_results(self):
        self.assertEqual(location.geocode("Nowhere, ZZ", "ua", fetch=FakeFetch({})), [])

    def test_describe(self):
        place = location.geocode("New York, NY", "ua", fetch=FakeFetch(NEW_YORK))[0]
        self.assertEqual(location.describe_place(place), "New York, NY (New York County, New York)  40.7143, -74.0060")


class TestLocationInSetup(unittest.TestCase):
    def collect(self, argv, lookup, choose=location.first_place):
        args = wr.build_parser().parse_args(["--yes", "--webhook", WEBHOOK, *argv])
        with quiet():
            return wizard.collect_values(args, {}, lookup=lookup, choose=choose)

    def test_location_lookup_uses_first_match(self):
        places = location.geocode("New York", "ua", fetch=FakeFetch(NEW_YORK))
        values = self.collect(["--location", "New York"], lookup=lambda q: places)
        self.assertEqual(values["LOCATION_NAME"], "New York, NY")
        self.assertEqual((values["LATITUDE"], values["LONGITUDE"]), ("40.7143", "-74.0060"))

    def test_custom_chooser(self):
        places = location.geocode("New York", "ua", fetch=FakeFetch(NEW_YORK))
        values = self.collect(["--location", "New York"], lookup=lambda q: places, choose=lambda p: p[1])
        self.assertEqual(values["LOCATION_NAME"], "New York, TX")

    def test_lat_lon_skip_lookup(self):
        def fail(q):
            raise AssertionError("lookup should not run")
        values = self.collect(["--location", "Camp", "--lat", "35", "--lon", "-80"], lookup=fail)
        self.assertEqual(values["LOCATION_NAME"], "Camp")

    def test_lookup_unreachable_is_a_setup_error(self):
        def down(q):
            raise RuntimeError("network down")
        with self.assertRaises(config.SetupError) as ctx:
            self.collect(["--location", "New York, NY"], lookup=down)
        self.assertIn("--lat and --lon", str(ctx.exception))

    def test_no_match_is_an_error(self):
        with self.assertRaises(config.SetupError):
            self.collect(["--location", "Nowhere, ZZ"], lookup=lambda q: [])


class TestHttpRetries(unittest.TestCase):
    def http_error(self, code):
        return urllib.error.HTTPError("https://x", code, "err", {}, None)

    def test_4xx_fails_without_retry(self):
        with mock.patch("urllib.request.urlopen", side_effect=self.http_error(404)) as urlopen, \
             mock.patch("time.sleep") as sleep:
            with self.assertRaises(report.HttpError) as ctx:
                report.http_get_json("https://x", "ua")
        self.assertEqual(ctx.exception.code, 404)
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_5xx_and_network_errors_retry(self):
        errors = [self.http_error(503), urllib.error.URLError("down"), self.http_error(500)]
        with mock.patch("urllib.request.urlopen", side_effect=errors) as urlopen, \
             mock.patch("time.sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                report.http_get_json("https://x", "ua")
        self.assertEqual(urlopen.call_count, 3)
        self.assertNotIsInstance(ctx.exception, report.HttpError)


class TestForecastCheck(unittest.TestCase):
    values = {"LATITUDE": "51.5", "LONGITUDE": "-0.12"}

    def check(self, error=None):
        def build(cfg, now):
            if error:
                raise error
            return {"embeds": [{"title": "ok"}]}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            wizard.check_forecast(self.values, Path("/opt/x/weather_report.py"), build=build)
        return out.getvalue()

    def test_ok(self):
        self.assertIn("OK: ok", self.check())

    def test_outside_nws_coverage_is_fatal(self):
        with self.assertRaises(config.SetupError) as ctx:
            self.check(report.HttpError("https://api.weather.gov/points/51.5,-0.12", 404))
        self.assertIn("NWS has no forecast", str(ctx.exception))

    def test_network_problem_only_warns(self):
        self.assertIn("WARN", self.check(RuntimeError("no network")))


if __name__ == "__main__":
    unittest.main()
