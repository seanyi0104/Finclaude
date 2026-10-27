"""The weekly rhythm, offline: which US trading week a day belongs to, the folder and tab names, the order of week
tabs, and the Friday review of holdings."""
import datetime as dt

import pandas as pd
import pytest

from finclaude import weekly


@pytest.mark.parametrize("day, monday", [
    ("2026-10-14", "2026-10-12"),  # Wednesday
    ("2026-10-16", "2026-10-12"),  # Friday
    ("2026-10-17", "2026-10-12"),  # Saturday: the week that just ended
    ("2026-10-18", "2026-10-12"),  # Sunday
    ("2026-10-19", "2026-10-19"),  # Monday
])
def test_a_day_belongs_to_its_monday_to_friday_week(day, monday):
    mon, fri = weekly.week_of(dt.date.fromisoformat(day))
    assert str(mon) == monday and fri == mon + dt.timedelta(days=4)


def test_us_day_is_taken_in_new_york_time():
    # Saturday 07:30 in Sydney is still Friday afternoon in New York
    sydney = pd.Timestamp("2026-10-17 07:30", tz="Australia/Sydney").to_pydatetime()
    assert weekly.us_day(sydney) == dt.date(2026, 10, 16)


def test_folder_and_tab_names_have_no_slashes():
    mon = dt.date(2026, 10, 19)
    assert weekly.folder_name(mon) == "2026-10-19_10-23"
    assert weekly.tab_name(mon) == "10-19～10-23"
    assert "/" not in weekly.folder_name(mon) + weekly.tab_name(mon)
    assert weekly.folder_name(dt.date(2026, 12, 28)) == "2026-12-28_01-01"


def test_newer_weeks_sort_first_in_the_doc():
    orders = [weekly.tab_order(dt.date(2026, 10, 12)), weekly.tab_order(dt.date(2026, 10, 19)),
              weekly.tab_order(dt.date(2027, 1, 4))]
    assert sorted(orders) == orders[::-1]
    assert all(o < weekly.GLOSSARY_ORDER for o in orders)  # the glossary tab stays last


def closes(values, start="2026-10-05"):
    return pd.Series(values, index=pd.bdate_range(start, periods=len(values)), dtype=float)


def test_weekly_change_runs_from_the_last_close_before_monday_to_the_week_end():
    close = closes([100, 101, 102, 103, 104, 105, 106, 107, 108, 110])  # 10-05 .. 10-16
    change = weekly.weekly_change(close, dt.date(2026, 10, 12), dt.date(2026, 10, 16))
    assert change["start_date"] == "2026-10-09" and change["start"] == 104
    assert change["end_date"] == "2026-10-16" and change["end"] == 110
    assert change["change"] == pytest.approx(110 / 104 - 1)


def test_weekly_change_handles_a_holiday_friday():
    close = closes([100, 101, 102, 103, 104, 105, 106, 107, 108])  # the week ends on Thursday 10-15
    change = weekly.weekly_change(close, dt.date(2026, 10, 12), dt.date(2026, 10, 16))
    assert change["end_date"] == "2026-10-15" and change["change"] == pytest.approx(108 / 104 - 1)


def test_review_lists_every_holding_and_the_account_without_account_numbers():
    rows = [{"code": "US.SOXL", "name": "Direxion Semiconductor Bull 3X", "qty": 20, "market_value_aud": 2900.0,
             "currency": "USD", "change": {"start": 104.0, "start_date": "2026-10-09", "end": 99.5, "end_date": "2026-10-16",
                                           "change": 99.5 / 104 - 1},
             "trend": {"long": "上升", "short": "转弱"}, "range": {"lo": 80.1, "hi": 123.4, "kind": "期权"},
             "cost": 96.0},
            {"code": "AU.IVV", "name": "iShares S&P 500 ETF", "qty": 15, "market_value_aud": 1110.0,
             "currency": "AUD", "change": {"start": 74.0, "start_date": "2026-10-09", "end": 74.2, "end_date": "2026-10-16",
                                           "change": 74.2 / 74 - 1},
             "trend": {"long": "上升", "short": "向上"}, "range": {"lo": 72.0, "hi": 76.5, "kind": "历史"}}]
    account = {"total_assets": 8500.0, "cash": 50.0, "as_of": "2026-10-17"}
    md = weekly.review_markdown(rows, account, dt.date(2026, 10, 12), dt.date(2026, 10, 16))
    assert md.startswith("## 持仓回顾和展望")
    for words in ("SOXL", "IVV", "-4.3%", "8,500 澳元", "80–123 美元", "72–76 澳元", "历史",
                  "你的成本 96.00 → 99.50（+3.6%）"):
        assert words in md, words
    assert "acc" not in md.lower()


