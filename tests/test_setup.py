"""Tests for setup: instance naming, config/cron/logrotate files, and value handling.

Everything is written into temporary folders; nothing touches /etc.
"""

import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import weather_report as wr  # noqa: E402
from src import config, install, wizard  # noqa: E402

WEBHOOK = "https://discord.com/api/webhooks/123456/abc_DEF-ghi"


def setup_args(argv):
    return wr.build_parser().parse_args(["--yes", *argv])


class TestInstanceName(unittest.TestCase):
    def test_names(self):
        cases = {
            "/opt/weather-reporter": "weather-reporter",
            "/opt/weather-reporter2": "weather-reporter2",
            "/opt/nyc": "weather-reporter-nyc",
            "/opt/NYC Weather": "weather-reporter-nyc-weather",
            "/opt/Weather-Reporter.Test": "weather-reporter-test",
        }
        for folder, expected in cases.items():
            self.assertEqual(install.instance_name(Path(folder)), expected, folder)

    def test_unusable_name(self):
        with self.assertRaises(config.SetupError):
            install.instance_name(Path("/opt/..."))


class TestValidation(unittest.TestCase):
    def test_webhooks(self):
        self.assertTrue(config.valid_webhook(WEBHOOK))
        self.assertTrue(config.valid_webhook("https://discordapp.com/api/webhooks/1/x"))
        self.assertTrue(config.valid_webhook("https://ptb.discord.com/api/webhooks/1/x"))
        for bad in ("", "https://discord.com/api/webhooks/REPLACE_ME",
                    "http://discord.com/api/webhooks/1/x", "https://evil.com/api/webhooks/1/x"):
            self.assertFalse(config.valid_webhook(bad), bad)

    def test_run_time(self):
        self.assertEqual(config.parse_run_time("08:00"), (8, 0))
        self.assertEqual(config.parse_run_time("7:05"), (7, 5))
        self.assertEqual(config.parse_run_time("23:59"), (23, 59))
        for bad in ("24:00", "8", "8:5", "08:60", "noon", ""):
            with self.assertRaises(config.SetupError, msg=bad):
                config.parse_run_time(bad)


class TestCollectValues(unittest.TestCase):
    def test_flags_override_existing_and_defaults(self):
        existing = {"DISCORD_WEBHOOK_URL": WEBHOOK, "RUN_TIME": "06:00", "CONTACT": "old@example.com"}
        values = wizard.collect_values(setup_args(["--time", "7:30", "--freeze-threshold", "34"]), existing)
        self.assertEqual(values["RUN_TIME"], "07:30")          # flag, normalized
        self.assertEqual(values["FREEZE_THRESHOLD_F"], "34")   # flag
        self.assertEqual(values["CONTACT"], "old@example.com")  # existing
        self.assertEqual(values["RAIN_THRESHOLD"], "30")       # default

    def test_lat_lon_and_location_label(self):
        values = wizard.collect_values(setup_args(
            ["--webhook", WEBHOOK, "--location", "New York, NY", "--lat", "40.71431", "--lon", "-74.0060"]), {})
        self.assertEqual((values["LATITUDE"], values["LONGITUDE"]), ("40.7143", "-74.0060"))
        self.assertEqual(values["LOCATION_NAME"], "New York, NY")

    def test_existing_unknown_keys_are_kept(self):
        values = wizard.collect_values(setup_args([]), {"DISCORD_WEBHOOK_URL": WEBHOOK, "MY_NOTE": "hi"})
        self.assertEqual(values["MY_NOTE"], "hi")

    def test_errors(self):
        bad = [
            ([], {}),                                               # no webhook
            (["--webhook", "https://example.com"], {}),             # invalid webhook
            (["--webhook", WEBHOOK, "--lat", "1"], {}),             # lat without lon
            (["--webhook", WEBHOOK, "--time", "25:00"], {}),        # bad time
            (["--webhook", WEBHOOK, "--rain-threshold", "101"], {}),
        ]
        for argv, existing in bad:
            with self.assertRaises(config.SetupError, msg=argv):
                wizard.collect_values(setup_args(argv), existing)


class InstallCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cron_dir = self.root / "cron.d"
        self.logrotate_dir = self.root / "logrotate.d"
        self.log_dir = self.root / "log" / "weather-reporter"
        self.cron_dir.mkdir()
        self.logrotate_dir.mkdir()
        self.values = wizard.collect_values(setup_args(["--webhook", WEBHOOK, "--time", "07:30"]), {})

    def repo(self, name):
        repo = self.root / "opt" / name
        repo.mkdir(parents=True)
        return repo

    def install(self, repo, values=None):
        return install.install(values or self.values, repo, repo / "weather_report.py", "/usr/bin/python3",
                          self.cron_dir, self.logrotate_dir, self.log_dir)


class TestInstall(InstallCase):
    def test_writes_all_files(self):
        repo = self.repo("weather-reporter")
        result = self.install(repo)

        config_file = repo / "config.env"
        self.assertEqual(stat.S_IMODE(config_file.stat().st_mode), 0o600)
        parsed = config.parse_env_file(config_file)
        self.assertEqual(parsed["DISCORD_WEBHOOK_URL"], WEBHOOK)
        self.assertEqual(parsed["RUN_TIME"], "07:30")
        for line in config_file.read_text().splitlines():  # comments only on their own lines
            self.assertFalse("=" in line and "#" in line.split("=", 1)[1], line)

        cron = (self.cron_dir / "weather-reporter").read_text()
        self.assertIn(
            f'30 7 * * * root "/usr/bin/python3" "{repo}/weather_report.py" --run '
            f'>> "{self.log_dir}/weather-reporter.log" 2>&1', cron)
        self.assertEqual(stat.S_IMODE((self.cron_dir / "weather-reporter").stat().st_mode), 0o644)
        self.assertTrue(self.log_dir.is_dir())
        self.assertIn(f'"{self.log_dir}/weather-reporter.log"', (self.logrotate_dir / "weather-reporter").read_text())
        self.assertEqual(result["instance"], "weather-reporter")

    def test_rerun_overwrites_own_install(self):
        repo = self.repo("weather-reporter")
        self.install(repo)
        self.install(repo, dict(self.values, RUN_TIME="09:15"))
        self.assertIn("15 9 * * *", (self.cron_dir / "weather-reporter").read_text())

    def test_two_instances_side_by_side(self):
        self.install(self.repo("weather-reporter"))
        self.install(self.repo("weather-reporter2"))
        self.assertEqual(sorted(p.name for p in self.cron_dir.iterdir()), ["weather-reporter", "weather-reporter2"])

    def test_refuses_other_installs_cron_job(self):
        self.install(self.repo("weather-reporter"))
        other = self.root / "srv" / "weather-reporter"
        other.mkdir(parents=True)
        with self.assertRaises(config.SetupError) as ctx:
            self.install(other)
        self.assertIn(str(self.root / "opt" / "weather-reporter"), str(ctx.exception))

    def test_refuses_unrelated_cron_file(self):
        (self.cron_dir / "weather-reporter").write_text("0 0 * * * root something-else\n")
        with self.assertRaises(config.SetupError):
            self.install(self.repo("weather-reporter"))

    def test_upgrades_cron_job_from_old_setup_sh(self):
        repo = self.repo("weather-reporter")
        (self.cron_dir / "weather-reporter").write_text(
            f"# Managed by {repo}/setup.sh - edit config.env and re-run setup.sh instead.\n"
            "SHELL=/bin/sh\n"
            f"0 8 * * * weather-reporter /usr/bin/python3 {repo}/weather_report.py >> /var/log/x.log 2>&1\n")
        self.install(repo)
        cron = (self.cron_dir / "weather-reporter").read_text()
        self.assertIn(" root ", cron)
        self.assertIn("--run", cron)
        self.assertNotIn(" weather-reporter /usr/bin", cron)

    def test_path_with_spaces_is_quoted(self):
        repo = self.repo("NYC Weather")
        self.install(repo)
        cron = (self.cron_dir / "weather-reporter-nyc-weather").read_text()
        self.assertIn(f'"{repo}/weather_report.py" --run', cron)

    def test_rejects_percent_in_path(self):
        with self.assertRaises(config.SetupError):
            self.install(self.repo("weather%reporter"))

    def test_existing_config_values_round_trip(self):
        repo = self.repo("weather-reporter")
        self.install(repo, dict(self.values, LOCATION_NAME="New York, TX", MY_NOTE="kept"))
        again = wizard.collect_values(setup_args([]), config.parse_env_file(repo / "config.env"))
        self.assertEqual(again["LOCATION_NAME"], "New York, TX")
        self.assertEqual(again["MY_NOTE"], "kept")


if __name__ == "__main__":
    unittest.main()
