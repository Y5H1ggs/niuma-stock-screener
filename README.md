# 选股分析牛马工具

一个**自包含、零依赖**的 Agent Skill：把 A 股隔日超短（当日买 / 次日卖）的研究流程、
数据源接口、判读铁律与回测方法固化下来，可被任何支持 `SKILL.md` 规范的 Agent 软件加载。

> 名字来源：干活的牛马自己给自己写工具。**不装任何 pip 包**，纯标准库跑通全链路。

---

## 一、这个 Skill 包含什么

| 文件 | 内容 |
|---|---|
| `SKILL.md` | **主入口**：基础数据录入、硬约束、执行流程、三口径切换、竞价校准、输出格式 |
| `config.example.json` | **个人参数模板**（资金 / 交易权限 / 费用率 / 止损纪律）——复制为 `config.json` 后填写 |
| `references/datasources.md` | 全部数据源接口 URL + 字段索引 + 限流降级链 |
| `references/playbook.md` | **判读铁律**：板块资金接力、情绪温度、相对强度、托单消耗、封单强度、竞价、资金强度分级 |
| `references/backtest.md` | T+1 形态回测方法论（口径 / 必经输出 / 分档切分） |
| `references/pitfalls.md` | 21 条真实踩坑记录（编码 / 字段 / 累加错误 / 代码猜测 / 视觉重叠…） |
| `scripts/cfg.py` | **个人参数配置加载 + 首次录入闸门**（不含任何预设个人数据） |
| `scripts/ds.py` | **便携数据源库**（纯标准库，零依赖） |
| `scripts/scan_pool.py` | 全市场候选池扫描（强势 / 趋势 / 潜力 三种口径） |
| `scripts/sector_compare.py` | 板块内横向对比（铁律 1 + 相对强度，含属性桶过滤） |
| `scripts/mood.py` | 情绪温度三指标（昨日涨停系资金 + 真实连板梯队 + 主线错位） |
| `scripts/backtest.py` | T+1 形态回测（12 种内置形态 + 基准对照 + 极端案例明细） |
| `scripts/report.py` | **一键生成卡片式 HTML 深度分析报告**（顶部深色评级 hero + 独立评级理由卡 + 八节，每个小板块独立成卡） |
| `examples/report_template.md` | 五段式**文本**输出模板（用于对话内输出） |
| `examples/notes_sample.json` | `report.py --notes` 的人工研判注入样例 |

---

## 二、环境要求

- **Python 3.8+**，**仅用标准库**（`urllib` / `json` / `ssl` / `re` / `math` / `time`），**无需 pip 安装任何包**。
- 需要能访问：`qt.gtimg.cn`、`push2*.eastmoney.com`、`money.finance.sina.com.cn`、`web.ifzq.gtimg.cn`

---

## 三、如何加载到不同软件

### 1. WorkBuddy / Claude Code / Claude.ai（标准 Agent Skills）

把整个 `选股分析牛马工具/` 文件夹放到 skills 目录：

```bash
# 个人级（本机所有项目可用）
# macOS / Linux
~/.claude/skills/选股分析牛马工具/
# Windows
%USERPROFILE%\.claude\skills\选股分析牛马工具\

# WorkBuddy
~/.workbuddy/skills/选股分析牛马工具/
```

放好后，Agent 会读取 `SKILL.md` 的 frontmatter（`name` + `description`）自动识别何时调用。

### 2. Cursor

Cursor 用 `.cursor/rules/*.mdc`。做法：在项目里新建 `.cursor/rules/a-share-t1.mdc`，
把 `SKILL.md` 正文粘进去，frontmatter 改为：
```
---
description: A股 T+1 隔日接力选股
globs:
alwaysApply: true
---
```
脚本仍可 `python scripts/xxx.py` 直接跑。

### 3. 任意其他 Agent / 本地 LLM 工作流（通用做法）

该 Skill 就是**纯文本 + 纯标准库脚本**，任何能读文件、执行命令的环境都能用：

- 把 `SKILL.md` 作为 system prompt 的一部分（或作为首条上下文）注入；
- 需要数据时按 `references/datasources.md` 调接口，或直接 `python scripts/ds.py`；
- 需要选股时依次跑 `scan_pool.py` → `sector_compare.py` → `mood.py` → `backtest.py`。

### 4. 纯手工 / 不加载任何软件

`references/` 下的四份文档本身就是一份**可独立阅读的 A 股短线研究手册**——
接口、规则、坑点、回测方法都写全了，照着跑脚本即可。

---

## 四、快速上手

```bash
cd scripts

# 0) 【首次必做】基础数据录入：把 config.example.json 复制为 config.json，填上
#    你的资金 / 交易权限 / 费用率 / 止损纪律。填完先自检：
python cfg.py

# 0.5) 自检：确认数据源连通
python ds.py

# 1) 情绪温度（今天该不该做接力）
python mood.py

# 2) 候选池扫描（三选一）
python scan_pool.py strong      # 强势口径：涨停/准涨停
python scan_pool.py trend       # 趋势口径：排除涨停，走势稳定向上
python scan_pool.py potential   # 潜力口径：未涨停+量能+资金

# 3) 板块横向对比（铁律 1，必做）
python sector_compare.py 002093

# 4) 形态回测（该股历史同类形态次日胜率）
python backtest.py 002093

# 5) 一键出 HTML 深度分析报告（同款版式）
python report.py 002093 --title 深度分析
python report.py 002093 --title 午盘深度分析 --notes notes.json --sector-top 4
```

**关于 `report.py`**：数值、板块相对强度、超买超卖判定、情景概率全部**自动计算**，
不传 `notes.json` 也能出一份完整报告；传入 `notes.json` 可用人工叙述覆盖
`verdict / bull / bear / scenarios / watch / oneline` 几段定性内容
（样例见 `examples/notes_sample.json`）。

> 所有脚本的**资金规模 / 板块权限 / 价格上限**都来自 `config.json`，脚本里没有任何预设值。
> `report.py` 会调用 `backtest.py` 作为模块，两者须放在同一目录。

---

## 五、内置硬约束（可在 SKILL.md 顶部按自己情况修改）

1. **只做你权限内的板块**：由 `config.json` 的 `allowed_prefixes` 决定（默认沪深主板 60/000/001/002/003）
2. **仓位模式**：默认全仓单吊，100 股整手
3. **3 板及以上默认不接力**
4. **板块内资金接力偏好必查**
5. **禁止绝对化表述**：所有情景必须带概率，≥50% 标高概率
6. **不给买卖指令**：只做客观推演，末尾强制风险提示

---

## 六、免责声明

本项目仅供个人研究与学习使用。所有数据来自第三方公开接口，可能存在延迟、缺失或错误；
所有统计与推演**不构成任何投资建议**。据此操作，风险自担。
