from __future__ import annotations

from datetime import date

from broker_alpaca import LIVE_TRADING_MESSAGE, MockBroker
from notifier import NullNotifier
from run import is_month_end, main, run_rebalance


def test_main_dry_run_works_without_keys(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    # Point at the repo config and a throwaway db.
    repo = __import__("pathlib").Path(__file__).resolve().parents[1]
    monkeypatch.setenv("KILL_SWITCH", "")
    rc = main(["--dry-run", "--config", str(repo / "config.yaml"), "--force"])
    # run.py writes cfg.db_path relative to cwd; that's fine.
    assert rc == 0
    out = capsys.readouterr().out
    assert "mock broker" in out.lower()
    assert "QQQ" in out
    assert "prior_20d_high" in out


def test_main_rejects_live_flag(capsys):
    rc = main(["--live"])
    assert rc == 2
    assert "explicit owner approval" in capsys.readouterr().err


def test_main_rejects_live_env(monkeypatch, capsys):
    monkeypatch.setenv("APCA_API_BASE_URL", "https://api.alpaca.markets")
    rc = main(["--dry-run"])
    assert rc == 2
    assert LIVE_TRADING_MESSAGE in capsys.readouterr().err


def test_submit_without_paper_is_rejected(capsys):
    rc = main(["--submit"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "requires --paper" in err or "owner approval" in err


def test_run_blocks_on_kill_switch(cfg, store, tmp_path):
    ks = tmp_path / ".killswitch"
    ks.write_text("on")
    # rebuild cfg with this kill file
    from dataclasses import replace

    cfg = replace(cfg, risk=replace(cfg.risk, kill_switch_file=str(ks)))
    result = run_rebalance(cfg, MockBroker(), store, NullNotifier(), dry_run=False, force=True, env={})
    assert result["action"] == "blocked_kill_switch"
    assert store.turnover_today(result["as_of"]) == 0


def test_run_does_not_submit_in_dry_run(cfg, store, drifted_positions):
    broker = MockBroker(
        cash=1_000.0,
        equity=100_000.0,
        positions=drifted_positions,
        prices={s: p.current_price for s, p in drifted_positions.items()},
    )
    result = run_rebalance(cfg, broker, store, NullNotifier(), dry_run=True, force=True, env={})
    assert result["action"] == "dry_run"
    assert result["orders"]
    assert broker.submitted == []


def test_run_submits_on_paper_mock_when_not_dry_run(cfg, store, drifted_positions):
    broker = MockBroker(
        cash=1_000.0,
        equity=100_000.0,
        positions=drifted_positions,
        prices={s: p.current_price for s, p in drifted_positions.items()},
    )
    result = run_rebalance(cfg, broker, store, NullNotifier(), dry_run=False, force=True, env={})
    assert result["action"] == "submitted"
    assert len(broker.submitted) == len(result["orders"])
    assert all(rec["client_order_id"].startswith("reb-") for rec in broker.submitted)


def test_run_trend_filter_orders_100_pct_bnd(cfg, store, drifted_positions, closes_below_sma):
    prices = {s: p.current_price for s, p in drifted_positions.items()}
    prices["VTI"] = float(closes_below_sma["VTI"].iloc[-1])
    broker = MockBroker(
        cash=1_000.0,
        equity=100_000.0,
        positions=drifted_positions,
        prices=prices,
        daily_closes=closes_below_sma,
    )
    result = run_rebalance(cfg, broker, store, NullNotifier(), dry_run=True, force=True, env={})
    assert result["decision"]["reason"] == "trend_filter_risk_off"
    assert result["decision"]["target_weights"]["BND"] == 1.0
    sides = {o["symbol"]: o["side"] for o in result["orders"]}
    assert sides.get("VTI") == "sell"
    assert sides.get("BND") == "buy"


def test_is_month_end_calendar():
    assert is_month_end(date(2026, 9, 30))
    assert not is_month_end(date(2026, 9, 28))
    trading = [date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30)]
    assert is_month_end(date(2026, 9, 30), trading)
    assert not is_month_end(date(2026, 9, 29), trading)


def test_within_band_no_orders(cfg, store, on_target_positions):
    broker = MockBroker(
        cash=1_000.0,
        equity=100_000.0,
        positions=on_target_positions,
        prices={s: p.current_price for s, p in on_target_positions.items()},
    )
    result = run_rebalance(cfg, broker, store, NullNotifier(), dry_run=True, force=True, env={})
    assert result["action"] == "within_band"
    assert result["orders"] == []
