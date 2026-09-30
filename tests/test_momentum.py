from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from broker_alpaca import LIVE_TRADING_MESSAGE, MockBroker
from config import QQQMomentumConfig, load_config
from market_clock import ET, evaluate_session
from momentum import OpenPosition, decide, prior_n_high, tradable_cash, trading_days_held
from notifier import NullNotifier
from rebalance import OrderIntent
from risk import RiskError, check_momentum_risk, scaled_order_cap
from run import main
from run_momentum import run_momentum
from store import Store


def _closes(values: list[float], start="2024-01-02") -> pd.Series:
    idx = pd.bdate_range(start, periods=len(values))
    return pd.Series(values, index=idx, name="QQQ")


def _cfg() -> QQQMomentumConfig:
    return QQQMomentumConfig()


def test_entry_when_close_breaks_prior_20d_high():
    values = [100.0] * 20 + [100.5]
    decision = decide(
        closes=_closes(values),
        as_of=date(2024, 1, 31),
        position=None,
        tradable=100.0,
        cfg=_cfg(),
    )
    assert decision.action == "buy"
    assert decision.reason == "close_above_prior_20d_high"
    assert decision.prior_high == 100.0
    assert decision.close == 100.5
    assert decision.order is not None
    assert decision.order.side == "buy"
    assert decision.order.notional == 100.0


def test_no_entry_when_close_equals_prior_high():
    values = [100.0] * 20 + [100.0]
    decision = decide(
        closes=_closes(values),
        as_of=date(2024, 1, 31),
        position=None,
        tradable=100.0,
        cfg=_cfg(),
    )
    assert decision.action == "hold_flat"
    assert decision.reason == "no_breakout"
    assert decision.order is None


def test_trailing_stop_uses_high_before_updating_peak():
    pos = OpenPosition("QQQ", qty=1.0, entry_date="2024-01-02", entry_price=100.0, high_close=110.0)
    # 5% below 110 is 104.5
    values = [100.0] * 20 + [104.5]
    decision = decide(
        closes=_closes(values),
        as_of=date(2024, 2, 15),
        position=pos,
        tradable=100.0,
        cfg=_cfg(),
        calendar=[d.date() for d in pd.bdate_range("2024-01-02", periods=40)],
    )
    assert decision.action == "sell_trail"
    assert decision.order is not None
    assert decision.order.close_position is True


def test_trailing_stop_not_triggered_above_threshold():
    pos = OpenPosition("QQQ", qty=1.0, entry_date="2024-01-02", entry_price=100.0, high_close=110.0)
    values = [100.0] * 20 + [104.51]
    decision = decide(
        closes=_closes(values),
        as_of=date(2024, 1, 10),
        position=pos,
        tradable=100.0,
        cfg=_cfg(),
        calendar=[d.date() for d in pd.bdate_range("2024-01-02", "2024-01-10")],
    )
    assert decision.action == "hold_long"


def test_time_exit_after_20_trading_days():
    entry = date(2024, 1, 2)
    cal = [d.date() for d in pd.bdate_range(entry, periods=30)]
    as_of = cal[20]  # 20 sessions after entry
    pos = OpenPosition("QQQ", qty=1.0, entry_date=entry.isoformat(), entry_price=100.0, high_close=100.0)
    values = [100.0] * 20 + [101.0]
    decision = decide(
        closes=_closes(values),
        as_of=as_of,
        position=pos,
        tradable=100.0,
        cfg=_cfg(),
        calendar=cal,
    )
    assert trading_days_held(entry, as_of, cal) == 20
    assert decision.action == "sell_time"
    assert decision.days_held == 20


def test_time_exit_not_before_20_days():
    entry = date(2024, 1, 2)
    cal = [d.date() for d in pd.bdate_range(entry, periods=25)]
    as_of = cal[19]
    pos = OpenPosition("QQQ", qty=1.0, entry_date=entry.isoformat(), entry_price=100.0, high_close=100.0)
    decision = decide(
        closes=_closes([100.0] * 20 + [101.0]),
        as_of=as_of,
        position=pos,
        tradable=100.0,
        cfg=_cfg(),
        calendar=cal,
    )
    assert decision.days_held == 19
    assert decision.action == "hold_long"


