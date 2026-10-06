import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import check_flights as cf


def google_leg(frm, to, dep_time, arr_time, dep_date=(2026, 12, 18), arr_date=(2026, 12, 18)):
    leg = [None] * 23
    leg[3], leg[4] = frm, f"{frm} airport"
    leg[5], leg[6] = f"{to} airport", to
    leg[8], leg[10] = dep_time, arr_time
    leg[11] = 60
    leg[17] = "Airbus A321"
    leg[20], leg[21] = list(dep_date), list(arr_date)
    return leg


def google_item(airlines, legs, price):
    flight = [None] * 23
    flight[0] = "VJ"
    flight[1] = airlines
    flight[2] = legs
    flight[22] = [None] * 7 + [50000, 52000]
    return [flight, [[None, price]]]


def google_payload(best, other):
    payload = [None] * 8
    payload[2] = [best]
    payload[3] = [other]
    payload[7] = [None, [[["SKYTEAM", "SkyTeam"]], [["VJ", "Vietjet"], ["VN", "Vietnam Airlines"]]]]
    return payload


def google_html(payload):
    return (
        "<html><body><script class=\"ds:1\" nonce=\"x\">"
        "AF_initDataCallback({key: 'ds:1', hash: '1', data:"
        + json.dumps(payload)
        + ", sideChannel: {}});</script></body></html>"
    )


PAYLOAD = google_payload(
    best=[google_item(["Vietjet"], [google_leg("SGN", "DLI", [6, 5], [7])], 1234000)],
    other=[
        google_item(["Vietnam Airlines"], [google_leg("SGN", "DLI", [None, 31], [1, 30])], 1500000),
        google_item(
            ["Vietnam Airlines"],
            [google_leg("SGN", "HAN", [20], [22, 10]), google_leg("HAN", "DLI", [23, 50], [1, 40], arr_date=(2026, 12, 19))],
            3000000,
        ),
        ["not", "a", "flight"],
    ],
)


def itinerary(dep, arr, airline="Vietjet", price=100, frm="SGN", to="DLI", date="2026-12-18"):
    return cf.Itinerary(
        legs=(cf.Leg(frm, to, f"{date} {dep}", f"{date} {arr}"),), airlines=(airline,), price=price
    )


class GoogleParsingTest(unittest.TestCase):
    def test_reads_best_and_other_sections(self):
        flights = cf.parse_google_html(google_html(PAYLOAD))
        self.assertEqual(len(flights), 3)
        first, second, third = flights
        self.assertEqual(first.legs, (cf.Leg("SGN", "DLI", "2026-12-18 06:05", "2026-12-18 07:00"),))
        self.assertEqual(first.airlines, ("Vietjet",))
        self.assertEqual(first.price, 1234000)
        self.assertEqual(second.legs[0].departs, "2026-12-18 00:31")
        self.assertEqual(second.legs[0].arrives, "2026-12-18 01:30")
        self.assertEqual(third.stops, 1)
        self.assertEqual(third.legs[1].arrives, "2026-12-19 01:40")

    def test_agrees_with_fast_flights_parser(self):
        try:
            from fast_flights.parser import parse
        except ImportError:
            self.skipTest("fast-flights not installed")
        # fast-flights only reads the "other flights" section and expects every
        # entry there to be a flight.
        payload = google_payload(best=[], other=PAYLOAD[3][0][:2])
        theirs = parse(google_html(payload))
        ours = cf.parse_google_html(google_html(payload))
        self.assertEqual(len(theirs), len(ours))
        for t, o in zip(theirs, ours):
            self.assertEqual(t.price, o.price)
            self.assertEqual(t.airlines, list(o.airlines))
            for tl, ol in zip(t.flights, o.legs):
                self.assertEqual((tl.from_airport.code, tl.to_airport.code), (ol.origin, ol.destination))
                y, m, d = tl.departure.date
                hh, mm = tl.departure.time
                self.assertEqual(f"{y:04d}-{m:02d}-{d:02d} {hh:02d}:{mm:02d}", ol.departs)

    def test_page_without_data_is_an_error(self):
        with self.assertRaises(cf.FetchError):
            cf.parse_google_html("<html><body>Before you continue to Google</body></html>")

    def test_error_status_means_no_flights(self):
        html = "<script class=\"ds:1\">AF_initDataCallback({key: 'ds:1', data:null, errorHasStatus: true, x: 1});</script>"
        self.assertEqual(cf.parse_google_html(html), [])


class SerpApiParsingTest(unittest.TestCase):
    def test_parses_options(self):
        data = {
            "best_flights": [
                {
                    "flights": [
                        {
                            "departure_airport": {"id": "SGN", "time": "2026-12-18 6:05"},
                            "arrival_airport": {"id": "DLI", "time": "2026-12-18 7:00"},
                            "airline": "Vietjet",
                            "flight_number": "VJ 360",
                        }
                    ],
                    "price": 45,
                }
            ],
            "other_flights": [
                {
                    "flights": [
                        {
                            "departure_airport": {"id": "SGN", "time": "2026-12-18 13:15"},
                            "arrival_airport": {"id": "DLI", "time": "2026-12-18 14:10"},
                            "airline": "Vietnam Airlines",
                            "flight_number": "VN 1587",
                        }
                    ],
                    "price": 60,
                }
            ],
        }
        flights = cf.parse_serpapi(data)
        self.assertEqual([f.legs[0].flight_number for f in flights], ["VJ 360", "VN 1587"])
        self.assertEqual(flights[0].legs[0].departs, "2026-12-18 06:05")
        self.assertEqual(flights[0].key, itinerary("06:05", "07:00").key)

    def test_no_results_is_empty(self):
        self.assertEqual(cf.parse_serpapi({"error": "Google Flights hasn't returned any results for this query."}), [])

    def test_other_errors_raise(self):
        with self.assertRaises(cf.FetchError):
            cf.parse_serpapi({"error": "Invalid API key."})


