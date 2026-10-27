"""Market data, offline: VIX and VXN closes from Cboe, the official source, and yfinance history, each kept until
its market's next close is out (finclaude.sessions)."""
import os

import pandas as pd
import pytest

from finclaude import data, sessions
from finclaude.config import DataUnavailable

CBOE_CSV = "DATE,OPEN,HIGH,LOW,CLOSE\n10/15/2026,17.550000,18.010000,16.340000,16.460000\n" \
           "10/16/2026,16.020000,22.310000,15.720000,19.990000\n"
MONDAY_ROW = "10/19/2026,18.370000,18.930000,17.390000,18.440000\n"
NEW_YORK, SYDNEY, HONG_KONG = "America/New_York", "Australia/Sydney", "Asia/Hong_Kong"


def at(text, zone=NEW_YORK):
    return pd.Timestamp(text, tz=zone).to_pydatetime()


def clock(monkeypatch, when):
    monkeypatch.setattr(sessions, "now", lambda: when)


def written(folder, when):
    """Date every saved file as written at `when`."""
    stamp = pd.Timestamp(when).timestamp()
    for path in folder.rglob("*"):
        if path.is_file():
            os.utime(path, (stamp, stamp))


class Answer:
    def __init__(self, status, text):
        self.status_code, self.text = status, text


def test_vix_comes_from_cboe_and_is_read_once_per_close(monkeypatch, tmp_path):
    asked = []

    def get(url, timeout):
        asked.append(url)
        return Answer(200, CBOE_CSV)

    monkeypatch.setattr(data, "RAW_DIR", tmp_path)
    monkeypatch.setattr(data.requests, "get", get)
    clock(monkeypatch, at("2026-10-17 10:00"))  # Saturday: Friday's close
    vix = data.get_vix_official()
    assert vix[pd.Timestamp("2026-10-16")] == pytest.approx(19.99) and vix.index[-1] == pd.Timestamp("2026-10-16")
    written(tmp_path, at("2026-10-17 10:00"))
    clock(monkeypatch, at("2026-10-19 09:28"))  # Monday morning: still Friday's close
    data.get_vix_official()
    assert asked == [data.CBOE_VIX]  # the second read uses the saved file


def test_a_cboe_file_saved_before_the_close_is_read_again_once_the_close_is_out(monkeypatch, tmp_path):
    # 2026-10-20 00:50 Sydney (Monday 09:50 New York) saved a file ending 10-16; by 13:15 Sydney Monday's close is out
    answers = iter([CBOE_CSV, CBOE_CSV + MONDAY_ROW])
    asked = []

    def get(url, timeout):
        asked.append(url)
        return Answer(200, next(answers))

    monkeypatch.setattr(data, "RAW_DIR", tmp_path)
    monkeypatch.setattr(data.requests, "get", get)
    clock(monkeypatch, at("2026-10-20 00:50", SYDNEY))
    assert data.get_vix_official().index[-1] == pd.Timestamp("2026-10-16")
    written(tmp_path, at("2026-10-20 00:50", SYDNEY))
    clock(monkeypatch, at("2026-10-20 13:15", SYDNEY))
    vix = data.get_vix_official()
    assert len(asked) == 2 and vix[pd.Timestamp("2026-10-19")] == pytest.approx(18.44)


def test_a_failed_second_try_keeps_the_cboe_file_saved_for_that_close(monkeypatch, tmp_path):
    answers = iter([Answer(200, CBOE_CSV), Answer(503, "busy")])
    asked = []

    def get(url, timeout):
        asked.append(url)
        return next(answers)

    monkeypatch.setattr(data, "RAW_DIR", tmp_path)
    monkeypatch.setattr(data.requests, "get", get)
    clock(monkeypatch, at("2026-10-19 16:40"))  # Monday's close is out; Cboe's file still ends on Friday
    data.get_vix_official()
    written(tmp_path, at("2026-10-19 16:40"))
    clock(monkeypatch, at("2026-10-19 17:15"))
    assert data.get_vix_official().index[-1] == pd.Timestamp("2026-10-16") and len(asked) == 2


def test_vxn_comes_from_its_own_cboe_file(monkeypatch, tmp_path):
    asked = []

    def get(url, timeout):
        asked.append(url)
        return Answer(200, CBOE_CSV.replace("19.990000", "27.130000"))

    monkeypatch.setattr(data, "RAW_DIR", tmp_path)
    monkeypatch.setattr(data.requests, "get", get)
    vxn = data.get_cboe_history("VXN")
    assert asked == [data.CBOE_HISTORY.format(name="VXN")] and vxn[pd.Timestamp("2026-10-16")] == pytest.approx(27.13)


