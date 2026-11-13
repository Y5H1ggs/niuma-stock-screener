# pitfalls 速查索引

> **本文件由 `scripts/make_index.py` 自动生成，禁止手改。**
> 只读这里（约 10 KB）就能定位问题；需要细节再用编号去 `pitfalls.md` 搜。
> 全文 130 条，正文约 103 KB —— **默认不要整篇读正文**。

| 编号 | 一句话判据 | 关键词 |
|---|---|---|
| **P5** | f100 行业字段大面积失真 | playbook.md |
| **P6** | 新浪 K 线是 JS 对象字面量（非严格 JSON） | json.loads |
| **P7** | 新浪 K 线末根是"前一交易日" | — |
| **P8** | 东财 push2his K 线盘中会限流 | — |
| **P9** | 腾讯快照解析按请求顺序对齐 | — |
| **P10** | ut token 失效 ≠ IP 被拉黑 | — |
| **P11** | 编码问题：Git Bash heredoc 追加中文会长出坏字节 | — |
| **P12** | Windows / PowerShell 中文路径与 .bat | printf |
| **P13** | 指标计算细节 | — |
| **P14** | 主力净额 ≠ 大资金介入（判读坑） | playbook.md |
| **P15** | 腾讯 fqkline?qfq 会返回 HTTP 501，必须准备不复权降级链 | hfq ds.kline_qq getKLineData |
| **P16** | 新浪 K 线 volume 单位是「股」，腾讯是「手」 | getKLineData volume ds.kline_sina |
| **P17** | 取数失败绝不能在评分/评级里静默计负分 致命 | ulist |
| **P18** | 日线级资金流 fflow/daykline 会整段限流 | ulist.np |
| **P19** | HTML 报告的「卡片重叠」是父卡没给内边距，不是 CSS 写错 视觉坑 | padding margin div |
| **P20** | 报告"全卡片化"：小板块不独立成卡就不像成品 | cnt |
| **P21** | Git Bash cat >> heredoc 追加中文，可能静默失败 | — |
| **P22** | 盘前/竞价时段：腾讯与新浪快照返回"今日未开盘"空壳 最贵的坑 | qt.gtimg.cn hq.sinajs.cn |
| **P23** | 东财 clist 行情字段返回 - 占位符，_fl("-") == 0.0 会静默伪造数据 | clist None sector_stats |
| **P24** | 指数必须带显式市场前缀，裸 000001 是平安银行 | tsym sz000001 sh000001 |
| **P25** | "封死"判定别拿卖档价格比涨停价 | — |
| **P26** | 盘前最容易犯的错是「口径混用」：个股用昨收、板块用今日竞价 | — |
| **P27** | 东财百分比字段是 ×100 存储 —— 不除 100 会印出「主力净占比 411%」 | ulist f184 |
| **P28** | 加分项只认方向不认强度 → 普通上涨票也能拿资金面满分 | — |
| **P29** | 1 天样本推不出"连续性" | — |
| **P30** | 主维度双缺失时必须「不予评级」，不要输出一个中性分数 | no_rating stop_loss_pct fee_rate_roundtrip |
| **P31** | 竞价只看单一时点 = 把「假高开」当接力到位 | — |
| **P32** | 「竞价量 ≥ 昨日 8%」未按个股成交量基数校准 | — |
| **P33** | 板块级加分与该股无关，把"被主线落下的票"抬成中性 | — |
| **P34** | 相对强度拿了「最热的那个板块」当基准 —— 大板块中位数天然偏低 | _skey best peer |
| **P35** | P34 的"取成分数最少者"仍是一个挑的动作 —— 同一只票能拿到两个相反结论 | f127 f129 f128 |
| **P36** | 同一文件并行编辑会静默互相覆盖（工具用法坑） | ds.clist ast.parse |
| **P37** | 盘中报告的两个口径错（2026-09-21 实测暴露，已修） | — |
| **P37.1** | 盘中"量能倍数"口径不可比 —— 会把放量算成缩量并倒扣分（真 bug） | — |
| **P37.2** | 成交额单位漏除（渲染 bug） | amount |
| **P38** | 「题材基准」在盘中会漂移（P35 的残余风险） | — |
| **P39** | 「可交易性闸门」——强势 ≠ 可买入（2026-09-21 用户实测反馈） | — |
| **P40** | 早盘开盘初段，clist 的成交额/换手/量比字段大面积返回占位符（2026-09-22 实测） | clist f10 f62 |
| **P41** | 板块代码不可凭记忆硬编码 —— 必须用 board_index() 动态取 | BK0459 BK1036 rec |
| **P42** | 挑基准的代码用了元组下标，把"涨幅"当"主力净额"比（09-22 实测） | — |
| **P43** | sector_members() 的 f6（成交额）盘中返回占位符（09-22 实测） | sector_members |
| **P44** | 模块在 import 时重绑 sys.stdout → 关闭调用方的 stdout（09-22 实测） | journal.py risk.py pit.py |
| **P45** | ds.boll() 的键名是 low，不是 dn（09-22 实测） | low |
| **P46** | Chrome --print-to-pdf 不认相对路径（09-22 实测） | DEVNULL |
| **P47** | 单位陷阱：腾讯快照成交额是万元，东财 f48 是元（09-22 实测） | amount f48 |
| **P48** | 数据目录与脚本同名 → import 被目录遮蔽（09-22 实测） | ModuleNotFoundError |
| **P49** | predict.py 的"触及保本价"按 100 股硬编码 → 概率系统性偏保守（09-22 实测） | — |
| **P50** | selfcheck 只记录失败项 → 全通过时像没检查（09-22 实测） | append |
| **P51** | predict.stats() 缺样本充足性标记 → 用 2 个样本冒充结论（09-22 实测） | reliable smoketest predict |
| **P52** | smoketest --seed 无法真正复现（09-22 实测） | clist |
| **P53** | risk.check 只回 allow=False 不给原因（09-22 实测） | reasons allow |
| **P54** | K 线日期刻度压在成交量柱上（09-22 实测，截图反馈） | — |
| **P55** | ds.kline() 返回二元组，但 docstring 没写 → 静默画空白图（09-22 实测） | report.py calibrate.py health.py |
| **P56** | rg 不支持 lookbehind → 静默返回"无匹配"，差点把回归推上线（09-22 实测） | — |
| **P57** | 同一文件的两处 Edit 并发提交 → 后写覆盖前写（09-22 实测，两次） | report.py charts.py |
| **P58** | ds.ulist() 只接受列表，传字符串会静默返回 0（09-22 实测） | data.diff |
| **P59** | 抬头卡右上角整片空白 —— flex-wrap 下长文本把兄弟元素挤走（09-22 实测，截图反馈） | — |
| **P60** | 新写的 CSS 类名撞上已有的全局工具类 → 元素白捡一块底色（09-22 实测） | hvup hvlo |
| **P61** | 视觉类缺陷必须"渲染成图片"验收，文本/结构检查一律查不出（09-22 实测） | pymupdf |
| **P62** | predict.py 案例列表：日期取的是次日、特征取的是信号日（09-22 实测） | — |
| **P63** | 提交钩子只装在 .git/hooks/ → 换机器后无声消失（09-22 实测） | git core.hooksPath |
| **P64** | 钩子脚本被转成 CRLF → sh 报 \r: command not found，闸门静默失效（0… | — |
| **P65** | grep -c $'\r' 数 CR 会假阳性 —— 计数类检查必须用"不会自扰"的方法（09-22… | grep |
| **P66** | python -c "…" 里的反斜杠被 shell 吞掉 → 正则静默失效（09-22 实测） | — |
| **P67** | 取数失败的 None 直接被送进格式化字符串 → 整个脚本崩溃（09-23 实测） | mood.py zl_yi None |
| **P68** | 全量抓取在"间歇空返回"下的耗时不可控 → 直接吃掉整个尾盘窗口（09-23 实测） | — |
| **P69** | 用 grep "main -> main" 判 push 成功 → 把「被拒绝」也当成成功（09-2… | a3782ec |
| **P70** | sys.path 首位插成"安装目录" → import journal 命中另一份台账，数据静默分… | journal.py sys.path calibrate.py |
| **P71** | 凭"感觉过了多久"判断时间 → 报错了市场状态（09-23 实测） | — |
| **P72** | ds.board_index() 失败页 break → 板块索引静默截断，同一名称两次查询结果不同… | None break diff |
| **P73** | 单次取数失败被写成"接口已不可用" → 一维数据被永久判死（09-23 实测） | report.py |
| **P74** | 日线资金流收盘后才把当日入表 → 同一个"近 N 日"在盘中与盘后指向不同窗口（09-23 实测） | — |
| **P75** | health.py 把"没抛异常"判成"可用" → 报「9/9（100%）」而实际全空（09-23 … | None report.py health.py |
| **P76** | 把「接口级 RST」误诊成「IP 级阻断」→ 3 小时里一直在"等恢复"，其实早有降级源（09-23… | push2.eastmoney.com quote.eastmoney.com push2 |
| **P77** | 熔断器没按主机族隔离 → 一个源挂掉，把可用源一起短接（09-23 实测） | families |
| **P78** | datacenter-web 是 push2 的可用降级源：四张报表覆盖资金面/板块面/板块归属（0… | RPT_DMSK_TS_STOCKNEW PRIME_INFLOW PRIME_COST |
| **P79** | 板块成分 pageSize<500 时静默截断 → "名单里找不到本股"被当成"不在该板块"（09-… | error seen |
| **P80** | 降级后行业基准整个丢失：最粗一级行业不在索引里，而代码只试了它（09-23 实测） | industries selfcheck.check_board_alignment |
| **P81** | 数据明明取到了，却在渲染层被判"不可用"丢掉 —— 与伪造数据是同一类失真（09-23 实测） | zl_yi _has_n |
| **P82** | 盘前调用 sector_stats()/mood()：用上一交易日的涨跌幅冒充今日红盘率（09-24… | None |
| **P83** | 两套「板块归属」体系随 push2 通断自动切换，且不告知调用方 → 同一只票两次给出不同板块集合（… | _src sector_compare continue |
| **P84** | 盗用行情软件的名字标注自定义指标 + K 线末根语义不一致（10-09 实测，使用者亲自指出） | report.py ds.py None |
| **P85** | 「缩窗口 / 静默丢 / 用 0 冒充」三件套一起修完，仍留下一个接口级教训（10-09 修毕） | sector_compare ds.py report.py |
| **P86** | 建"预测模型"时最容易犯的三个错：随机切分、横截面不可比、只报好结果（10-09 实测） | macd_gold rsi6 chg |
| **P87** | 「因子恒定不变」与「零样本照常出表」—— 又一次"没有"被伪装成"无效"（10-09 实测） | daily_factors amt_log turnover |
| **P88** | 「平均指标」掩盖时间不稳定 + 「AUC>0.5 却 Brier≥基线」的真因是校准（10-09 实测） | — |
| **P89** | 因子有效性完全取决于股票池 —— 不固定池子，任何"因子有效/无效"的结论都不可比（10-09 实测） | amt_log rev_log pos250 |
| **P90** | 因子数量 ≠ 独立信息维度：加到 100 个特征，样本外零提升（10-09 受控实验） | model.py daily_factors max_dd20 |
| **P91** | 股票池的前视偏差：用「期末价格」筛池、却回溯历史（10-09 实测并修复） | make_pool.py pos250 amt_log |
| **P92** | clist 的「涨幅降序分页」不等于全市场样本 —— 用它算涨跌家数会得到反向结论 | — |
| **P93** | 多字段元组的「列序」颠倒，会把数据安静地放进错的格子 | — |
| **P94** | 平仓后现金不回账 —— docstring 说有，代码里没有 | account.json cash peak_equity |
| **P95** | 「概率」输出出现 0% / 100% 的绝对表述 | — |
| **P96** | 「经营现金流为负」不是结论 —— 单期判断会得出相反答案 | — |
| **P97** | 多个股东的环比变动是同一个数字 → 那是送转除权，不是减持 | FREE_RATIO_QOQ Counter transfer_note |
| **P98** | 对二值/离散变量算分位 = 制造出没有意义的"极值" | new_high60 |
| **P99** | 布尔类因子 bool 与 float 混用 —— JSON 里变成 true/1，下游统计静默出错 | above20 bull yang |
| **P100** | 在 CHANGELOG 里"贴验证结果"，把账户口径带进了公开仓库 | check_privacy.py e307c4e |
| **P101** | 批量快照按序号对齐 → 中间缺一只，后面全部张冠李戴 | snapshot qt.gtimg.cn enumerate |
| **P102** | 第三方 SDK「ok:true 但字段全空」—— 三处静默错位（v1.2.42） | klines members code |
| **P103** | 上传前「自查」逼出的问题（v1.2.42） | ds_ths.py _ths_worker.py copy.deepcopy |
| **P104** | 动态冒烟把「取数成功但返回空」算成通过 | ds.snapshot faults.append faults |
| **P105** | 盘前空数据让动态冒烟「0 只票被测却报通过」（v1.2.43） | runtime_smoke faults except |
| **P106** | 上架适配时踩的两个「自检器自己出错」（v1.2.44） | _CLIST_META dict.update fields |
| **P107** | 「净额 ≠ 广度」与「条件基准率必须分段」（v1.2.45） | model.py rsi6 |
| **P108** | 板块代码凭记忆写 → 整段数据是别的板块，而且不报错 | BK0475 f58 BK1030 |
| **P109** | 把多组基准率各自报成区间 → 概率必然自相矛盾 | prob.py threshold_probs calibrate.py |
| **P110** | 返回条数正常，但字段整批是 None | — |
| **P111** | card() 漏一个 </div>，而「section 配对」断言照样通过 | div check_html.py HTMLParser |
| **P112** | 「这个源不支持该标的」——其实是标识符规则算错（v1.2.47） | sh000001 BK1030 BK1200 |
| **P113** | 概率模型看不见板块 β（v1.2.47） | prob.py conds ds.secid |
| **P114** | 文档宣称的规则，代码里根本没实现（v1.2.48） | position_mode kelly config.json |
| **P115** | 「半凯利」出现负值 —— 那一列装的不是半凯利 | check_html.py snake_case |
| **P116** | 同一个字段名 f2，clist 和 ulist 是两个量纲 | clist ulist f10 |
| **P117** | 自造评分函数 = 换了个名字的 model.py | memory model.py f62 |
| **P118** | 否决判据默认「无罪」——取不到数据就放行 | sector_flow None |
| **P119** | 免责声明不是许可证 | — |
| **P120** | 证据重复计数：同一份数据的两种读法被当成两重验证 | — |
| **P121** | 「价差」不是「涨跌幅」——我一边复盘这个边又犯了一次 | chg_vs_prevclose chg_vs_0945 chg |
| **P122** | 日 K 线末根是哪一天，是运行时变量，不是接口常量 | raw_last has_today warn |
| **P123** | 风控回撤恒为 0 —— 因为第一笔就亏，if peak > 0 永远不成立 | dd_cur peak_equity |
| **P124** | 「现金不足」与「仓位规则不可执行」是两件事 | budget |
| **P125** | 小资金下，凯利仓位与一手门槛在数学上冲突 | — |
| **P126** | 隐私清单靠手写 → 只覆盖 16%，而闸门照样显示 | _privacy_terms.txt cash peak_equity |
| **P127** | 短 token 落在更长数值里 —— 子串判据又栽一次 | CTX_WORDS |
| **P128** | 财报是累计口径，不年化就等于全部算错 | RPT_LICO_FN_CPD None |
| **P129** | ulist 单只和批量，走的不是同一条数据路径 | PE_DYNAMIC _src |
| **P130** | 用工商企业的指标去衡量银行，每一条都是错读数 | notes flags |
| **P131** | ds 指标族返回类型不统一：同一行代码，昨天是序列、今天是标量 | ema kdj None |
| **P132** | require_all=True 不是"所有条件都满足"，多条件永远是 OR | require_all True |

## 怎么用

1. 先在本表**搜索症状词**（如「负值」「末根」「降级」「量纲」）；
2. 命中后 `grep -A 40 "^## P115" references/pitfalls.md` 只看那一节；
3. 新增条目后跑 `python scripts/make_index.py` 重新生成，**不要手动补表**。