class SelectTest(unittest.TestCase):
    def test_filters_and_dedupes(self):
        flights = cf.parse_google_html(google_html(PAYLOAD))
        cheap_copy = cf.Itinerary(legs=flights[0].legs, airlines=flights[0].airlines, price=999)
        wrong_day = itinerary("06:05", "07:00", date="2026-12-19")
        wrong_route = itinerary("06:05", "07:00", to="HAN")

        nonstop = cf.select(flights + [cheap_copy, wrong_day, wrong_route], "SGN", "DLI", "2026-12-18", 0)
        self.assertEqual([f.legs[0].departs[-5:] for f in nonstop], ["00:31", "06:05"])
        self.assertEqual(nonstop[1].price, 999)

        anything = cf.select(flights, "SGN", "DLI", "2026-12-18", None)
        self.assertEqual(len(anything), 3)


class CompareTest(unittest.TestCase):
    def test_new_gone_and_back(self):
        a, b, c = itinerary("06:05", "07:00"), itinerary("09:00", "10:00"), itinerary("12:00", "13:00")

        changes, flights = cf.compare({}, [a, b], "2026-10-06")
        self.assertEqual(changes.new, [a, b])

        changes, flights = cf.compare(flights, [a, c], "2026-10-07")
        self.assertEqual(changes.new, [c])
        self.assertEqual(changes.returned, [])
        self.assertEqual([g["legs"][0]["departs"] for g in changes.gone], ["2026-12-18 09:00"])
        self.assertTrue(changes.alert)
        self.assertFalse(flights[b.key]["listed"])
        self.assertEqual(flights[b.key]["unlisted_on"], "2026-10-07")

        changes, flights = cf.compare(flights, [a, c], "2026-10-08")
        self.assertFalse(changes.any)

        changes, flights = cf.compare(flights, [a, b, c], "2026-10-09")
        self.assertEqual(changes.returned, [b])
        self.assertEqual(changes.new, [])
        self.assertTrue(flights[b.key]["listed"])
        self.assertNotIn("unlisted_on", flights[b.key])
        self.assertEqual(flights[b.key]["first_seen"], "2026-10-06")

    def test_price_change_is_not_a_change(self):
        a = itinerary("06:05", "07:00", price=100)
        _, flights = cf.compare({}, [a], "2026-10-06")
        changes, _ = cf.compare(flights, [itinerary("06:05", "07:00", price=80)], "2026-10-07")
        self.assertFalse(changes.any)


class MainTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.state = self.dir / "state.json"
        self.output = self.dir / "github_output"
        self.report = self.dir / "report.md"
        env = mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(self.output)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("SERPAPI_API_KEY", None)

    def run_main(self, flights, today="2026-10-06"):
        self.output.write_text("")
        with mock.patch.object(cf, "fetch_google", return_value=flights), mock.patch.object(
            cf, "local_today", return_value=dt.date.fromisoformat(today)
        ):
            with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                code = cf.main(["--state-file", str(self.state), "--report-file", str(self.report)])
        outputs = dict(line.split("=", 1) for line in self.output.read_text().splitlines())
        return code, outputs

    def test_baseline_then_new_flight(self):
        a, b = itinerary("06:05", "07:00"), itinerary("09:00", "10:00")

        code, out = self.run_main([a])
        self.assertEqual(code, 0)
        self.assertEqual(out["alert"], "false")
        self.assertEqual(out["state_changed"], "true")
        self.assertIn("First run", self.report.read_text())

        code, out = self.run_main([a], today="2026-10-07")
        self.assertEqual((out["alert"], out["state_changed"]), ("false", "false"))

        code, out = self.run_main([a, b], today="2026-10-08")
        self.assertEqual(out["alert"], "true")
        self.assertEqual(out["state_changed"], "true")
        self.assertIn("1 new", out["title"])
        report = self.report.read_text()
        self.assertIn("1 new flight", report)
        self.assertIn("| 09:00 | 10:00 | Vietjet |", report)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["flights"][b.key]["first_seen"], "2026-10-08")

    def test_keepalive_rewrites_state_weekly(self):
        a = itinerary("06:05", "07:00")
        self.run_main([a], today="2026-10-06")
        _, out = self.run_main([a], today="2026-10-12")
        self.assertEqual(out["state_changed"], "false")
        _, out = self.run_main([a], today="2026-10-13")
        self.assertEqual(out["state_changed"], "true")
        self.assertEqual(json.loads(self.state.read_text())["last_saved"], "2026-10-13")

    def test_no_flights_is_a_failure_and_keeps_state(self):
        self.run_main([itinerary("06:05", "07:00")])
        before = self.state.read_text()
        code, out = self.run_main([], today="2026-10-07")
        self.assertEqual(code, 1)
        self.assertEqual(out, {})
        self.assertEqual(self.state.read_text(), before)

    def test_after_the_date(self):
        code, out = self.run_main([itinerary("06:05", "07:00")], today="2026-12-19")
        self.assertEqual(code, 0)
        self.assertEqual(out["expired"], "true")
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
