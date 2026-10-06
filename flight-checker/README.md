# Flight checker: Ho Chi Minh City → Da Lat, 18 Dec 2026

Checks nonstop flights from **SGN** (Tan Son Nhat) to **DLI** (Lien Khuong) on
**18 December 2026** once a day, and tells you when a flight shows up that wasn't
listed the day before.

Each run compares today's flights with the ones it saw on earlier runs and reports:

- **New flights**: never seen before (for example, an added schedule or a new airline).
- **Back on sale**: listed before, then gone, now listed again (seats freed up).
- **No longer listed**: listed last time and gone now. Usually sold out or cancelled.

Price changes alone don't count as a change.

## How it runs

The [`Flight checker` workflow](../.github/workflows/flight-checker.yml) runs on
GitHub Actions every day at 07:43 Vietnam time (00:43 UTC):

1. It fetches the flight list and compares it with
   `state/SGN-DLI-2026-12-18.json`.
2. If anything is new or back on sale, it **opens a GitHub issue** with the
   details and mentions you, so you get an email and an app notification.
3. It commits the updated state file. When nothing changes, it still commits
   once a week. GitHub turns off scheduled workflows in public repos after 60
   days without a commit.
4. After 18 Dec it turns itself off.

The first run saves a baseline and doesn't open an issue. Every run's report
is in the run's summary page under the **Actions** tab. To run a check now,
open **Actions → Flight checker → Run workflow**.

If a run can't fetch flights (Google blocks the request or changes its page),
the run fails, GitHub emails you about the failed run, and the state file is
left alone.

## Where the data comes from

By default, the checker reads Google Flights directly, using the
[`fast-flights`](https://github.com/AWeirdDev/flights) scraper. This needs no
account, but scraping can break when Google changes things.

For a more reliable source that also gives flight numbers, sign up at
[SerpApi](https://serpapi.com/). The free plan covers one search a day. Then
add the key as a repository secret named `SERPAPI_API_KEY`
(**Settings → Secrets and variables → Actions → New repository secret**). The
workflow uses SerpApi automatically when the secret is set.

When you switch sources, the first SerpApi run can report every flight once as
new.

## Run it on your own computer

macOS or Linux:

```sh
cd flight-checker
pip install -r requirements.txt
python check_flights.py
```

Windows PowerShell (needs [Python 3.10 or later](https://www.python.org/downloads/)):

```powershell
cd flight-checker
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe check_flights.py
```

After the first time, only the last line is needed.

Local runs update your own copy of the state file. Before you `git pull`
later, discard those changes with `git checkout -- state`.

Options:

| Option | Default | |
|---|---|---|
| `--origin`, `--destination` | `SGN`, `DLI` | IATA airport codes |
| `--date` | `2026-12-18` | departure date |
| `--max-stops` | `0` | `1` also includes one-stop connections; `-1` means no limit |
| `--currency` | `USD` | for example, `VND` |
| `--source` | `auto` | `google`, or `serpapi` (needs `SERPAPI_API_KEY`) |
| `--state-file` | `state/<route>-<date>.json` | where seen flights are stored |
| `--report-file` | | also write the Markdown report to this file |

To check a different route or date, change the arguments in the workflow's
`Check flights` step. Each route and date gets its own state file.

## Tests

```sh
cd flight-checker
pip install -r requirements.txt
python -m unittest -v
```
