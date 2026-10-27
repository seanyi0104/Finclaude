"""Live data checks (need internet). Skip with: uv run pytest -m "not network"."""
import pytest

from finclaude import data
from finclaude.config import DataUnavailable, NotConfigured

pytestmark = pytest.mark.network


def test_hong_kong_stock_has_long_history():
    df = data.get_prices("0700.HK")
    assert df.index[0].year <= 2005
    assert {"close", "volume", "source"} <= set(df.columns)
    assert (df["source"] == "yfinance").all()


def test_hang_seng_index():
    assert data.get_prices("^HSI").index[0].year <= 1990


def test_us_ticker_uses_yfinance_until_treg_is_connected(monkeypatch):
    monkeypatch.delenv("TREG_TOKEN", raising=False)
    df = data.get_prices("AAPL", "2024-01-01", "2024-12-31")
    assert df["source"].iloc[0] == "yfinance（Treg 未接入）"
    assert df.index.min().year == 2024


def test_second_request_before_the_next_close_reads_the_saved_file(monkeypatch):
    data.get_prices("SPY")

    def offline(*args, **kwargs):
        raise AssertionError("should have used data/raw instead of the network")

    monkeypatch.setattr(data.yf, "Ticker", offline)
    assert not data.get_prices("SPY").empty


def test_fx_series():
    assert data.get_fx("AUDUSD=X", "2025-01-01").between(0.4, 1.2).all()


def test_us_option_chain_has_calls_and_puts():
    chain = data.get_option_chain("SPY")
    assert {"call", "put"} <= set(chain["type"])


def test_hong_kong_options_wait_for_moomoo():
    with pytest.raises(NotConfigured):
        data.get_option_chain("0700.HK")


def test_unknown_ticker_raises_instead_of_guessing():
    with pytest.raises(DataUnavailable):
        data.get_prices("ZZZZNOTREAL.HK")


@pytest.mark.parametrize("name", ["VIX", "VXN"])
def test_volatility_indexes_from_cboe_match_yfinance_on_finished_days(name):
    official = data.get_cboe_history(name)
    yahoo = data.get_prices(f"^{name}")["close"]
    common = official.index.intersection(yahoo.index)[-5:]
    assert len(common) == 5
    for day in common:
        assert abs(yahoo[day] / official[day] - 1) < 0.0005, day
