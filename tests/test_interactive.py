"""Tests for the interactive setup questions, using scripted answers."""

import contextlib
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import weather_report as wr  # noqa: E402
from src import wizard  # noqa: E402

WEBHOOK = "https://discord.com/api/webhooks/1562538190261452942/IACEvp57TPuvBTMR2WZmO4AdQLcU"
NEW_WEBHOOK = "https://discord.com/api/webhooks/999/newtoken"
NEW_YORK_NY = {"name": "New York, NY", "detail": "New York County, New York", "lat": 40.7143, "lon": -74.0060}
NEW_YORK_TX = {"name": "New York, TX", "detail": "Henderson County, Texas", "lat": 32.1679, "lon": -95.6691}


class Script:
    """Feeds answers to input() and records the prompts."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"unexpected prompt: {prompt}")
        return self.answers.pop(0)


def run(argv, existing, answers, lookup=lambda q: [NEW_YORK_NY]):
    args = wr.build_parser().parse_args(argv)
    script = Script(*answers)
    with contextlib.redirect_stdout(io.StringIO()) as out:
        args = wizard.ask_setup_questions(args, existing, ask_fn=script, lookup=lookup)
        values = wizard.collect_values(args, existing)
    return values, script, out.getvalue()


class TestHelpers(unittest.TestCase):
    def test_mask_webhook(self):
        self.assertEqual(wizard.mask_webhook(WEBHOOK),
                         "https://discord.com/api/webhooks/1562538190261452942/****QLcU")
        self.assertNotIn("IACEvp57", wizard.mask_webhook(WEBHOOK))

    def test_yes_no(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(wizard.ask_yes_no("q", ask_fn=Script("")))
            self.assertFalse(wizard.ask_yes_no("q", default=False, ask_fn=Script("")))
            self.assertFalse(wizard.ask_yes_no("q", ask_fn=Script("maybe", "N")))
            self.assertTrue(wizard.ask_yes_no("q", ask_fn=Script("yes")))


class TestFirstRun(unittest.TestCase):
    def test_answers_everything(self):
        values, script, _ = run([], {}, [
            "not-a-url", WEBHOOK,       # bad webhook is re-asked
            "New York, NY", "",         # lookup, confirm the single match
            "25:00", "7:30",            # bad time is re-asked
            "me@example.com",
        ])
        self.assertEqual(values["DISCORD_WEBHOOK_URL"], WEBHOOK)
        self.assertEqual(values["LOCATION_NAME"], "New York, NY")
        self.assertEqual((values["LATITUDE"], values["LONGITUDE"]), ("40.7143", "-74.0060"))
        self.assertEqual(values["RUN_TIME"], "07:30")
        self.assertEqual(values["CONTACT"], "me@example.com")
        self.assertEqual(values["RAIN_THRESHOLD"], "30")  # thresholds aren't asked
        self.assertEqual(len(script.prompts), 7)

    def test_enter_takes_defaults(self):
        values, _, _ = run([], {}, [WEBHOOK, "", "", ""])
        self.assertEqual(values["LOCATION_NAME"], "New York, NY")
        self.assertEqual(values["RUN_TIME"], "08:00")
        self.assertEqual(values["CONTACT"], "")


class TestLocationQuestion(unittest.TestCase):
    def test_pick_from_several(self):
        values, _, out = run([], {}, [WEBHOOK, "New York", "2", "", ""],
                             lookup=lambda q: [NEW_YORK_NY, NEW_YORK_TX])
        self.assertEqual(values["LOCATION_NAME"], "New York, TX")
        self.assertIn("2) New York, TX (Henderson County, Texas)", out)

    def test_reject_and_search_again(self):
        values, _, _ = run([], {}, [WEBHOOK, "New York", "n", "New York, TX", "", "", ""],
                           lookup=lambda q: [NEW_YORK_TX] if q.endswith("TX") else [NEW_YORK_NY])
        self.assertEqual(values["LOCATION_NAME"], "New York, TX")

    def test_zero_searches_again(self):
        values, _, _ = run([], {}, [WEBHOOK, "New York", "0", "New York", "1", "", ""],
                           lookup=lambda q: [NEW_YORK_NY, NEW_YORK_TX])
        self.assertEqual(values["LOCATION_NAME"], "New York, NY")
        self.assertEqual(values["LATITUDE"], "40.7143")  # looked up, not the 40.7128 default

    def test_no_match_or_network_error_asks_again(self):
        calls = []

        def lookup(q):
            calls.append(q)
            if q == "Nowhere":
                return []
            if q == "Offline":
                raise RuntimeError("network down")
            return [NEW_YORK_NY]

        values, _, out = run([], {}, [WEBHOOK, "Nowhere", "Offline", "New York, NY", "", "", ""], lookup=lookup)
        self.assertEqual(calls, ["Nowhere", "Offline", "New York, NY"])
        self.assertIn("No US place found", out)
        self.assertIn("Couldn't reach", out)
        self.assertEqual(values["LOCATION_NAME"], "New York, NY")
        self.assertEqual(values["LATITUDE"], "40.7143")  # looked up, not the 40.7128 default


class TestReconfigure(unittest.TestCase):
    existing = {"DISCORD_WEBHOOK_URL": WEBHOOK, "LOCATION_NAME": "New York, TX", "LATITUDE": "32.1679",
                "LONGITUDE": "-95.6691", "RUN_TIME": "06:45", "CONTACT": "me@example.com",
                "FREEZE_THRESHOLD_F": "34"}

    def test_enter_keeps_everything_without_lookup(self):
        def no_lookup(q):
            raise AssertionError("Enter should keep the current location")
        values, script, _ = run([], self.existing, ["", "", "", ""], lookup=no_lookup)
        for key, value in self.existing.items():
            self.assertEqual(values[key], value, key)
        self.assertIn("****QLcU", script.prompts[0])          # masked, not the full token
        self.assertNotIn("IACEvp57", script.prompts[0])
        self.assertIn("[New York, TX]", script.prompts[1])
        self.assertIn("[06:45]", script.prompts[2])

    def test_change_only_webhook(self):
        values, _, _ = run([], self.existing, [NEW_WEBHOOK, "", "", ""])
        self.assertEqual(values["DISCORD_WEBHOOK_URL"], NEW_WEBHOOK)
        self.assertEqual(values["LOCATION_NAME"], "New York, TX")


class TestFlagsSkipQuestions(unittest.TestCase):
    def test_flags_are_not_asked(self):
        values, script, _ = run(
            ["--webhook", WEBHOOK, "--location", "Camp", "--lat", "35", "--lon", "-80", "--time", "06:00"],
            {}, ["me@example.com"])
        self.assertEqual(len(script.prompts), 1)  # only contact
        self.assertIn("Contact", script.prompts[0])
        self.assertEqual(values["LOCATION_NAME"], "Camp")

    def test_threshold_flags_still_apply(self):
        values, _, _ = run(["--rain-threshold", "50"], {}, [WEBHOOK, "", "", ""])
        self.assertEqual(values["RAIN_THRESHOLD"], "50")


if __name__ == "__main__":
    unittest.main()