def test_cap_sizing_uses_cap_plus_realized_pnl_not_account():
    assert tradable_cash(capital_cap_usd=100.0, realized_pnl=25.5) == 125.5
    assert tradable_cash(capital_cap_usd=100.0, realized_pnl=-40.0) == 60.0
    assert tradable_cash(capital_cap_usd=100.0, realized_pnl=-150.0) == 0.0
    decision = decide(
        closes=_closes([100.0] * 20 + [110.0]),
        as_of=date(2024, 1, 31),
        position=None,
        tradable=tradable_cash(capital_cap_usd=100.0, realized_pnl=12.34),
        cfg=_cfg(),
    )
    assert decision.order is not None
    assert decision.order.notional == 112.34
    assert decision.order.notional != 100_000.0


def test_below_min_trade_stays_flat():
    decision = decide(
        closes=_closes([100.0] * 20 + [110.0]),
        as_of=date(2024, 1, 31),
        position=None,
        tradable=0.5,
        cfg=_cfg(),
        min_trade_notional=1.0,
    )
    assert decision.action == "hold_flat"
    assert decision.reason == "below_min_trade"


def test_prior_n_high_excludes_today():
    s = _closes([1, 2, 3, 10])
    assert prior_n_high(s, 3) == 3.0


def test_momentum_kill_switch(cfg, store, tmp_path):
    ks = tmp_path / ".killswitch"
    ks.write_text("on")
    from dataclasses import replace

    cfg = replace(cfg, risk=replace(cfg.risk, kill_switch_file=str(ks)))
    result = run_momentum(
        cfg,
        MockBroker(positions={}),
        store,
        NullNotifier(),
        dry_run=False,
        force=True,
        env={},
        reports_dir=tmp_path / "reports",
    )
    assert result["action"] == "blocked_kill_switch"


def test_check_momentum_risk_kill_switch_env():
    order = OrderIntent("QQQ", "buy", notional=100.0, qty=0.2)
    with pytest.raises(RiskError, match="kill switch"):
        check_momentum_risk(
            order,
            tradable=100.0,
            max_order_notional=100.0,
            max_daily_loss_pct=1.0,
            realized_pnl_today=0.0,
            env={"KILL_SWITCH": "1"},
        )


def test_check_momentum_risk_blocks_buy_above_tradable():
    order = OrderIntent("QQQ", "buy", notional=150.0, qty=0.3)
    with pytest.raises(RiskError, match="cap\\+sleeve"):
        check_momentum_risk(
            order,
            tradable=100.0,
            max_order_notional=200.0,
            max_daily_loss_pct=1.0,
            realized_pnl_today=0.0,
            env={},
        )


def test_check_momentum_risk_daily_loss():
    order = OrderIntent("QQQ", "buy", notional=50.0, qty=0.1)
    with pytest.raises(RiskError, match="daily sleeve loss"):
        check_momentum_risk(
            order,
            tradable=100.0,
            max_order_notional=100.0,
            max_daily_loss_pct=0.10,
            realized_pnl_today=-12.0,
            env={},
        )


def test_scaled_order_cap_never_exceeds_tradable(cfg):
    assert scaled_order_cap(100.0, cfg) == 100.0
    assert scaled_order_cap(40.0, cfg) == 40.0


def test_paper_guard_still_rejects_live():
    from broker_alpaca import assert_paper_only, LiveTradingNotApprovedError

    with pytest.raises(LiveTradingNotApprovedError, match="explicit owner approval"):
        assert_paper_only("https://api.alpaca.markets")
    assert LIVE_TRADING_MESSAGE


