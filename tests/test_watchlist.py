"""The watchlist: the companies the user follows, sorted into the areas they care about, growing as they chat."""
import pandas as pd
import pytest

from finclaude import watchlist


@pytest.fixture
def kept(monkeypatch, tmp_path):
    monkeypatch.setattr(watchlist, "WATCHLIST_CSV", tmp_path / "watchlist.csv")
    watchlist.add("NVDA", "英伟达", "M7", "AI 芯片", tags="也属于半导体", added="2026-10-20")
    watchlist.add("SOXL", "3 倍做多半导体 ETF", "半导体", "半导体指数（每日 3 倍）", tags="杠杆 3 倍", added="2026-10-20")
    watchlist.add("IVV.AX", "iShares 标普 500 ETF", "宽基 ETF（长期仓）", "标普 500（澳元）", added="2026-10-20")


def test_companies_are_added_once_and_looked_up_by_ticker(kept):
    assert watchlist.is_watched("nvda") and not watchlist.is_watched("AMD")
    assert watchlist.add("NVDA", "英伟达", "M7", "AI 芯片") is False  # already there: nothing changes
    assert len(watchlist.load()) == 3


def test_the_table_groups_by_area_marks_holdings_and_keeps_empty_areas(kept):
    md = watchlist.markdown(held={"NVDA", "IVV.AX"})
    assert md.index("## M7") < md.index("## 半导体") < md.index("## 能源") < md.index("## 宽基 ETF（长期仓）")
    assert "| NVDA | 英伟达 | AI 芯片 | 持仓 · 也属于半导体 | 2026-10-20 |" in md
    assert "| SOXL | 3 倍做多半导体 ETF | 半导体指数（每日 3 倍） | 杠杆 3 倍 | 2026-10-20 |" in md
    assert md.split("## 能源")[1].split("## ")[0].count("还没有") == 1  # an area the user named, still empty


def test_a_new_area_appears_after_the_named_ones(kept):
    watchlist.add("LLY", "礼来", "医药", "减肥药与糖尿病药", added="2026-10-21")
    md = watchlist.markdown(held=set())
    assert md.index("## 宽基 ETF（长期仓）") < md.index("## 医药（1 家）")


def test_an_unknown_ticker_needs_a_name_and_an_area(kept):
    with pytest.raises(ValueError):
        watchlist.add("AMD", "", "半导体", "CPU 与 GPU")
    with pytest.raises(ValueError):
        watchlist.add("AMD", "超威半导体", "", "CPU 与 GPU")
    assert isinstance(watchlist.load(), pd.DataFrame)
