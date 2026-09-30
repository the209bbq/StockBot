from __future__ import annotations

from report import summarize
from store import Store


def test_weekly_summary_vs_benchmark(tmp_path):
    store = Store(tmp_path / "t.db")
    prices0 = {"VTI": 100.0, "VXUS": 50.0, "BND": 80.0}
    prices1 = {"VTI": 110.0, "VXUS": 50.0, "BND": 80.0}
    mix = {"VTI": 0.55, "VXUS": 0.25, "BND": 0.20}
    store.snapshot_equity("2026-09-01", 100_000.0, 1_000.0, {})
    store.init_benchmark_if_needed("2026-09-01", 100_000.0, prices0, mix)
    store.snapshot_equity("2026-09-21", 102_000.0, 1_000.0, {})
    store.mark_benchmark("2026-09-21", prices1)
    text = summarize(store, as_of="2026-09-21")
    assert "Since inception" in text
    assert "Portfolio:" in text
    assert "Buy & hold mix:" in text
    assert "Last week" in text
    assert "+2.00%" in text
    assert "Closed trades:" in text
    assert "Win rate:" in text