def test_run_momentum_dry_run_does_not_open_stored_position(cfg, store, tmp_path):
    closes = _closes([100.0] * 20 + [120.0])
    broker = MockBroker(
        cash=100_000.0,
        equity=100_000.0,
        positions={},
        prices={"QQQ": 120.0},
        daily_closes={"QQQ": closes},
    )
    result = run_momentum(
        cfg, broker, store, NullNotifier(), dry_run=True, force=True, env={}, reports_dir=tmp_path / "reports"
    )
    assert result["action"] == "dry_run"
    assert result["orders_submitted"] is False
    assert result["orders"]
    assert result["orders"][0]["side"] == "buy"
    assert result["orders"][0]["notional"] == 100.0
    assert result["managed"]["tradable"] == 100.0
    assert store.get_open_position() is None
    assert broker.submitted == []
    assert not store.is_live_started()
    assert store.inception_date() is None
    assert store.equity_history() == []
    assert store.benchmark_history() == []


def test_run_momentum_submit_records_position_and_benchmark(cfg, store, tmp_path):
    closes = _closes([100.0] * 20 + [120.0])
    broker = MockBroker(
        cash=100_000.0,
        equity=100_000.0,
        positions={},
        prices={"QQQ": 120.0},
        daily_closes={"QQQ": closes},
    )
    result = run_momentum(
        cfg, broker, store, NullNotifier(), dry_run=False, force=True, env={}, reports_dir=tmp_path / "reports"
    )
    assert result["action"] == "submitted"
    assert result["orders_submitted"] is True
    assert broker.submitted
    pos = store.get_open_position()
    assert pos is not None
    assert pos.symbol == "QQQ"
    assert store.is_live_started()
    assert store.inception_date() == result["as_of"]
    assert store.benchmark_history()
    assert (tmp_path / "reports" / "latest.json").exists()
    payload = __import__("json").loads((tmp_path / "reports" / "latest.json").read_text())
    assert payload["orders_submitted"] is True
    assert payload["signal"]["close"] == 120.0
    assert payload["signal"]["prior_20d_high"] == 100.0


def test_first_live_run_resets_dry_run_benchmark(cfg, store, tmp_path):
    store.init_benchmark_if_needed("2020-01-02", 100.0, {"QQQ": 50.0}, {"QQQ": 1.0})
    store.snapshot_equity("2020-01-02", 99.0, 99.0, {})
    closes = _closes([100.0] * 20 + [110.0])
    broker = MockBroker(
        cash=100_000.0,
        equity=100_000.0,
        positions={},
        prices={"QQQ": 110.0},
        daily_closes={"QQQ": closes},
    )
    result = run_momentum(
        cfg, broker, store, NullNotifier(), dry_run=False, force=True, env={}, reports_dir=tmp_path / "reports"
    )
    assert store.is_live_started()
    assert store.inception_date() == result["as_of"]
    assert store.inception_date() != "2020-01-02"
    assert all(s.as_of != "2020-01-02" for s in store.equity_history())


def test_once_per_session_gate_skips_second_live_eval(cfg, store, tmp_path):
    from types import SimpleNamespace
    from datetime import time as dtime

    day = date(2026, 9, 28)
    closes = _closes([100.0] * 20 + [120.0], start="2026-08-28")
    row = SimpleNamespace(date=day, close=dtime(16, 0))
    broker = MockBroker(
        cash=100_000.0,
        equity=100_000.0,
        positions={},
        prices={"QQQ": 120.0},
        daily_closes={"QQQ": closes},
    )
    broker.calendar_rows = [row]
    now = datetime(2026, 9, 28, 15, 50, tzinfo=ET)
    first = run_momentum(
        cfg,
        broker,
        store,
        NullNotifier(),
        dry_run=False,
        force=False,
        as_of="2026-09-28",
        now=now,
        env={},
        reports_dir=tmp_path / "r1",
    )
    assert first["action"] == "submitted"
    assert len(broker.submitted) == 1
    second = run_momentum(
        cfg,
        broker,
        store,
        NullNotifier(),
        dry_run=False,
        force=False,
        as_of="2026-09-28",
        now=now,
        env={},
        reports_dir=tmp_path / "r2",
    )
    assert second["action"] == "skipped"
    assert second["reason"] == "already_evaluated"
    assert len(broker.submitted) == 1


