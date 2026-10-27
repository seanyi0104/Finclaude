---
name: etf-pick
description: 在同一类别里挑选 ETF（如几只标普 500 ETF、几只港股宽基 ETF），由 Jev 给出概率并与「费率最低」基线对照。我给出候选代码并问「选哪只」「比较这几只」时使用。
---
# 同类选基
> 方法和阈值改编自 chia Y《You Buy Because Other People are Buying》（2026-09-29）里的「选 ETF」提示词：https://withchia.substack.com/p/you-buy-because-other-people-are

角色：取数、计算、整理。不给观点、不用买卖用语。所有数学用 Python，不估算。失败即停并报告。

0. 先问我：候选代码（至少 2 只）和基准；确认后运行 finclaude.jev.preflight()。
1. 数据：美股走 Treg，港股走 yfinance。近 3 年日线、规模、费率、成立日期、类型（主动 / 被动）、买卖价差。拿不到的字段填 null 并列在报告里；没有报价时用高低价差近似，并标注 spread_proxy。先列出 Treg 费用，等我确认。
2. 指标（两位小数，对齐基准交易日）：近 1 年 / 3 年年化收益、相对基准超额、3 年最大回撤、1 年波动、跟踪误差（仅被动）、费率、规模、成立年限、20 日日均成交额、价差。
3. 硬过滤（代码执行）：按 profile/ips.md 的门槛剔除；未填时用 chia Y 的默认值（规模 < 1 亿美元、成立 < 1 年、日成交 < 100 万美元、价差 > 0.30%）。港股金额按当日汇率折算成美元再比。字段为 null 不过滤但标注。幸存少于 2 只就停。
4. Jev：一次调用。先用 finclaude.jev.quote(1) 列出费用，等我确认后调用 finclaude.jev.evaluate(..., max_cost_usd=确认的金额, save_as=报告文件夹/jev_result.json)。choice 题，选项 = 幸存者，state = 每只一行指标；用 finclaude.jev.ranking 取概率前 1–2。
5. 基线：费率最低的那只；报告 Jev 与基线是否一致。
产出：结论要点写进当周周报文档的 Decision；文件存到 reports/<周>/_附件/etf-pick/：metrics.csv（每个字段注明数据商）、jev_result.json、report.html（费用、缺失字段、近似值、剔除项、基线一致性；用 finclaude.render.save_report 生成）。
