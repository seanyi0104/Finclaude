"""The glossary marks (懂了 / 半懂 / 不懂) from the weekly doc's 术语表 tab, kept in profile/glossary.json, so a Decision
card can end with the words it uses that are not yet marked 懂了 (CLAUDE.md 输出约定: such words point to the
glossary). Copy the marks in with save() whenever the tab is read (/week, or the first chat of a day).
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET

from .config import PROFILE_DIR

GLOSSARY_JSON = PROFILE_DIR / "glossary.json"
KNOWN = "懂了"
# Each glossary term (as named in the 术语表 tab, in its order) and what shows it in a card.
CARD_TERMS = {
    "收盘价": r"收盘",
    "均线（MA20、MA200）": r"MA\d|均线",
    "长线 / 短线趋势": r"长线|短线",
    "正常波动区间（68%）": r"68%|区间 \d",
    "上涨概率：历史": r"上涨概率 历史",
    "上涨概率：期权": r"· 期权 \d",
    "上涨概率：Jev": r"Jev \d",
    "到期记分（Brier 分数）": r"到期记分|Brier",
    "隐含波动率（IV）": r"隐含波动率|\bIV\b",
    "财报预期波动": r"期权预期当天",
    "每股盈利（EPS）": r"EPS",
    "分析师预期修正": r"EPS 预期[上下]调",
    "市盈率（P/E）": r"市盈率|P/E",
    "杠杆 ETF（如 SOXL）": r"杠杆",
    "复权价": r"复权",
    "支撑 / 压力（前高前低）": r"近半年高|前高|前低|支撑|压力",
    "VIX（恐慌指数）": r"VIX",
    "VXN（纳指恐慌指数）": r"VXN",
    "RSI（相对强弱指数）": r"RSI",
    "MACD": r"MACD",
}


def marks_from_doc(xml: str) -> dict[str, str]:
    """The marks in a read of the 术语表 tab (the xml a docs read returns): each row's first cell is the term, the
    dropdown in its last cell the mark ("" when left unmarked); the header row is skipped."""
    marks = {}
    for row in ET.fromstring(xml).iter("row"):
        cells = row.findall("cell")
        if len(cells) < 2 or cells[0].get("header") == "true":
            continue
        dropdown = cells[-1].find(".//dropdown")
        marks["".join(cells[0].itertext()).strip()] = (dropdown.text or "").strip() if dropdown is not None else ""
    return marks


def save(marks: dict[str, str], as_of: str) -> None:
    """Keep the marks as read from the 术语表 tab ("" for a row left unmarked) and the date they were read."""
    GLOSSARY_JSON.write_text(json.dumps({"as_of": as_of, "source": "周报文档「术语表」", "terms": marks},
                                        ensure_ascii=False, indent=1), encoding="utf-8")


def _kept() -> dict:
    return json.loads(GLOSSARY_JSON.read_text(encoding="utf-8")) if GLOSSARY_JSON.exists() else {}


def load() -> dict[str, str]:
    return _kept().get("terms", {})


def as_of() -> str | None:
    return _kept().get("as_of")


def unknown_terms(text: str, marks: dict[str, str] | None = None) -> list[str]:
    """Glossary terms the text uses that are not marked 懂了, in the glossary's order."""
    marks = load() if marks is None else marks
    return [term for term, shown in CARD_TERMS.items() if marks.get(term) != KNOWN and re.search(shown, text)]


def pointer(text: str, marks: dict[str, str] | None = None) -> str | None:
    """The card's last line: its words not yet 懂了, short names, pointing to the glossary; None when there are none."""
    groups: list[tuple[str, list[str]]] = []
    for term in unknown_terms(text, marks):
        head, _, tail = term.split("（")[0].strip().partition("：")
        if tail and groups and groups[-1][0] == head and groups[-1][1]:
            groups[-1][1].append(tail)
        else:
            groups.append((head, [tail] if tail else []))
    if not groups:
        return None
    words = "、".join(f"{head}（{' / '.join(tails)}）" if len(tails) > 1 else f"{head}：{tails[0]}" if tails else head
                     for head, tails in groups)
    return f"生词（还没标「懂了」，见术语表）：{words}"
