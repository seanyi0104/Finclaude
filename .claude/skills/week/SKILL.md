---
name: week
description: 每周交付的开关：先收尾上周（周五盘后的持仓回顾和展望、到期记分、精简存档、记忆），再开新一周（周报文档加标签、官方日历、会话改名）。我在新会话里打 /week，或说「开始新的一周」「本周收尾」时使用。
---
# 每周：收尾上周，开新一周

一周 = 美东时间周一到周五。先用 weekly.close_and_open(weekly.us_day()) 得到要收尾的周和要开的周的周一（工作日运行：收尾上周、开本周；周末运行：收尾刚结束的这周、开下周）。我只看周报文档（Claude Docs，链接写在 CLAUDE.local.md；用 docs 工具读写，先 read 文档拿到各标签的 id）和 reports/<周>/ 的精简存档，其余归你管。没有周报文档（没有 Claude Docs 或没写链接）时：三页直接写成 reports/<周>/News.md、Decision.md、Lesson.md，下面说的「标签」「子标签」都换成这三个文件，术语表勾选直接改 profile/glossary.json。所有数字用 Python 算；拿不到就写「无可靠数据」和原因。

## A. 收尾上周（美东周五收盘后才做；reports/<上周>/ 已有三份存档就跳过）
1. 到期记分：forecast.score()，再 forecast.scoreboard()。在上周 Decision 的「到期的预测」写：哪几条到期、对了几条、各来源的 Brier 分数（抛硬币是 0.25）。没有到期的，照实写最早一批的日期。
2. 持仓回顾和展望：weekly.friday_review(周一, 周五) 写进上周 Decision（Moomoo 只读：持仓、资金、现金流水；OpenD 没开时自动用 profile/ 的快照，并注明）。它先写「这周账户赚 / 亏多少澳元」（总资产对比 data/weekly/ 里上一周的记录，扣掉入金和出金），再逐只写；这周的总额存进 data/weekly/<周五>.json，下周对比。美股在交易（盘前、盘中、盘后、夜盘）时 Moomoo 按当时的价格给持仓估值，所以这一步最好在美东周五 20:00（盘后结束）到周日 20:00（夜盘开始）之间做（我的本地时间按 CLAUDE.local.md 的时区，用 Python 换算）；在交易时段读的，回顾里会自动注明。美股、港股、澳股都要覆盖；读不到的代码照实写，不能悄悄跳过。只有当月第一周加「月度体检」（/checkup），IPS 提醒只在那里出现。
3. 精简存档：读上周的三个子标签，写 reports/<上周>/News.md、Decision.md、Lesson.md（文件夹名用 weekly.folder_name(周一)）。只留核心结论和洞察，不是全文导出；用列表，不用宽表：
   - News：最重要的官方事件（每条保留原文链接）、下周日历、持续热点、没读到的来源。
   - Decision：每只股票的一屏卡片、持仓回顾和展望、到期记分。
   - Lesson：本周的卡片，和术语表里我勾选的变化。
   附件（jev_result.json、完整报告）放 reports/<上周>/_附件/。
4. 记忆：跨周有用的东西写进记忆（我的偏好变化、哪个来源更准、我反复问的概念）。
5. 告诉我：上周已存档，旧会话可以删了。

## B. 开新一周
1. 建文件夹 reports/<本周>/（weekly.folder_name(周一)）。
2. 周报文档加标签（用周报文档时；一个 batch）：父标签名 weekly.tab_name(周一)，order = weekly.tab_order(周一)（新的一周排最前，「术语表」order z0 一直在最后）；下面三个子标签 News（order a1）、Decision（a2）、Lesson（a3），subtabOf 指向父标签。父标签写本周概要：美东日期和对应的本地时间（时区见 CLAUDE.local.md；用 Python 换算，两地夏令时可能不同步）、三个子标签各放什么、看不懂的词先查术语表。
3. News：本周官方日历（持仓和关注股票的财报日、BLS / BEA 发布、FOMC），只收官方页面，每条带链接；再补到最近一个交易日的官方公告，跟进持续热点，加 VIX、VXN 各一行：用 data.get_cboe_history("VIX" / "VXN")（Cboe 官方，只有已收盘的日子）和 analysis.vix_view(那个序列, 它的最后一天) 写收盘点位、比前一天、比过去一年多少日子高，来源写 Cboe，带日期。公司已公布的财报日写进 journal/official_events.csv（ticker,event,date,time,url,checked）。读不到的官方页面写进「这周没读到的」。
4. 术语表勾选（用周报文档时）：read「术语表」标签，把返回的 xml 存成临时文件，glossary.save(glossary.marks_from_doc(xml), 美东今天)；Lesson 的用词深浅靠它。
5. 会话改名为 weekly.tab_name(周一)（mcp__ccd_session_mgmt__set_session_title；Claude 桌面版有这个工具时）。

## 平时（在当周会话里；规则见 CLAUDE.md 输出约定）
- 我当天第一次开口：先用 mcp__ccd_session_mgmt__get_usage 看会话上下文（有这个工具时），超过 30 万 token 就提醒我换新会话；再看 News 更新到哪天，落后就按固定清单补到最近一个交易日（关注池公司在 SEC 的 8-K、当天 BLS / BEA / 美联储发布、VIX / VXN 一行；不读长文件，不收博客和 Form 4 / 144），顺带按 B.4 更新术语表勾选，再回答。
- 聊到某只股票：analysis.prepare(代码) → analysis.card_markdown(prep)，写进本周 Decision；Jev 用 analysis.jev_odds(prep, max_cost_usd=jev.auto_budget()) 自动调用（每月 1 美元内，不用问我）；auto_budget 抛 CostNotApproved 时才用 analysis.jev_quote() 列「端点 | 次数 | 单价 | 合计」，等我确认；概率用 forecast.log_forecasts(analysis.journal_rows(prep)) 记日志。
- 聊到不在关注池的公司（watchlist.is_watched 为假）：出卡后用弹窗问我要不要加进关注池，选项写出建议的分类和细分（分类先用 watchlist.BUCKETS 里我定的方向，都不合适就新开一类）；我同意就 watchlist.add(...)；用周报文档时，再在「关注池」标签对应分类的表格末尾加一行、更新标题里的家数（新分类就加一节）。
- 我在美股盘中、盘前、盘后或夜盘时问到某只美股：卡片照常按最近收盘价；另起一行写 quote.extended_hours(["US.代码"]) 读到的当前时段价格、涨跌和美东时间（这个时段还没有成交就照实写）。
- 公司官宣了财报日：写进 journal/official_events.csv（附原文链接、核对日期），卡片会写「公司已公布」。
- 我的提问（聊天、文档里 @ 的评论、在文档里高亮写的问题）：在问的地方先简答（评论用 docs 工具 create utterance，不用 Artifact 评论工具；高亮的问题答完就清掉）；同时把问题原话、出处标签和完整解答记成本周 Lesson 卡片：知识卡（一句话、类比、这周的真实数字、常见误区，计算过程放最后）或研究卡（事实 → 原因 → 其他解释 → 对我的影响，带来源）；卡片标题后放「你的掌握」下拉（术语表同一个 enum），我标半懂 / 不懂的下次换个讲法补；新词加进术语表。
- News、Decision 用取整的数字，不夹讲解；术语表里我没标「懂了」的词，指向术语表或 Lesson。

结尾（投资相关）：「教育用途，非投资建议；决定权在你。」
