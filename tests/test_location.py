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
from src import config, location, report  # noqa: E402

WEBHOOK = "https://discord.com/api/webhooks/123456/abc"

# Shape from the Open-Meteo geocoding docs (https://open-meteo.com/en/docs/geocoding-api)
EVANS = {
    "results": [
        {"name": "Evans", "latitude": 33.53376, "longitude": -82.13067, "country_code": "US",
         "admin1": "Georgia", "admin2": "Columbia County"},
        {"name": "Evans", "latitude": 40.37637, "longitude": -104.69220, "country_code": "US",
         "admin1": "Colorado", "admin2": "Weld County"},
        {"name": "Evans", "latitude": 1.0, "longitude": 1.0, "country_code": "CA", "admin1": "Ontario"},
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
        fetch = FakeFetch(EVANS)
        places = location.geocode("Evans, GA", "ua", fetch=fetch)
        self.assertEqual(len(places), 2)  # Canadian result dropped
        self.assertEqual(places[0], {"name": "Evans, GA", "detail": "Columbia County, Georgia",
                                     "lat": 33.5338, "lon": -82.1307})
        self.assertEqual(places[1]["name"], "Evans, CO")

    def test_request(self):
        fetch = FakeFetch({})
        location.geocode("  Evans, GA ", "weather-reporter/1.0 (me)", fetch=fetch)
        url, ua, kwargs = fetch.calls[0]
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertTrue(url.startswith(location.GEOCODE_URL))
        self.assertEqual(query["name"], ["Evans, GA"])
        self.assertEqual(query["countryCode"], ["US"])
        self.assertEqual(ua, "weather-reporter/1.0 (me)")
        self.assertEqual(kwargs["accept"], "application/json")

    def test_no_results(self):
        self.assertEqual(location.geocode("Nowhere, ZZ", "ua", fetch=FakeFetch({})), [])

    def test_describe(self):
        place = location.geocode("Evans, GA", "ua", fetch=FakeFetch(EVANS))[0]
        self.assertEqual(location.describe_place(place), "Evans, GA (Columbia County, Georgia)  33.5338, -82.1307")


class TestLocationInSetup(unittest.TestCase):
    def collect(self, argv, lookup, choose=location.first_place):
        args = wr.build_parser().parse_args(["--yes", "--webhook", WEBHOOK, *argv])
        with quiet():
            return wr.collect_values(args, {}, lookup=lookup, choose=choose)

    def test_location_lookup_uses_first_match(self):
        places = location.geocode("Evans", "ua", fetch=FakeFetch(EVANS))
        values = self.collect(["--location", "Evans"], lookup=lambda q: places)
        self.assertEqual(values["LOCATION_NAME"], "Evans, GA")
        self.assertEqual((values["LATITUDE"], values["LONGITUDE"]), ("33.5338", "-82.1307"))

    def test_custom_chooser(self):
        places = location.geocode("Evans", "ua", fetch=FakeFetch(EVANS))
        values = self.collect(["--location", "Evans"], lookup=lambda q: places, choose=lambda p: p[1])
        self.assertEqual(values["LOCATION_NAME"], "Evans, CO")

    def test_lat_lon_skip_lookup(self):
        def fail(q):
            raise AssertionError("lookup should not run")
        values = self.collect(["--location", "Camp", "--lat", "35", "--lon", "-80"], lookup=fail)
        self.assertEqual(values["LOCATION_NAME"], "Camp")

    def test_lookup_unreachable_is_a_setup_error(self):
        def down(q):
            raise RuntimeError("network down")
        with self.assertRaises(config.SetupError) as ctx:
            self.collect(["--location", "Evans, GA"], lookup=down)
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
            wr.check_forecast(self.values, Path("/opt/x/weather_report.py"), build=build)
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
