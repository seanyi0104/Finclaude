"""Guards from CLAUDE.md: Moomoo stays read-only and keys never leave .env."""
from pathlib import Path

import pandas as pd
import pytest
from moomoo import RET_ERROR, RET_OK, TrdEnv, TrdMarket
from moomoo.common.constant import TRADE

from finclaude import account, jev, treg
from finclaude.config import NotConfigured

PACKAGE = Path(__file__).resolve().parent.parent / "finclaude"
TRADING_CALLS = ("place_order", "place_combo_order", "modify_order", "change_order", "cancel_all_order", "unlock_trade")



@pytest.fixture(autouse=True)
def no_real_waiting(monkeypatch):
    """Deal queries are rate limited with time.sleep; fake contexts need no waiting or running OpenD."""
    monkeypatch.setattr(account.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(account, "_opend_reachable", lambda host, port: True)


def test_package_never_calls_trading_functions():
    for path in PACKAGE.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for name in TRADING_CALLS:
            assert name not in text, f"{path.name} 出现了交易接口 {name}"


class FakeContext:
    positions = {
        TrdMarket.US: [{"code": "US.VOO", "stock_name": "Vanguard S&P 500 ETF", "position_market": "US",
                        "qty": 10, "cost_price": 480.0, "market_val": 7400.0, "currency": "AUD"}],
        TrdMarket.HK: [{"code": "HK.02800", "stock_name": "Tracker Fund", "position_market": "HK",
                        "qty": 500, "cost_price": 22.0, "market_val": 2400.0, "currency": "AUD"}],
        TrdMarket.AU: [],
    }

    def __init__(self, filter_trdmarket, **kwargs):
        self.market = filter_trdmarket

    def get_acc_list(self):
        return RET_OK, pd.DataFrame({"acc_id": [111, 222], "trd_env": [TrdEnv.REAL, TrdEnv.SIMULATE]})

    def position_list_query(self, trd_env, acc_id, position_market, currency):
        assert trd_env == TrdEnv.REAL and acc_id == 111
        return RET_OK, pd.DataFrame(self.positions[position_market])

    def history_deal_list_query(self, start, end, trd_env, acc_id, deal_market):
        assert trd_env == TrdEnv.REAL and acc_id == 111
        return RET_OK, pd.DataFrame(columns=["code", "qty", "price", "trd_side", "create_time", "status"])

    def close(self):
        pass


def test_snapshot_reads_real_accounts_without_account_numbers(monkeypatch):
    monkeypatch.setattr(account, "OpenSecTradeContext", FakeContext)
    holdings = account.snapshot()
    assert list(holdings["code"]) == ["US.VOO", "HK.02800"]
    assert "acc_id" not in holdings.columns
    assert not holdings.astype(str).apply(lambda col: col.str.contains("111")).any().any()


def test_snapshot_reports_average_cost_not_diluted_cost(monkeypatch):
    # Free shares that later paid out: average cost 0, diluted cost negative.
    class FreeShares(FakeContext):
        positions = {**FakeContext.positions,
                     TrdMarket.US: [{"code": "US.AAPL", "stock_name": "Apple", "position_market": "US", "qty": 0.05,
                                     "cost_price": -0.2, "average_cost": 0.0, "diluted_cost": -0.2,
                                     "market_val": 12.5, "currency": "USD"}]}

    monkeypatch.setattr(account, "OpenSecTradeContext", FreeShares)
    holdings = account.snapshot()
    assert holdings.loc[holdings["code"] == "US.AAPL", "cost_price"].item() == 0.0


def test_snapshot_only_queries_markets_the_sdk_accepts():
    # The SDK rejects (TrdMarket.AU, TrdEnv.REAL) as a context filter ("the type of environment param is wrong"),
    # but accepts TrdMarket.NONE; each query then names its market, AU included (2026-10-06).
    assert TRADE.check_mkt_envtype(account.CONTEXT_MARKET, TrdEnv.REAL)
    assert TrdMarket.AU in account.MARKETS


def test_snapshot_stops_if_reading_requires_unlock(monkeypatch):
    class Locked(FakeContext):
        def get_acc_list(self):
            return RET_ERROR, "please unlock trade first"

    monkeypatch.setattr(account, "OpenSecTradeContext", Locked)
    with pytest.raises(PermissionError):
        account.snapshot()


def test_paid_services_stop_cleanly_until_connected(monkeypatch):
    monkeypatch.delenv("TREG_TOKEN", raising=False)
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    with pytest.raises(NotConfigured):
        treg.get_eod_history("AAPL")
    with pytest.raises(NotConfigured):
        jev.preflight()


def test_jev_compare_reports_set_and_order():
    assert jev.compare(["XLK", "XLE"], ["XLE", "XLK"]) == {"same_set": True, "same_order": False}