def test_review_says_when_a_holding_has_no_data():
    rows = [{"code": "US.ORCX", "name": "Tradr 2X Long ORCL", "qty": 5, "market_value_aud": 300.0, "currency": "USD",
             "error": "Tiingo 没有返回 ORCX 的日线"}]
    md = weekly.review_markdown(rows, {"total_assets": 8500.0, "cash": 50.0, "as_of": "2026-10-17"},
                                dt.date(2026, 10, 12), dt.date(2026, 10, 16))
    assert "ORCX" in md and "无可靠数据" in md and "Tiingo 没有返回" in md


def test_closed_opend_falls_back_to_the_saved_snapshot_and_converts_to_aud(monkeypatch, tmp_path):
    from finclaude import account, analysis, data, forecast
    from finclaude.config import NotConfigured

    def closed(**kwargs):
        raise NotConfigured("OpenD closed")

    monkeypatch.setattr(account, "snapshot", closed)
    monkeypatch.setattr(account, "load_holdings", lambda: pd.DataFrame(
        {"code": ["US.SOXL", "AU.IVV"], "name": ["SOXL", "IVV"], "qty": [20, 15], "market_value": [1900.0, 1110.0],
         "currency": ["USD", "AUD"], "as_of": ["2026-10-17", "2026-10-17"]}))
    funds_csv = tmp_path / "funds.csv"
    pd.DataFrame({"total_assets": [8500.1234], "cash": [50.0], "as_of": ["2026-10-17"]}).to_csv(funds_csv, index=False)
    monkeypatch.setattr(account, "FUNDS_CSV", funds_csv)
    monkeypatch.setattr(data, "get_fx", lambda pair, start=None, end=None: pd.Series([0.65, 0.6946], index=pd.DatetimeIndex(["2026-10-15", "2026-10-16"])))
    bars = pd.DataFrame({"close": pd.Series(range(100, 400), dtype=float).to_numpy()},
                        index=pd.bdate_range(end="2026-10-16", periods=300))
    monkeypatch.setattr(data, "get_bars", lambda ticker: bars)
    monkeypatch.setattr(forecast, "risk_free", lambda: (0.0407, "2026-10-16"))
    monkeypatch.setattr(analysis, "option_views", lambda *a, **k: {"2w": {"lo": 300.0, "hi": 420.0}})
    rows, funds = weekly.holding_rows(dt.date(2026, 10, 12), dt.date(2026, 10, 16))
    assert "快照" in funds["source"] and "AUDUSD 0.6946" in funds["fx"]
    soxl = next(r for r in rows if r["code"] == "US.SOXL")
    assert soxl["market_value_aud"] == pytest.approx(1900.0 / 0.6946)
    assert soxl["range"]["kind"] == "期权"
    ivv = next(r for r in rows if r["code"] == "AU.IVV")
    assert ivv["market_value_aud"] == pytest.approx(1110.0) and ivv["range"]["kind"] == "历史"
    md = weekly.review_markdown(rows, funds, dt.date(2026, 10, 12), dt.date(2026, 10, 16))
    assert "快照" in md and "AUDUSD 0.6946（yfinance，2026-10-16）" in md


