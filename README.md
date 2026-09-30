# StockBot

Personal, low-turnover, rules-based ETF rebalancer for **Alpaca paper trading only**.

There is no live-trading path. The process refuses to start unless the API base URL is `https://paper-api.alpaca.markets`. Any live URL or `--live` flag exits with: *Live trading needs explicit owner approval.*

This automates discipline (bands, a slow trend filter, risk caps), not alpha. See the research notes that scoped the project: a 55/25/20 VTI/VXUS/BND mix, month-end 5 percentage-point bands, and an optional 200-day SMA filter into 100% BND.

## Strategy

Configured in [`config.yaml`](config.yaml):

| Rule | Default |
|---|---|
| Target mix | VTI 55% / VXUS 25% / BND 20% |
| Calendar | Month-end check |
| Bands | If any fund is more than **5 percentage points** from target, rebalance the whole book back to target |
| Trend filter | **On**. If VTI closes below its 200-day SMA at the monthly check, hold 100% BND until a later monthly check closes above the SMA |
| Cash buffer | 1% uninvested |
| Min trade | $50 notional — tiny drifts do not trade |

Turn the trend filter off with a single flag:

```yaml
trend_filter:
  enabled: false
```

## Safety

- **Paper only.** `broker_alpaca.assert_paper_only()` rejects every non-paper base URL before an SDK client is built. `paper=True` and the paper URL are forced.
- **Secrets from the environment only.** `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`. Never commit a `.env`.
- **Reconcile** against broker positions before ordering. The broker is source of truth.
- **Risk gates** in `risk.py`: max order notional, max daily turnover (buy+sell), kill switch via `.killswitch` or `KILL_SWITCH=1`.
- **Idempotent** `client_order_id` values so a retry of the same day/symbol/side/size does not double-send.
- **Retries with backoff** on 429 / 5xx.
- **Default `--dry-run`**: print the plan, persist it as `dry_run` rows, submit nothing.

## Setup

Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### Alpaca paper account

1. Create an account at [Alpaca](https://alpaca.markets) and open the **Paper Trading** dashboard ([docs](https://docs.alpaca.markets/us/docs/paper-trading)).
2. Generate paper API keys (the paper account starts with $100k of buying power).
3. Put only the paper keys in `.env`:

```
APCA_API_KEY_ID=...
APCA_API_SECRET_KEY=...
APCA_API_BASE_URL=https://paper-api.alpaca.markets
```

Paper and live use the same API shape with different keys and hostnames. This repo will not talk to `https://api.alpaca.markets`.

## Dry-run (no keys)

Works offline against an in-memory mock broker with a deliberately drifted 70/15/15 book:

```bash
python run.py --dry-run
```

You should see planned sell/buy notionals and **no** broker calls.

Paper dry-run (keys required, still sends nothing):

```bash
python run.py --paper --dry-run
```

Submit to the paper account (still blocked if the kill switch is on or the URL is not paper):

```bash
python run.py --paper --submit
```

`--force` runs the check on a non-month-end day. Dry-run implies `--force` so a local demo always prints a plan.

## Backtest

The engine in `backtest.py` is the research backtester (buy-and-hold vs 5pp bands vs bands + SMA200, 5 bps slippage, T+1 execution). Cached adjusted closes live under `tests/fixtures/data_cache/` so pytest stays offline.

```bash
python backtest.py --universe etf --offline --cache-dir tests/fixtures/data_cache --out-dir /tmp/bt
```

ETF rows should match `tests/fixtures/expected_results.csv` within rounding (the attached research run: 2011-01-28 through 2026-09-28).

Refresh prices (needs network):

```bash
python backtest.py --universe etf --refresh
```

## Weekly report

`run.py` snapshots portfolio equity and a buy-and-hold of the **same mix**, started the same day, into SQLite (`data/trader.db` by default).

```bash
python report.py
```

Prints since-inception and last-week return versus that benchmark.

## Kill switch and alerts

```bash
# either
echo 1 > .killswitch
# or
export KILL_SWITCH=1
```

Both block every order. Optional alerts (no-op if unset):

- `DISCORD_WEBHOOK_URL` — fills, errors, kill switch, drift past the band
- `NOTIFY_EMAIL_TO` + `SMTP_HOST` (+ `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM`)

## Scheduler

This strategy does not need an always-on server.

- Cron line: [`scheduler/crontab.example`](scheduler/crontab.example)
- GitHub Actions: [`.github/workflows/rebalance.yml`](.github/workflows/rebalance.yml) — **manual `workflow_dispatch` only**. The cron schedule is commented out. Do not enable it until paper keys are stored as Actions secrets.

## Tests

Offline, mocked broker, no API keys:

```bash
pytest
```

Coverage includes order diffing, 5pp band logic, the 200-day filter, the paper-only guard, the kill switch, dry-run, and the ETF backtest vs the attached results.

## Layout

```
config.yaml        # mix, band, trend flag, cash buffer, risk caps
strategy.py        # target weights + SMA filter
rebalance.py       # current vs target → order intents
broker_alpaca.py   # paper-only wrapper (account, positions, submit/cancel, retries)
risk.py            # size / turnover / kill switch
store.py           # SQLite: orders, fills, targets, equity, B&H benchmark
notifier.py        # Discord / email / no-op
backtest.py        # research engine
run.py             # cron/Actions entrypoint
report.py          # weekly summary
```

## What you still need to wire up

- Alpaca **paper** keys in `.env` (or Actions secrets) before `--paper` or `--submit` will talk to the API
- Optional Discord webhook or SMTP for alerts
- A host for the cron line, or enable the commented `schedule:` in the Actions workflow after secrets are set
- Several paper months before even *considering* live trading — and live trading is intentionally not implemented
