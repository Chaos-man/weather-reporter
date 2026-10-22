"""Tests for the interactive setup questions, using scripted answers."""

import contextlib
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import weather_report as wr  # noqa: E402

WEBHOOK = "https://discord.com/api/webhooks/1562538190261452942/IACEvp57TPuvBTMR2WZmO4AdQLcU"
NEW_WEBHOOK = "https://discord.com/api/webhooks/999/newtoken"
EVANS_GA = {"name": "Evans, GA", "detail": "Columbia County, Georgia", "lat": 33.5338, "lon": -82.1307}
EVANS_CO = {"name": "Evans, CO", "detail": "Weld County, Colorado", "lat": 40.3764, "lon": -104.6922}


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


def run(argv, existing, answers, lookup=lambda q: [EVANS_GA]):
    args = wr.build_parser().parse_args(argv)
    script = Script(*answers)
    with contextlib.redirect_stdout(io.StringIO()) as out:
        args = wr.ask_setup_questions(args, existing, ask_fn=script, lookup=lookup)
        values = wr.collect_values(args, existing)
    return values, script, out.getvalue()


class TestHelpers(unittest.TestCase):
    def test_mask_webhook(self):
        self.assertEqual(wr.mask_webhook(WEBHOOK),
                         "https://discord.com/api/webhooks/1562538190261452942/****QLcU")
        self.assertNotIn("IACEvp57", wr.mask_webhook(WEBHOOK))

    def test_yes_no(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(wr.ask_yes_no("q", ask_fn=Script("")))
            self.assertFalse(wr.ask_yes_no("q", default=False, ask_fn=Script("")))
            self.assertFalse(wr.ask_yes_no("q", ask_fn=Script("maybe", "N")))
            self.assertTrue(wr.ask_yes_no("q", ask_fn=Script("yes")))


class TestFirstRun(unittest.TestCase):
    def test_answers_everything(self):
        values, script, _ = run([], {}, [
            "not-a-url", WEBHOOK,       # bad webhook is re-asked
            "Evans, GA", "",            # lookup, confirm the single match
            "25:00", "7:30",            # bad time is re-asked
            "me@example.com",
        ])
        self.assertEqual(values["DISCORD_WEBHOOK_URL"], WEBHOOK)
        self.assertEqual(values["LOCATION_NAME"], "Evans, GA")
        self.assertEqual((values["LATITUDE"], values["LONGITUDE"]), ("33.5338", "-82.1307"))
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
        values, _, out = run([], {}, [WEBHOOK, "Evans", "2", "", ""],
                             lookup=lambda q: [EVANS_GA, EVANS_CO])
        self.assertEqual(values["LOCATION_NAME"], "Evans, CO")
        self.assertIn("2) Evans, CO (Weld County, Colorado)", out)

    def test_reject_and_search_again(self):
        values, _, _ = run([], {}, [WEBHOOK, "Evans", "n", "Evans, CO", "", "", ""],
                           lookup=lambda q: [EVANS_CO] if q.endswith("CO") else [EVANS_GA])
        self.assertEqual(values["LOCATION_NAME"], "Evans, CO")

    def test_zero_searches_again(self):
        values, _, _ = run([], {}, [WEBHOOK, "Evans", "0", "Evans", "1", "", ""],
                           lookup=lambda q: [EVANS_GA, EVANS_CO])
        self.assertEqual(values["LOCATION_NAME"], "Evans, GA")

    def test_no_match_or_network_error_asks_again(self):
        calls = []

        def lookup(q):
            calls.append(q)
            if q == "Nowhere":
                return []
            if q == "Offline":
                raise RuntimeError("network down")
            return [EVANS_GA]

        values, _, out = run([], {}, [WEBHOOK, "Nowhere", "Offline", "Evans, GA", "", "", ""], lookup=lookup)
        self.assertEqual(calls, ["Nowhere", "Offline", "Evans, GA"])
        self.assertIn("No US place found", out)
        self.assertIn("Couldn't reach", out)
        self.assertEqual(values["LOCATION_NAME"], "Evans, GA")


class TestReconfigure(unittest.TestCase):
    existing = {"DISCORD_WEBHOOK_URL": WEBHOOK, "LOCATION_NAME": "Evans, GA", "LATITUDE": "33.5338",
                "LONGITUDE": "-82.1307", "RUN_TIME": "06:45", "CONTACT": "me@example.com",
                "FREEZE_THRESHOLD_F": "34"}

    def test_enter_keeps_everything_without_lookup(self):
        def no_lookup(q):
            raise AssertionError("Enter should keep the current location")
        values, script, _ = run([], self.existing, ["", "", "", ""], lookup=no_lookup)
        for key, value in self.existing.items():
            self.assertEqual(values[key], value, key)
        self.assertIn("****QLcU", script.prompts[0])          # masked, not the full token
        self.assertNotIn("IACEvp57", script.prompts[0])
        self.assertIn("[Evans, GA]", script.prompts[1])
        self.assertIn("[06:45]", script.prompts[2])

    def test_change_only_webhook(self):
        values, _, _ = run([], self.existing, [NEW_WEBHOOK, "", "", ""])
        self.assertEqual(values["DISCORD_WEBHOOK_URL"], NEW_WEBHOOK)
        self.assertEqual(values["LOCATION_NAME"], "Evans, GA")


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
