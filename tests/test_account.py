"""Purchase dates and total cost in the holdings snapshot, from read-only deal history."""
import datetime as dt
import socket

import pandas as pd
import pytest
from moomoo import RET_ERROR, RET_OK, Currency, TrdEnv, TrdMarket

from finclaude import account

DEAL_COLUMNS = ["code", "stock_name", "deal_market", "deal_id", "order_id", "qty", "price", "trd_side",
                "create_time", "counter_broker_id", "counter_broker_name", "status", "jp_acc_type"]



REAL_REACHABLE = account._opend_reachable


@pytest.fixture(autouse=True)
def no_real_waiting(monkeypatch):
    """Deal queries are rate limited with time.sleep; fake contexts need no waiting or running OpenD."""
    monkeypatch.setattr(account.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(account, "_opend_reachable", lambda host, port: True)


def deal(code, side, qty, price, time):
    return {"code": code, "stock_name": code, "deal_market": "US", "deal_id": "D1", "order_id": "O1", "qty": qty,
            "price": price, "trd_side": side, "create_time": time, "counter_broker_id": 0,
            "counter_broker_name": "", "status": "OK", "jp_acc_type": "N/A"}


class FakeContext:
    positions = {TrdMarket.US: [], TrdMarket.HK: [], TrdMarket.AU: []}
    deals = {TrdMarket.US: [], TrdMarket.HK: [], TrdMarket.AU: []}
    windows = []
    filters = []

    def __init__(self, filter_trdmarket, **kwargs):
        self.market = filter_trdmarket
        type(self).filters.append(filter_trdmarket)

    def get_acc_list(self):
        return RET_OK, pd.DataFrame({"acc_id": [987654], "trd_env": [TrdEnv.REAL]})

    def position_list_query(self, trd_env, acc_id, position_market, currency):
        return RET_OK, pd.DataFrame(self.positions[position_market])

    def history_deal_list_query(self, start, end, trd_env, acc_id, deal_market):
        assert trd_env == TrdEnv.REAL
        type(self).windows.append((deal_market, dt.date.fromisoformat(start), dt.date.fromisoformat(end)))
        rows = [d for d in self.deals[deal_market] if start <= d["create_time"][:10] <= end]
        return RET_OK, pd.DataFrame(rows, columns=DEAL_COLUMNS)

    def close(self):
        pass


def position(code, qty, average_cost, market_val):
    return {"code": code, "stock_name": code, "position_market": "US", "qty": qty, "average_cost": average_cost,
            "market_val": market_val, "currency": "USD"}


def use(monkeypatch, positions, deals, au_positions=(), au_deals=()):
    ctx = type("Ctx", (FakeContext,), {"positions": {TrdMarket.US: positions, TrdMarket.HK: [], TrdMarket.AU: list(au_positions)},
                                         "deals": {TrdMarket.US: deals, TrdMarket.HK: [], TrdMarket.AU: list(au_deals)},
                                         "windows": [], "filters": []})
    monkeypatch.setattr(account, "OpenSecTradeContext", ctx)
    return ctx


def test_snapshot_adds_purchase_dates_and_total_cost(monkeypatch):
    use(monkeypatch, [position("US.KO", 3.5, 60.2, 215.25)],
        [deal("US.KO", "BUY", 3.0, 60.2, "2026-03-16 09:45:00.100"),
         deal("US.KO", "BUY", 0.5, 60.2, "2026-03-16 09:45:00.200")])
    row = account.snapshot().set_index("code").loc["US.KO"]
    assert row["first_buy"] == "2026-03-16" and row["last_buy"] == "2026-03-16"
    assert row["cost_total"] == round(3.5 * 60.2, 2)
    assert row["qty_no_deal"] == 0


def test_holding_period_restarts_after_selling_everything(monkeypatch):
    use(monkeypatch, [position("US.NVDA", 1.0, 180.0, 190.0)],
        [deal("US.NVDA", "BUY", 2.0, 120.0, "2025-12-01 10:00:00.000"),
         deal("US.NVDA", "SELL", 2.0, 150.0, "2026-02-03 10:00:00.000"),
         deal("US.NVDA", "BUY", 1.0, 180.0, "2026-07-01 10:00:00.000")])
    row = account.snapshot().set_index("code").loc["US.NVDA"]
    assert row["first_buy"] == "2026-07-01"


def test_shares_without_deals_are_flagged(monkeypatch):
    # Free shares: 2 bought at 150.00 plus 0.25 given (average cost 133.333); US.PEP came with no trade at all.
    use(monkeypatch, [position("US.MSFT", 2.25, 133.333, 380.0), position("US.PEP", 0.05, 0.0, 8.1)],
        [deal("US.MSFT", "BUY", 2.0, 150.0, "2026-02-02 10:00:00.000")])
    rows = account.snapshot().set_index("code")
    assert rows.loc["US.MSFT", "qty_no_deal"] == 0.25
    assert rows.loc["US.MSFT", "first_buy"] == "2026-02-02"
    assert rows.loc["US.PEP", "qty_no_deal"] == 0.05
    assert pd.isna(rows.loc["US.PEP", "first_buy"])


def test_deal_history_is_read_in_windows_the_server_accepts(monkeypatch):
    # OpenD: 单次查询的时间跨度不能超过 360 天 (2026-10-05). 2025-10-11..2026-10-05 was accepted.
    ctx = use(monkeypatch, [], [])
    account.snapshot(deals_since=dt.date(2023, 1, 1))
    windows = [(start, end) for market, start, end in ctx.windows if market == TrdMarket.US]
    assert windows[0][0] == dt.date(2023, 1, 1) and windows[-1][1] == dt.date.today()
    for (start, end), (next_start, _) in zip(windows, windows[1:]):
        assert next_start == end + dt.timedelta(days=1)
    assert all((end - start).days <= 359 for start, end in windows)


def test_deals_table_has_no_account_numbers_or_order_ids(monkeypatch):
    use(monkeypatch, [position("US.ORCX", 30.0, 20.0, 610.0)],
        [deal("US.ORCX", "BUY", 30.0, 20.0, "2026-05-04 10:00:00.000")])
    deals = account.deal_history()
    assert list(deals.columns) == ["code", "name", "market", "side", "qty", "price", "time"]
    assert not deals.astype(str).apply(lambda col: col.str.contains("987654")).any().any()


def test_australian_holdings_are_read_through_one_all_markets_context(monkeypatch):
    # Australian holdings once went missing because AU was dropped from the query list (fixed 2026-10-06).
    ivv = {"code": "AU.IVV", "stock_name": "iShares S&P 500 ETF", "position_market": "AU", "qty": 15.0,
           "average_cost": 70.0, "market_val": 1110.0, "currency": "AUD"}
    buy = {**deal("AU.IVV", "BUY", 15.0, 70.0, "2026-04-14 10:30:00.000"), "deal_market": "AU"}
    ctx = use(monkeypatch, [position("US.KO", 3.5, 60.2, 215.25)], [], au_positions=[ivv], au_deals=[buy])
    rows = account.snapshot().set_index("code")
    assert rows.loc["AU.IVV", "market_value"] == 1110.0 and rows.loc["AU.IVV", "currency"] == "AUD"
    assert rows.loc["AU.IVV", "first_buy"] == "2026-04-14"
    assert "US.KO" in rows.index
    assert set(ctx.filters) == {TrdMarket.NONE}


def test_deal_queries_stay_under_the_rate_limit(monkeypatch):
    # OpenD: 查询历史成交频率太高，每30秒最多10次 (2026-10-06, three markets x four windows = 12 calls).
    clock = {"now": 0.0}
    calls = []
    monkeypatch.setattr(account.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(account.time, "sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))
    ctx = use(monkeypatch, [], [])
    original = ctx.history_deal_list_query

    def counted(self, **kwargs):
        calls.append(clock["now"])
        return original(self, **kwargs)

    monkeypatch.setattr(ctx, "history_deal_list_query", counted)
    account.deal_history(since=dt.date(2023, 1, 1))
    assert len(calls) == 12
    for i, t in enumerate(calls):
        assert sum(1 for u in calls[i:] if u - t < 30) <= account.DEAL_RATE_CALLS


def funds_row(**values):
    row = {"total_assets": 0.0, "cash": 0.0, "market_val": 0.0, "au_cash": 0.0, "us_cash": 0.0, "hk_cash": 0.0,
           "avl_withdrawal_cash": 0.0, "currency": "AUD"}
    return {**row, **values}


def test_funds_report_cash_and_total_in_aud_without_account_numbers(monkeypatch):
    # 2026-10-16: the 5,000 AUD savings moved into the account, and positions alone cannot show cash.
    calls = []

    class WithFunds(FakeContext):
        def accinfo_query(self, trd_env, acc_id, currency):
            calls.append((trd_env, currency))
            return RET_OK, pd.DataFrame([funds_row(total_assets=9351.16, cash=5000.0, market_val=4351.16, au_cash=5000.0)])

    monkeypatch.setattr(account, "OpenSecTradeContext", WithFunds)
    table = account.funds()
    assert calls == [(TrdEnv.REAL, Currency.AUD)]
    row = table.iloc[0]
    assert (row["total_assets"], row["cash"], row["au_cash"]) == (9351.16, 5000.0, 5000.0)
    assert row["as_of"] == dt.date.today().isoformat()
    assert not table.astype(str).apply(lambda col: col.str.contains("987654")).any().any()


def test_funds_stop_if_reading_requires_unlock(monkeypatch):
    class Locked(FakeContext):
        def accinfo_query(self, trd_env, acc_id, currency):
            return RET_ERROR, "please unlock trade first"

    monkeypatch.setattr(account, "OpenSecTradeContext", Locked)
    with pytest.raises(PermissionError):
        account.funds()


def test_stops_fast_when_opend_is_not_running(monkeypatch):
    # 2026-10-17: with OpenD closed the SDK retried every 8 s and the refresh hung until killed.
    opened = []

    class Recorder(FakeContext):
        def __init__(self, filter_trdmarket, **kwargs):
            opened.append(filter_trdmarket)

    monkeypatch.setattr(account, "OpenSecTradeContext", Recorder)
    monkeypatch.setattr(account, "_opend_reachable", lambda host, port: False)
    with pytest.raises(account.NotConfigured, match="OpenD"):
        account.funds()
    with pytest.raises(account.NotConfigured, match="OpenD"):
        account.snapshot(deals=pd.DataFrame(columns=["code", "side", "qty", "time"]))
    assert opened == []


def test_opend_check_tells_an_open_port_from_a_closed_one():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        assert REAL_REACHABLE("127.0.0.1", server.getsockname()[1]) is True
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    assert REAL_REACHABLE("127.0.0.1", closed_port) is False


# --- cash flows: money moved in or out, read one clearing date at a time ---------------------------------------------

FLOW_FIELDS = ["cashflow_id", "clearing_date", "settlement_date", "currency", "cashflow_type", "cashflow_direction",
               "cashflow_amount", "cashflow_remark", "create_time"]
FLOWS = {"2026-10-20": [{"cashflow_id": 7179, "clearing_date": "2026-10-20", "settlement_date": "2026-10-20",
                         "currency": "AUD", "cashflow_type": "银行转存", "cashflow_direction": "IN",
                         "cashflow_amount": 1300.0, "cashflow_remark": "N/A", "create_time": "N/A"}],
         "2026-10-22": [{"cashflow_id": 209828, "clearing_date": "2026-10-22", "settlement_date": "2026-10-22",
                         "currency": "USD", "cashflow_type": "现金分红", "cashflow_direction": "IN",
                         "cashflow_amount": 0.54, "cashflow_remark": "NVDA dividend", "create_time": "N/A"}]}


class WithFlows(FakeContext):
    asked = []

    def get_acc_cash_flow(self, clearing_date, trd_env, acc_id):
        assert trd_env == TrdEnv.REAL
        type(self).asked.append(clearing_date)
        return RET_OK, pd.DataFrame(FLOWS.get(clearing_date, []), columns=FLOW_FIELDS)


def test_cash_flows_are_read_one_clearing_date_at_a_time_without_ids(monkeypatch):
    # 2026-10-19: OpenD answered a ranged query with "仅支持通过清算日期查询资金流水" for this account
    WithFlows.asked = []
    monkeypatch.setattr(account, "OpenSecTradeContext", WithFlows)
    table = account.cash_flows(dt.date(2026, 10, 18), dt.date(2026, 10, 24))
    assert WithFlows.asked == [f"2026-10-{d}" for d in range(18, 25)]
    assert list(table["cashflow_type"]) == ["银行转存", "现金分红"]
    assert "cashflow_id" not in table.columns
    assert not table.astype(str).apply(lambda col: col.str.contains("987654")).any().any()


def test_cash_flow_queries_stay_under_the_rate_limit(monkeypatch):
    # OpenD: 查询现金流水频率太高，请求失败，每30秒最多20次 (2026-10-19)
    clock, calls = {"now": 0.0}, []
    monkeypatch.setattr(account.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(account.time, "sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))

    class Counted(WithFlows):
        def get_acc_cash_flow(self, clearing_date, trd_env, acc_id):
            calls.append(clock["now"])
            return super().get_acc_cash_flow(clearing_date, trd_env, acc_id)

    monkeypatch.setattr(account, "OpenSecTradeContext", Counted)
    account.cash_flows(dt.date(2026, 9, 1), dt.date(2026, 10, 15))
    assert len(calls) == 45
    for i, t in enumerate(calls):
        assert sum(1 for u in calls[i:] if u - t < 30) < 20


def test_refusals_never_show_the_account_number(monkeypatch):
    class Refuses(FakeContext):
        def get_acc_cash_flow(self, clearing_date, trd_env, acc_id):
            return RET_ERROR, "账户123456789012345678仅支持通过清算日期查询资金流水"

    monkeypatch.setattr(account, "OpenSecTradeContext", Refuses)
    with pytest.raises(account.NotConfigured) as raised:
        account.cash_flows(dt.date(2026, 10, 18), dt.date(2026, 10, 18))
    assert "123456789012345678" not in str(raised.value) and "仅支持通过清算日期" in str(raised.value)