def test_session_gate_skips_wrong_dst_hour_without_submitting(cfg, store, tmp_path):
    from types import SimpleNamespace
    from datetime import time as dtime

    day = date(2026, 7, 15)
    closes = _closes([100.0] * 20 + [120.0], start="2026-06-15")
    broker = MockBroker(
        cash=100_000.0,
        equity=100_000.0,
        positions={},
        prices={"QQQ": 120.0},
        daily_closes={"QQQ": closes},
    )
    broker.calendar_rows = [SimpleNamespace(date=day, close=dtime(16, 0))]
    result = run_momentum(
        cfg,
        broker,
        store,
        NullNotifier(),
        dry_run=False,
        force=False,
        as_of="2026-07-15",
        now=datetime(2026, 7, 15, 20, 50, tzinfo=ZoneInfo("UTC")),
        env={},
        reports_dir=tmp_path / "reports",
    )
    assert result["action"] == "skipped"
    assert result["reason"] == "outside_near_close_window"
    assert broker.submitted == []
    assert not store.is_live_started()


def test_run_momentum_ignores_account_equity(cfg, store, tmp_path):
    closes = _closes([100.0] * 20 + [120.0])
    broker = MockBroker(
        cash=99_900.0,
        equity=100_000.0,
        positions={},
        prices={"QQQ": 120.0},
        daily_closes={"QQQ": closes},
    )
    result = run_momentum(
        cfg, broker, store, NullNotifier(), dry_run=True, force=True, env={}, reports_dir=tmp_path / "reports"
    )
    assert result["orders"][0]["notional"] == 100.0
    assert result["account"]["equity"] == 100_000.0
    assert result["managed"]["equity"] == 100.0


def test_main_momentum_dry_run_without_keys(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    repo = __import__("pathlib").Path(__file__).resolve().parents[1]
    monkeypatch.setenv("KILL_SWITCH", "")
    rc = main(["--dry-run", "--config", str(repo / "config.yaml"), "--force"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "mock broker" in out.lower()
    assert "QQQ" in out
    assert "prior_20d_high" in out


def test_session_skips_holiday_when_calendar_known():
    info = evaluate_session(
        as_of=date(2026, 1, 1),
        now=datetime(2026, 1, 1, 15, 50, tzinfo=ET),
        calendar_row=None,
        calendar_available=True,
        force=False,
    )
    assert info.skip_reason == "not_a_trading_day"


def test_session_skips_early_close():
    info = evaluate_session(
        as_of=date(2026, 11, 27),
        now=datetime(2026, 11, 27, 12, 0, tzinfo=ET),
        calendar_close=__import__("datetime").time(13, 0),
        calendar_available=True,
        calendar_row=object(),
        force=False,
    )
    assert info.is_early_close
    assert info.skip_reason == "early_close"


def test_session_near_close_window_et():
    info = evaluate_session(
        as_of=date(2026, 9, 28),
        now=datetime(2026, 9, 28, 15, 50, tzinfo=ET),
        calendar_row=object(),
        calendar_available=True,
        force=False,
    )
    assert info.near_close
    assert info.skip_reason is None
    info_utc = evaluate_session(
        as_of=date(2026, 9, 28),
        now=datetime(2026, 9, 28, 19, 50, tzinfo=ZoneInfo("UTC")),
        calendar_row=object(),
        calendar_available=True,
        force=False,
    )
    assert info_utc.near_close


def test_default_config_is_qqq_momentum():
    from pathlib import Path

    cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    assert cfg.strategy == "qqq_momentum"
    assert cfg.capital_cap_usd == 100.0
    assert cfg.symbols == ["QQQ"]
