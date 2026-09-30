# StockBot

Personal, paper-only trading bot. The **live (paper) default** is a short-term **QQQ momentum** rule with a **$100 capital cap**. The original VTI/VXUS/BND month-end rebalancer is still in the repo and selectable.

There is no live-trading path. The process refuses to start unless the API base URL is `https://paper-api.alpaca.markets`. Any live URL or `--live` flag exits with: *Live trading needs explicit owner approval.*

## Default strategy: QQQ momentum

Configured in [`config.yaml`](config.yaml) (`strategy: qqq_momentum`):

| Rule | Default |
|---|---|
| Universe | QQQ only |
| When | Once per regular trading day, near the close (~3:50 PM America/New_York) |
| Entry | If flat and today's close (last print as proxy) **> highest close of the prior 20 trading days**, buy QQQ |
| Size | `capital_cap_usd + realized sleeve P&L` (starts at **$100**; **gains compound**; the paper account's leftover ~$99,900 is ignored) |
| Exit | Sell all if the close is **5% or more below the highest close since entry**, or after **20 trading days** held |
| Orders | Fractional notional (buy) / qty (sell), market, **$1 minimum** |
| Data | Alpaca daily bars when the paper key can fetch them; otherwise yfinance. The run log records `price_source` |

The GitHub Actions schedule fires at **both** 19:50 UTC (EDT) and 20:50 UTC (EST). The bot converts to Eastern time, asks Alpaca's clock/calendar, and skips weekends, holidays, the DST hour that is not 15:50 ET, and a second fire the same session (`last_live_session`). Early-close days (session close before 15:30 ET) are **skipped** by those 15:50 ET crons; if a run happens to land in the window just before the early close, it evaluates then instead. The skip reason is written to `reports/latest.json`.

## Capital cap

```yaml
capital_cap_usd: 100.0
# Tradable cash = cap + realized P&L of this bot (gains compound).
```

Risk checks never put more than that tradable amount in the market. Max daily sleeve loss and the kill switch (`.killswitch` or `KILL_SWITCH=1`) still apply.

## Optional strategy: ETF rebalancer

Set `strategy: etf_rebalance` to use the original month-end 55/25/20 VTI/VXUS/BND book with 5pp bands and the optional 200-day VTI SMA → 100% BND filter. That code path is unchanged; it is just no longer the default.

## Safety

- **Paper only.** `broker_alpaca.assert_paper_only()` rejects every non-paper base URL before an SDK client is built. `paper=True` and the paper URL are forced.
- **Secrets from the environment only.** `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`. Never commit a `.env`.
- **Risk gates** in `risk.py`: kill switch, $1 min, never more than cap+sleeve P&L, max daily sleeve loss.
- **Idempotent** `client_order_id` values so a retry of the same day/symbol/side/size does not double-send.
- **Retries with backoff** on 429 / 5xx.
- **Default `--dry-run`**: print the plan, persist it as `dry_run` rows, **submit nothing**. A dry-run does **not** open a live position and does **not** start the $100 QQQ buy-and-hold benchmark. The first `--paper --submit` run does.

## Setup

Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### Alpaca paper account

1. Create an account at [Alpaca](https://alpaca.markets) and open the **Paper Trading** dashboard.
2. Generate paper API keys (the paper account starts with $100k of buying power; this bot only uses $100 plus its own P&L).
3. Put only the paper keys in `.env`:

```
APCA_API_KEY_ID=...
APCA_API_SECRET_KEY=...
APCA_API_BASE_URL=https://paper-api.alpaca.markets
```

## Dry-run (no keys)

Works offline against an in-memory mock broker:

```bash
python run.py --dry-run
```

Paper dry-run (keys required, still sends nothing):

```bash
python run.py --paper --dry-run
```

Submit to the paper account is still blocked unless you pass `--paper --submit`, the kill switch is off, and the URL is the paper host. The GitHub Actions workflow **never** submits.

`--force` skips the near-close / calendar gates (and the ETF month-end gate). Dry-run implies `--force` so a local demo always prints a plan.

## Backtest

```bash
python backtest_momentum.py --offline --cache-dir tests/fixtures/data_cache
```

This is the round-one “tuned” rule: 20-day high, 5% trail, 20-day max hold, QQQ from 2005-01-03 through 2026-09-30, 2.5 bps per side, $100 start. Research ballpark was ~8.9% CAGR / $100 → ~$639. The command prints **this engine’s actual number**.

The original ETF research engine is still `python backtest.py --universe etf --offline`.

## Weekly report

`run.py` snapshots the **bot sleeve** and a virtual **$100 QQQ buy-and-hold** started on the bot’s first run day into SQLite (`data/trader.db`).

```bash
python report.py
```

Prints weekly and since-inception bot vs hold, plus closed-trade count, wins, and win rate.

## State persistence (GitHub Actions)

Runners are ephemeral, so `data/trader.db` is restored and saved every paper job:

1. **`bot-state` git branch** (source of truth). The workflow checks out `origin/bot-state:data/trader.db` before the run and force-pushes the updated file after a successful dry-run. This survives cache eviction and is the most robust option.
2. **Actions cache** (`trader-db-*`) as a fast path if the branch is missing.
3. **Artifact** `trader-db` retained 30 days as a backup.

Dry-runs write snapshots, signals, and `dry_run` order rows. They do **not** invent a live open position, so a later `--submit` will not think it already bought.

Locally, `data/*.db` stays gitignored on feature branches.

## Kill switch and alerts

```bash
echo 1 > .killswitch
# or
export KILL_SWITCH=1
```

Optional alerts (no-op if unset): `DISCORD_WEBHOOK_URL`, or `NOTIFY_EMAIL_TO` + SMTP vars (see `.env.example`).

## Scheduler

- Cron: [`scheduler/crontab.example`](scheduler/crontab.example) — dual UTC hours or `CRON_TZ=America/New_York`.
- GitHub Actions: [`.github/workflows/rebalance.yml`](.github/workflows/rebalance.yml)
  - **Schedule** (weekdays 19:50 and 20:50 UTC): `python run.py --paper --submit`. Paper orders only. The session gate allows one trade per regular session.
  - **`workflow_dispatch`**: defaults to **dry-run**. Choose `submit` to force a paper order off-hours.
  - No `push` trigger. Pushes do not trade.

After each successful run the `bot-state` branch gets `data/trader.db`, `reports/latest.json`, and `reports/weekly.md`. Fetch without opening SQLite:

```
https://raw.githubusercontent.com/the209bbq/StockBot/bot-state/reports/latest.json
https://raw.githubusercontent.com/the209bbq/StockBot/bot-state/reports/weekly.md
```

A failed run fails the Actions job and does **not** persist new state.

## Tests

Offline, mocked broker, no API keys:

```bash
pytest
```

Coverage includes the QQQ entry signal, trailing stop, time exit, cap sizing, paper-only guard, kill switch, dry-run (no stored position / no benchmark), mocked submit, the once-per-session gate, and the offline QQQ backtest.

## Layout

```
config.yaml           # strategy selector, $100 cap, QQQ rule, leftover ETF mix
momentum.py           # Donchian entry / trail / time-stop
run_momentum.py       # daily paper runner
market_clock.py       # 15:50 ET window, holidays, early closes, DST
backtest_momentum.py  # 2005–2026 QQQ tuned backtest
strategy.py / rebalance.py / backtest.py   # original ETF path (non-default)
broker_alpaca.py      # paper-only wrapper + clock/calendar
risk.py               # kill switch, $1 min, cap+P&L, daily loss
store.py              # SQLite: orders, sleeve, QQQ B&H, closed trades
run.py                # dispatches on config.strategy
report.py             # weekly bot vs hold + win rate
```
