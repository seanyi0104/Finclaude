"""The glossary marks (懂了 / 半懂 / 不懂) kept from the weekly doc, and the card's pointer to words not yet 懂了."""
import pytest

from finclaude import glossary

CARD = """### NVDA　214.13 美元（2026-10-16 收盘）

一句话：长线还在上升，短线在回调；未来 2 周有 68% 的可能在 196–233 美元之间。

- 2 周（到 2026-10-30）：区间 196–233｜上涨概率 历史 56% · 期权 49% · Jev 未调用
- 参考价位：MA20 220 · MA200 190 · 近半年高 243 / 低 164（只是参考点，不是预测）
- 市场情绪：VIX（恐慌指数）20.0，比前一天 +21%，比过去一年 82% 的日子高（2026-10-16 收盘，yfinance）
"""


@pytest.fixture
def marks(monkeypatch, tmp_path):
    monkeypatch.setattr(glossary, "GLOSSARY_JSON", tmp_path / "glossary.json")
    glossary.save({"收盘价": "懂了", "每股盈利（EPS）": "懂了", "支撑 / 压力（前高前低）": "半懂", "均线（MA20、MA200）": ""},
                  "2026-10-19")


def test_marks_are_kept_with_their_date(marks):
    kept = glossary.load()
    assert kept["收盘价"] == "懂了" and kept["支撑 / 压力（前高前低）"] == "半懂"
    assert glossary.as_of() == "2026-10-19"


def test_only_words_the_card_uses_and_not_yet_known_are_listed(marks):
    words = glossary.unknown_terms(CARD)
    assert "收盘价" not in words  # marked 懂了
    assert "上涨概率：Jev" not in words  # the card has no Jev number
    assert "市盈率（P/E）" not in words  # not in this card
    assert words == ["均线（MA20、MA200）", "长线 / 短线趋势", "正常波动区间（68%）", "上涨概率：历史", "上涨概率：期权",
                     "支撑 / 压力（前高前低）", "VIX（恐慌指数）"]


def test_the_pointer_names_short_words_and_the_glossary(marks):
    line = glossary.pointer(CARD)
    assert line.startswith("生词") and "术语表" in line
    assert "均线、长线 / 短线趋势、正常波动区间、上涨概率（历史 / 期权）、支撑 / 压力、VIX" in line


def test_no_pointer_once_every_word_is_known(monkeypatch, tmp_path):
    monkeypatch.setattr(glossary, "GLOSSARY_JSON", tmp_path / "glossary.json")
    glossary.save({term: "懂了" for term in glossary.CARD_TERMS}, "2026-10-19")
    assert glossary.pointer(CARD) is None


def test_without_saved_marks_every_card_word_counts_as_new(monkeypatch, tmp_path):
    monkeypatch.setattr(glossary, "GLOSSARY_JSON", tmp_path / "missing.json")
    assert glossary.load() == {} and "收盘价" in glossary.unknown_terms(CARD)


def test_marks_are_read_from_the_glossary_tab_as_the_docs_read_returns_it():
    xml = ("<doc node='a819f294-6369' rev='52'><paragraph id='m941d6tnyxq.0' heading='1'><text id='.1'>术语表</text>"
           "</paragraph><table id='m941d6tnyxq.116'><row id='.117'><cell id='.118' header='true'><paragraph id='.119'>"
           "<text id='.120'>术语</text></paragraph></cell><cell id='.122' header='true'><paragraph id='.123'><text id='.124'>"
           "一句话</text></paragraph></cell><cell id='.127' header='true'><paragraph id='.128'><text id='.129'>你的掌握</text>"
           "</paragraph></cell></row><row id='.133'><cell id='.134'><paragraph id='.135'><text id='.136'>收盘价</text>"
           "</paragraph></cell><cell id='.139'><paragraph id='.140'><text id='.141'>一天交易结束时的最后成交价</text></paragraph>"
           "</cell><cell id='.154'><paragraph id='.155'><dropdown id='.156' enum='07022354-8f97' index='0'>懂了</dropdown>"
           "</paragraph></cell></row><row id='.157'><cell id='.158'><paragraph id='.159'><text id='.160'>均线（MA20、MA200）"
           "</text></paragraph></cell><cell id='.174'><paragraph id='.175'><text id='.176'>过去 20 天 / 200 天收盘价的平均数"
           "</text></paragraph></cell><cell id='.204'><paragraph id='.205'><dropdown id='.206' enum='07022354-8f97'/>"
           "</paragraph></cell></row></table></doc>")
    assert glossary.marks_from_doc(xml) == {"收盘价": "懂了", "均线（MA20、MA200）": ""}
