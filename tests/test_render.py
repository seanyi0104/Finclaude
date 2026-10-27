"""Reports as readable HTML pages: tables, nested lists and fold-out explanations rendered, light and dark theme.
Only the .html is saved; the Markdown is just the way the page is written."""
from finclaude import render

REPORT = """# 组合体检　2026-10-06

**本周触发 3 条规则**。

| 持仓 | 市值（澳元） |
| --- | --- |
| ORCX | 1,389.84 |

- 近 90 天评级动作：维持 12
    - 2026-10-02 Citizens：重申 Market Outperform
"""


def test_tables_bold_and_nested_lists_are_rendered():
    page = render.to_html(REPORT)
    assert "<table>" in page and "<td>ORCX</td>" in page
    assert "<strong>本周触发 3 条规则</strong>" in page
    assert page.count("<ul>") == 2  # the analyst change sits inside its parent item
    assert "|" not in page.split("<body>")[1]


def test_a_list_right_under_a_line_of_text_is_still_a_list():
    # CommonMark lets a list follow a paragraph line directly; Python-Markdown needs a blank line, so render adds one
    page = render.to_html("**上涨概率**（到期那天）\n- 1 个月：历史 38.7%\n- 3 个月：历史 23.7%\n")
    assert "<li>1 个月：历史 38.7%</li>" in page and "<li>3 个月：历史 23.7%</li>" in page


def test_fold_out_explanations_render_their_markdown():
    page = render.to_html('<details markdown="1">\n<summary>怎么算的</summary>\n\n- 上限 = **27.22** 美元\n\n</details>\n')
    assert "<details>" in page and "<summary>怎么算的</summary>" in page
    assert "<li>上限 = <strong>27.22</strong> 美元</li>" in page


def test_page_takes_its_title_from_the_first_heading_and_has_both_themes():
    page = render.to_html(REPORT)
    assert "<title>组合体检　2026-10-06</title>" in page
    assert '<meta charset="utf-8">' in page
    assert "prefers-color-scheme: dark" in page


def test_report_is_saved_as_html_only(tmp_path):
    folder = tmp_path / "reports" / "checkup" / "2026-10-06"
    page = render.save_report(REPORT, folder)
    assert page == folder / "report.html"
    assert "<td>1,389.84</td>" in page.read_text(encoding="utf-8")
    assert [p.name for p in folder.iterdir()] == ["report.html"]  # no .md copy next to it