def test_review_lists_the_largest_holdings_first():
    def row(code, value):
        return {"code": code, "name": code, "qty": 1, "market_value_aud": value, "currency": "USD",
                "error": "no data"}

    md = weekly.review_markdown([row("US.TSLA", 15.0), row("US.SOXL", 2900.0), row("AU.IVV", 1110.0)],
                                {"total_assets": 8500.0, "cash": 50.0, "as_of": "2026-10-17"},
                                dt.date(2026, 10, 12), dt.date(2026, 10, 16))
    assert md.index("SOXL") < md.index("IVV") < md.index("TSLA")


@pytest.mark.parametrize("day, closing, opening", [
    ("2026-10-18", "2026-10-12", "2026-10-19"),  # Sunday: the week that just ended closes, next week opens
    ("2026-10-17", "2026-10-12", "2026-10-19"),  # Saturday
    ("2026-10-19", "2026-10-12", "2026-10-19"),  # Monday: last week closes, this week opens
    ("2026-10-21", "2026-10-12", "2026-10-19"),  # Wednesday
    ("2026-10-24", "2026-10-19", "2026-10-26"),  # the next Saturday
])
def test_week_command_knows_which_week_to_close_and_which_to_open(day, closing, opening):
    close, open_ = weekly.close_and_open(dt.date.fromisoformat(day))
    assert (str(close), str(open_)) == (closing, opening)


# --- the account's week: total assets against the last saved week, bank transfers taken out ---------------------

LAST_FRIDAY = {"total_assets": 8500.0, "cash": 50.0, "as_of": "2026-10-17", "source": "Moomoo 只读"}


def flows_table(rows):
    return pd.DataFrame(rows, columns=["clearing_date", "currency", "cashflow_type", "cashflow_direction",
                                       "cashflow_amount", "cashflow_remark"])


def test_week_snapshots_are_saved_and_the_latest_earlier_one_is_found(tmp_path):
    weekly.save_week(dt.date(2026, 10, 16), LAST_FRIDAY, tmp_path)
    weekly.save_week(dt.date(2026, 10, 9), {**LAST_FRIDAY, "total_assets": 7000.0, "as_of": "2026-10-10"}, tmp_path)
    previous = weekly.previous_week(dt.date(2026, 10, 23), tmp_path)
    assert previous["friday"] == "2026-10-16" and previous["total_assets"] == 8500.0
    assert weekly.previous_week(dt.date(2026, 10, 9), tmp_path) is None
    # a second run with OpenD closed (older figures) never overwrites the newer ones
    weekly.save_week(dt.date(2026, 10, 16), {**LAST_FRIDAY, "total_assets": 1.0, "as_of": "2026-10-10"}, tmp_path)
    assert weekly.previous_week(dt.date(2026, 10, 23), tmp_path)["total_assets"] == 8500.0


def test_the_week_takes_bank_transfers_out_and_keeps_dividends_in(monkeypatch, tmp_path):
    from finclaude import account
    weekly.save_week(dt.date(2026, 10, 16), LAST_FRIDAY, tmp_path)
    asked = []

    def flows(start, end):
        asked.append((start, end))
        return flows_table([["2026-10-20", "AUD", "银行转存", "IN", 1300.0, "N/A"],
                            ["2026-10-22", "USD", "现金分红", "IN", 0.54, "NVDA dividend"],
                            ["2026-10-22", "AUD", "货币兑换", "OUT", -500.0, "AUD -> USD"]])

    monkeypatch.setattr(account, "cash_flows", flows)
    funds = {"total_assets": 10100.0, "cash": 1380.0, "as_of": "2026-10-24", "source": "Moomoo 只读",
             "rates": {"USD": 0.65}}
    week = weekly.account_week(funds, dt.date(2026, 10, 23), tmp_path)
    assert asked == [(dt.date(2026, 10, 18), dt.date(2026, 10, 24))]
    assert (week["deposits"], week["withdrawals"]) == (1300.0, 0)
    assert week["profit"] == pytest.approx(10100.0 - 8500.0 - 1300.0)
    md = weekly.review_markdown([], funds, dt.date(2026, 10, 19), dt.date(2026, 10, 23), week)
    for words in ("这周账户赚 300 澳元", "8,500 → 10,100 澳元", "入金 1,300 澳元（2026-10-20 银行转存）"):
        assert words in md, words


