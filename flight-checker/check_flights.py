#!/usr/bin/env python3
"""Check one route on one date for flights that weren't listed last time.

Each run fetches the flights currently on sale, compares them with the state
file written by earlier runs, and reports:

  * new flights      - never seen before
  * back on sale     - seen before, missing last time, listed again now
  * no longer listed - listed last time, missing now (sold out or cancelled)

Flight data comes from Google Flights, scraped with the `fast-flights` package.
If SERPAPI_API_KEY is set, SerpApi's Google Flights API is used instead, which
is more reliable and also reports flight numbers.

Exit status is 0 when the check ran (whether or not anything changed) and 1
when the flights could not be fetched; the state file is only touched on 0.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ORIGIN = "SGN"  # Tan Son Nhat, Ho Chi Minh City
DEFAULT_DESTINATION = "DLI"  # Lien Khuong, Da Lat
DEFAULT_DATE = "2026-12-18"

# Both airports are in Vietnam, so "today" and the expiry check use local time.
LOCAL_TZ = dt.timezone(dt.timedelta(hours=7), "ICT")

# Rewrite the state file at least this often even when nothing changed. GitHub
# disables scheduled workflows in public repos after 60 days without a commit.
KEEPALIVE_DAYS = 7

HERE = Path(__file__).resolve().parent


class FetchError(Exception):
    """The flight list could not be fetched or understood."""


@dataclass(frozen=True)
class Leg:
    origin: str
    destination: str
    departs: str  # "YYYY-MM-DD HH:MM", local time at the origin
    arrives: str  # "YYYY-MM-DD HH:MM", local time at the destination
    airline: str = ""
    flight_number: str = ""


@dataclass(frozen=True)
class Itinerary:
    legs: tuple[Leg, ...]
    airlines: tuple[str, ...]
    price: int | None = field(default=None, compare=False)

    @property
    def stops(self) -> int:
        return len(self.legs) - 1

    @property
    def key(self) -> str:
        """Stable identity for an itinerary: route, times and airline."""
        legs = " / ".join(
            f"{leg.origin} {leg.departs} -> {leg.destination} {leg.arrives}"
            for leg in self.legs
        )
        return f"{legs} | {', '.join(self.airlines)}"

    def to_record(self) -> dict:
        return {
            "airlines": list(self.airlines),
            "legs": [
                {
                    "from": leg.origin,
                    "to": leg.destination,
                    "departs": leg.departs,
                    "arrives": leg.arrives,
                    "flight_number": leg.flight_number,
                }
                for leg in self.legs
            ],
        }


@dataclass
class Changes:
    new: list[Itinerary]
    returned: list[Itinerary]
    gone: list[dict]

    @property
    def alert(self) -> bool:
        return bool(self.new or self.returned)

    @property
    def any(self) -> bool:
        return bool(self.new or self.returned or self.gone)


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


def normalize_datetime(value: str) -> str:
    """Turn "2026-12-18 6:05" into "2026-12-18 06:05"."""
    date, time = value.strip().split(" ", 1)
    hour, minute = time.split(":")[:2]
    return f"{date} {int(hour):02d}:{int(minute):02d}"


def fetch_google(origin: str, destination: str, date: str, currency: str) -> list[Itinerary]:
    from fast_flights import FlightQuery, Passengers, create_query, fetch_flights_html

    # max_stops is filtered locally instead of in the query, so a change in how
    # Google interprets the stops filter can't silently hide flights.
    query = create_query(
        flights=[FlightQuery(date=date, from_airport=origin, to_airport=destination)],
        trip="one-way",
        seat="economy",
        passengers=Passengers(adults=1),
        language="en-US",
        currency=currency,
    )
    try:
        html = fetch_flights_html(query)
    except Exception as exc:  # primp raises its own exception types
        raise FetchError(f"Google Flights request failed: {exc}") from exc
    return parse_google_html(html)


def parse_google_html(html: str) -> list[Itinerary]:
    from selectolax.lexbor import LexborHTMLParser

    script = LexborHTMLParser(html).css_first(r"script.ds\:1")
    if script is None:
        raise FetchError(
            "Google Flights returned a page without flight data "
            "(blocked, consent page, or the page layout changed)"
        )
    js = script.text()
    if "data:" not in js:
        raise FetchError("Google Flights flight data block is empty")
    data = js.split("data:", 1)[1].rsplit(",", 1)[0]
    if data.endswith("errorHasStatus: true"):
        return []
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise FetchError(f"could not decode Google Flights data: {exc}") from exc
    return parse_google_payload(payload)


def parse_google_payload(payload: list) -> list[Itinerary]:
    """Read itineraries from Google Flights' embedded results data.

    Same field positions as fast-flights' parser, but it reads both the
    "best flights" (index 2) and "other flights" (index 3) sections, while
    fast-flights only reads the latter.
    """
    itineraries = []
    for section in (2, 3):
        try:
            items = payload[section][0]
        except (IndexError, TypeError):
            continue
        for item in items or []:
            try:
                itineraries.append(_google_itinerary(item))
            except (IndexError, TypeError, ValueError):
                continue  # not a flight entry
    return itineraries


def _google_itinerary(item: list) -> Itinerary:
    flight = item[0]
    legs = tuple(
        Leg(
            origin=_airport_code(leg[3]),
            destination=_airport_code(leg[6]),
            departs=_google_datetime(leg[20], leg[8]),
            arrives=_google_datetime(leg[21], leg[10]),
        )
        for leg in flight[2]
    )
    if not legs:
        raise ValueError("itinerary without legs")
    try:
        price = int(item[1][0][1])
    except (IndexError, TypeError, ValueError):
        price = None
    return Itinerary(legs=legs, airlines=tuple(str(a) for a in flight[1]), price=price)


def _airport_code(value: object) -> str:
    if not (isinstance(value, str) and len(value) == 3 and value.isalpha()):
        raise ValueError(f"not an airport code: {value!r}")
    return value


def _google_datetime(date: list[int], time: list[int | None] | None) -> str:
    year, month, day = date
    # Google drops zero components: [8] is 08:00 and [None, 31] is 00:31.
    hour, minute = [*(time or []), None, None][:2]
    return f"{year:04d}-{month:02d}-{day:02d} {hour or 0:02d}:{minute or 0:02d}"


def fetch_serpapi(
    origin: str, destination: str, date: str, currency: str, max_stops: int | None, api_key: str
) -> list[Itinerary]:
    params = {
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": date,
        "type": "2",  # one way
        "currency": currency,
        "hl": "en",
        "api_key": api_key,
    }
    if max_stops is not None and max_stops <= 2:
        params["stops"] = str(max_stops + 1)  # 1 = nonstop, 2 = up to 1 stop, ...
    url = "https://serpapi.com/search.json?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=90) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.load(exc).get("error", exc.reason)
        except (ValueError, AttributeError):
            detail = exc.reason
        raise FetchError(f"SerpApi request failed ({exc.code}): {detail}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise FetchError(f"SerpApi request failed: {exc}") from exc
    return parse_serpapi(data)


def parse_serpapi(data: dict) -> list[Itinerary]:
    options = data.get("best_flights", []) + data.get("other_flights", [])
    if not options and data.get("error"):
        if "hasn't returned any results" in data["error"]:
            return []
        raise FetchError(f"SerpApi error: {data['error']}")
    itineraries = []
    for option in options:
        legs = tuple(
            Leg(
                origin=seg["departure_airport"]["id"],
                destination=seg["arrival_airport"]["id"],
                departs=normalize_datetime(seg["departure_airport"]["time"]),
                arrives=normalize_datetime(seg["arrival_airport"]["time"]),
                airline=seg.get("airline", ""),
                flight_number=seg.get("flight_number", ""),
            )
            for seg in option["flights"]
        )
        airlines = tuple(dict.fromkeys(leg.airline for leg in legs if leg.airline))
        itineraries.append(Itinerary(legs=legs, airlines=airlines, price=option.get("price")))
    return itineraries


def select(
    itineraries: list[Itinerary], origin: str, destination: str, date: str, max_stops: int | None
) -> list[Itinerary]:
    """Keep matching itineraries, one per key (the cheapest), sorted by departure."""
    best: dict[str, Itinerary] = {}
    for it in itineraries:
        if it.legs[0].origin != origin or it.legs[-1].destination != destination:
            continue
        if not it.legs[0].departs.startswith(date):
            continue
        if max_stops is not None and it.stops > max_stops:
            continue
        current = best.get(it.key)
        if current is None or (it.price is not None and (current.price is None or it.price < current.price)):
            best[it.key] = it
    return sorted(best.values(), key=lambda it: (it.legs[0].departs, it.legs[-1].arrives, it.key))


# --------------------------------------------------------------------------
# Comparing with earlier runs
# --------------------------------------------------------------------------


def compare(previous: dict[str, dict], current: list[Itinerary], today: str) -> tuple[Changes, dict[str, dict]]:
    """Diff the current listing against the stored one.

    Returns the changes and the updated flight records. Records are never
    deleted, so a flight that disappears and comes back is reported as
    "back on sale" rather than "new".
    """
    flights = {key: dict(record) for key, record in previous.items()}
    changes = Changes(new=[], returned=[], gone=[])
    current_keys = set()

    for it in current:
        current_keys.add(it.key)
        record = flights.get(it.key)
        if record is None:
            changes.new.append(it)
            flights[it.key] = {**it.to_record(), "first_seen": today, "listed": True}
        elif not record.get("listed", True):
            changes.returned.append(it)
            record["listed"] = True
            record.pop("unlisted_on", None)

    for key, record in flights.items():
        if key not in current_keys and record.get("listed", True):
            changes.gone.append(record)
            record["listed"] = False
            record["unlisted_on"] = today

    return changes, flights


def load_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")


def needs_keepalive(state: dict | None, today: dt.date) -> bool:
    try:
        last_saved = dt.date.fromisoformat(state["last_saved"])
    except (TypeError, KeyError, ValueError):
        return True
    return (today - last_saved).days >= KEEPALIVE_DAYS


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def google_flights_link(origin: str, destination: str, date: str) -> str:
    q = f"Flights from {origin} to {destination} on {date} one way"
    return "https://www.google.com/travel/flights?" + urllib.parse.urlencode({"q": q, "hl": "en"})


def _time(value: str, date: str) -> str:
    day, time = value.split(" ")
    if day == date:
        return time
    shift = (dt.date.fromisoformat(day) - dt.date.fromisoformat(date)).days
    return f"{time} ({shift:+d}d)"


def _row(record: dict, price: int | None, currency: str, date: str) -> str:
    """One table row from a flight record (see Itinerary.to_record)."""
    legs = record["legs"]
    numbers = ", ".join(leg["flight_number"] for leg in legs if leg.get("flight_number")) or "–"
    if len(legs) == 1:
        stops = "nonstop"
    else:
        stops = f"{len(legs) - 1} stop(s) via " + ", ".join(leg["to"] for leg in legs[:-1])
    price_text = f"{price:,} {currency}" if price is not None else "–"
    return (
        f"| {_time(legs[0]['departs'], date)} | {_time(legs[-1]['arrives'], date)} "
        f"| {', '.join(record['airlines'])} | {numbers} | {stops} | {price_text} |"
    )


TABLE_HEADER = "| Departs | Arrives | Airline | Flight | Stops | Price |\n|---|---|---|---|---|---|"


def _table(rows: list[str]) -> str:
    return "\n".join([TABLE_HEADER, *rows])


def build_report(
    *,
    origin: str,
    destination: str,
    date: str,
    currency: str,
    source: str,
    current: list[Itinerary],
    changes: Changes | None,
) -> str:
    day = dt.date.fromisoformat(date).strftime("%a %d %b %Y")
    lines = [f"## ✈️ {origin} → {destination} on {day}", ""]

    def itinerary_rows(items: list[Itinerary]) -> list[str]:
        return [_row(it.to_record(), it.price, currency, date) for it in items]

    if changes is None:
        lines.append(
            f"First run: recorded **{len(current)}** flights as the baseline. "
            "Future runs will report any flight that isn't in this list."
        )
    elif not changes.any:
        lines.append(f"No changes. **{len(current)}** flights listed, same as last time.")
    else:
        if changes.new:
            lines += [f"### 🆕 {len(changes.new)} new flight(s)", "", _table(itinerary_rows(changes.new)), ""]
        if changes.returned:
            lines += [
                f"### 🔁 {len(changes.returned)} flight(s) back on sale",
                "These were listed before, then disappeared, and are listed again now.",
                "",
                _table(itinerary_rows(changes.returned)),
                "",
            ]
        if changes.gone:
            gone_rows = [_row(record, None, currency, date) for record in changes.gone]
            lines += [
                f"### ❌ {len(changes.gone)} flight(s) no longer listed",
                "Usually sold out or cancelled.",
                "",
                _table(gone_rows),
                "",
            ]

    lines += [
        "",
        f"<details><summary>All {len(current)} flights currently listed</summary>",
        "",
        _table(itinerary_rows(current)),
        "",
        "</details>",
        "",
        f"[Open this search on Google Flights]({google_flights_link(origin, destination, date)}) · "
        f"data: {source} · prices for 1 adult, economy, one way",
    ]
    return "\n".join(lines).strip() + "\n"


def report_title(origin: str, destination: str, date: str, changes: Changes) -> str:
    parts = []
    if changes.new:
        parts.append(f"{len(changes.new)} new")
    if changes.returned:
        parts.append(f"{len(changes.returned)} back on sale")
    day = dt.date.fromisoformat(date).strftime("%d %b")
    return f"✈️ {origin} → {destination} {day}: {' and '.join(parts)} flight(s)"


def set_github_output(**values: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for name, value in values.items():
            f.write(f"{name}={value}\n")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def local_today() -> dt.date:
    return dt.datetime.now(LOCAL_TZ).date()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--origin", default=DEFAULT_ORIGIN, help="origin airport (default: %(default)s)")
    p.add_argument("--destination", default=DEFAULT_DESTINATION, help="destination airport (default: %(default)s)")
    p.add_argument("--date", default=DEFAULT_DATE, help="departure date, YYYY-MM-DD (default: %(default)s)")
    p.add_argument(
        "--max-stops",
        type=int,
        default=0,
        help="ignore itineraries with more stops; -1 for no limit (default: %(default)s, nonstop only)",
    )
    p.add_argument("--currency", default="USD", help="currency for prices (default: %(default)s)")
    p.add_argument(
        "--source",
        choices=["auto", "google", "serpapi"],
        default="auto",
        help="where to get flights; auto uses serpapi when SERPAPI_API_KEY is set (default: %(default)s)",
    )
    p.add_argument("--state-file", type=Path, help="where to remember seen flights (default: state/<route>-<date>.json)")
    p.add_argument("--report-file", type=Path, help="also write the Markdown report here")
    args = p.parse_args(argv)
    args.origin = args.origin.upper()
    args.destination = args.destination.upper()
    dt.date.fromisoformat(args.date)  # validate
    if args.max_stops < 0:
        args.max_stops = None
    if args.state_file is None:
        args.state_file = HERE / "state" / f"{args.origin}-{args.destination}-{args.date}.json"
    return args


def main(argv: list[str] | None = None) -> int:
    # On Windows, redirected output uses the ANSI code page, which can't encode
    # the report's ✈️ and →. Print "?" for those instead of crashing.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    args = parse_args(argv)
    today = local_today()

    if today > dt.date.fromisoformat(args.date):
        print(f"{args.date} has passed; nothing left to check.")
        set_github_output(expired="true", alert="false", state_changed="false")
        return 0

    api_key = os.environ.get("SERPAPI_API_KEY", "")
    source = args.source
    if source == "auto":
        source = "serpapi" if api_key else "google"

    try:
        if source == "serpapi":
            if not api_key:
                raise FetchError("--source serpapi needs the SERPAPI_API_KEY environment variable")
            fetched = fetch_serpapi(args.origin, args.destination, args.date, args.currency, args.max_stops, api_key)
            source_name = "SerpApi (Google Flights)"
        else:
            fetched = fetch_google(args.origin, args.destination, args.date, args.currency)
            source_name = "Google Flights"
        current = select(fetched, args.origin, args.destination, args.date, args.max_stops)
        if not current:
            # A busy domestic route never has zero flights, so treat it as a
            # failed fetch rather than marking every flight as gone.
            raise FetchError(
                f"no matching flights found ({len(fetched)} itineraries fetched before filtering); "
                "not updating the state file"
            )
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    state = load_state(args.state_file)
    if state is None:
        changes = None
        _, flights = compare({}, current, today.isoformat())
    else:
        changes, flights = compare(state.get("flights", {}), current, today.isoformat())

    state_changed = changes is None or changes.any or needs_keepalive(state, today)
    if state_changed:
        save_state(
            args.state_file,
            {
                "route": {"origin": args.origin, "destination": args.destination, "date": args.date},
                "last_saved": today.isoformat(),
                "flights": flights,
            },
        )

    report = build_report(
        origin=args.origin,
        destination=args.destination,
        date=args.date,
        currency=args.currency,
        source=source_name,
        current=current,
        changes=changes,
    )
    print(report)
    if args.report_file:
        args.report_file.write_text(report, encoding="utf-8")

    alert = changes is not None and changes.alert
    set_github_output(
        alert="true" if alert else "false",
        state_changed="true" if state_changed else "false",
        expired="false",
        title=report_title(args.origin, args.destination, args.date, changes) if alert else "",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