def test_an_unreadable_cboe_file_is_data_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(data, "RAW_DIR", tmp_path)
    monkeypatch.setattr(data.requests, "get", lambda url, timeout: Answer(403, "<html>denied</html>"))
    with pytest.raises(DataUnavailable, match="Cboe"):
        data.get_vix_official()


class FakeTicker:
    """yfinance.Ticker stand-in: hands out the next canned history and counts the downloads."""

    def __init__(self, histories, asked):
        self.histories, self.asked = histories, asked

    def history(self, period, auto_adjust):
        self.asked.append(period)
        return self.histories[len(self.asked) - 1]


def yf_frame(closes, zone):
    """A yfinance-style history: one row per day, dated in the exchange's time zone."""
    index = pd.DatetimeIndex(pd.to_datetime(list(closes))).tz_localize(zone)
    values = list(closes.values())
    return pd.DataFrame({"Open": values, "High": values, "Low": values, "Close": values,
                         "Volume": [1000] * len(values)}, index=index)


def fake_yfinance(monkeypatch, tmp_path, histories):
    asked = []
    monkeypatch.setattr(data, "RAW_DIR", tmp_path)
    monkeypatch.setattr(data.yf, "Ticker", lambda ticker: FakeTicker(histories, asked))
    return asked


def test_asx_history_read_while_the_asx_trades_is_read_again_after_its_close(monkeypatch, tmp_path):
    # Tuesday 10-20 10:00 Sydney the ASX is trading: yfinance's Tuesday row is 74.00 so far; by 20:00 it closed at 74.50
    asked = fake_yfinance(monkeypatch, tmp_path, [yf_frame({"2026-10-19": 73.6, "2026-10-20": 74.0}, SYDNEY),
                                                  yf_frame({"2026-10-19": 73.6, "2026-10-20": 74.5}, SYDNEY)])
    clock(monkeypatch, at("2026-10-20 10:00", SYDNEY))
    assert data.get_prices("IVV.AX")["close"].iloc[-1] == 74.0
    written(tmp_path, at("2026-10-20 10:00", SYDNEY))
    clock(monkeypatch, at("2026-10-20 15:00", SYDNEY))  # still trading: the same file
    data.get_prices("IVV.AX")
    assert len(asked) == 1
    clock(monkeypatch, at("2026-10-20 20:00", SYDNEY))
    assert data.get_prices("IVV.AX")["close"].iloc[-1] == 74.5 and len(asked) == 2


def test_us_index_history_follows_the_new_york_close(monkeypatch, tmp_path):
    asked = fake_yfinance(monkeypatch, tmp_path, [yf_frame({"2026-10-15": 16.46, "2026-10-16": 19.99}, NEW_YORK),
                                                  yf_frame({"2026-10-16": 19.99, "2026-10-19": 18.44}, NEW_YORK)])
    clock(monkeypatch, at("2026-10-20 00:28", SYDNEY))  # Monday 09:28 New York
    assert data.get_prices("^VIX").index[-1] == pd.Timestamp("2026-10-16")
    written(tmp_path, at("2026-10-20 00:28", SYDNEY))
    clock(monkeypatch, at("2026-10-20 13:15", SYDNEY))  # Monday 22:15 New York
    assert data.get_prices("^VIX").index[-1] == pd.Timestamp("2026-10-19") and len(asked) == 2


def test_hong_kong_bars_follow_the_hong_kong_close(monkeypatch, tmp_path):
    asked = fake_yfinance(monkeypatch, tmp_path, [yf_frame({"2026-10-19": 600.0}, HONG_KONG),
                                                  yf_frame({"2026-10-19": 600.0, "2026-10-20": 610.0}, HONG_KONG)])
    clock(monkeypatch, at("2026-10-20 15:00", HONG_KONG))
    assert data.get_bars("0700.HK")["close"].iloc[-1] == 600.0
    written(tmp_path, at("2026-10-20 15:00", HONG_KONG))
    clock(monkeypatch, at("2026-10-20 16:31", HONG_KONG))
    assert data.get_bars("0700.HK")["close"].iloc[-1] == 610.0 and len(asked) == 2