def test_a_quiet_week_shows_the_percent_and_a_first_week_waits(monkeypatch, tmp_path):
    from finclaude import account
    monkeypatch.setattr(account, "cash_flows", lambda start, end: flows_table([]))
    funds = {"total_assets": 8400.0, "cash": 50.0, "as_of": "2026-10-24", "source": "Moomoo 只读"}
    first = weekly.account_week(funds, dt.date(2026, 10, 23), tmp_path)
    assert first == {"previous": None}
    assert "从下周起对比" in weekly.review_markdown([], funds, dt.date(2026, 10, 19), dt.date(2026, 10, 23), first)
    weekly.save_week(dt.date(2026, 10, 16), LAST_FRIDAY, tmp_path)
    week = weekly.account_week(funds, dt.date(2026, 10, 23), tmp_path)
    md = weekly.review_markdown([], funds, dt.date(2026, 10, 19), dt.date(2026, 10, 23), week)
    assert "这周账户亏 100 澳元（-1.2%）" in md and "没有入金或出金" in md


def test_stale_or_unreadable_figures_say_so(monkeypatch, tmp_path):
    from finclaude import account
    from finclaude.config import NotConfigured
    weekly.save_week(dt.date(2026, 10, 16), LAST_FRIDAY, tmp_path)
    stale = {**LAST_FRIDAY, "source": "OpenD 没开，用上次保存的快照"}
    week = weekly.account_week(stale, dt.date(2026, 10, 23), tmp_path)
    assert "算不出" in weekly.review_markdown([], stale, dt.date(2026, 10, 19), dt.date(2026, 10, 23), week)

    def closed(start, end):
        raise NotConfigured("连不上 moomoo OpenD")

    monkeypatch.setattr(account, "cash_flows", closed)
    funds = {"total_assets": 8400.0, "cash": 50.0, "as_of": "2026-10-24", "source": "Moomoo 只读"}
    week = weekly.account_week(funds, dt.date(2026, 10, 23), tmp_path)
    md = weekly.review_markdown([], funds, dt.date(2026, 10, 19), dt.date(2026, 10, 23), week)
    assert "差 -100 澳元" in md and "现金流水读不到" in md and "可能含入金或出金" in md


def test_a_total_read_while_us_stocks_trade_says_so(monkeypatch, tmp_path):
    # 2026-10-19 01:59 New York (overnight session): Moomoo values US holdings at the overnight price, not the
    # Friday close, so the total then is not a Friday-close total
    from finclaude import account
    monkeypatch.setattr(account, "cash_flows", lambda start, end: flows_table([]))
    weekly.save_week(dt.date(2026, 10, 16), {**LAST_FRIDAY, "read_at": "2026-10-17T12:30+11:00"}, tmp_path)  # Fri 21:30 NY
    funds = {"total_assets": 8800.0, "cash": 50.0, "as_of": "2026-10-19", "read_at": "2026-10-19T16:59+11:00",
             "source": "Moomoo 只读"}
    week = weekly.account_week(funds, dt.date(2026, 10, 23), tmp_path)
    md = weekly.review_markdown([], funds, dt.date(2026, 10, 19), dt.date(2026, 10, 23), week)
    assert "2026-10-19 读取时美股在夜盘交易" in md and "不是周五收盘价" in md
    assert "2026-10-17 读取时" not in md  # read while closed: nothing to say
    assert weekly.previous_week(dt.date(2026, 10, 23), tmp_path)["read_at"] == "2026-10-17T12:30+11:00"


def test_the_review_ends_with_this_months_jev_spending():
    line = weekly.jev_usage_line("2026-10", 0.001023, 0.6978)
    assert line == "工具费用：Jev 本月（2026-10）已用 0.0010 美元（约 0.0015 澳元），每月自动调用的上限是 1 美元。"
    assert "澳元" not in weekly.jev_usage_line("2026-10", 0.0, None)
