---
name: overlap
description: 检查我持有或打算买的几只基金是否重复，由 Jev 判断是否冗余、重复组里留哪只。我问「这几只重不重叠」「分散够不够」时使用；/checkup 每季度也会调用。
---
# 持仓重叠检查
> 方法和阈值改编自 chia Y《You Buy Because Other People are Buying》（2026-09-29）里的「查重叠」提示词：https://withchia.substack.com/p/you-buy-because-other-people-are

角色：数据工程。取数、计算、整理；判断交给 Jev。不用买卖用语。失败即停并报告。

0. 先问我要比较哪些基金（至少 2 只；默认用 profile/holdings.csv 里的全部 ETF），展示清单等我确认；再列出 Treg 费用等我确认。
1. 数据：每只的前 50 大持仓与权重（拿不到就用前十大）、1 年日线、费率、规模；注明持仓披露日期。
2. 每一对计算（两位小数；跨市场时按共同交易日对齐；用 finclaude.metrics）：
   - overlap_pct = 共同持仓的较小权重之和
   - shared_top10 = 前十大重合数
   - corr_1y = 1 年日收益相关性
   - corr_stress = 基准（美股 SPY、港股 2800.HK）最差 10% 交易日上的相关性
   - te_1y = 相互跟踪误差
3. Jev：D1、D2 各一次调用。先用 finclaude.jev.quote(2) 列出费用，等我确认后调用 finclaude.jev.evaluate(..., max_cost_usd=确认的单价上限, save_as=报告文件夹/jev_D1.json 或 jev_D2.json)。D1 每一对选「重复 / 部分重叠 / 分散」；P(重复) ≥ 0.5 的对连成组；D2 每组选留哪只（state 写每只的费率、规模、组内跟踪误差）。
4. 基线：相关性 ≥ 0.95 或重叠度 ≥ 50% 判重复（阈值可在 IPS 改），组内留费率最低的；报告是否一致。
产出：结论要点写进当周周报文档的 Decision；文件存到 reports/<周>/_附件/overlap/：pairs.csv、top10_side_by_side.csv（共同持仓标 *）、jev_D1.json、jev_D2.json、report.html（用 finclaude.render.save_report 生成）。
结尾提醒：危机时相关性会上升，分散能降低风险，但不能消除风险。
