# 数据源接口规格与限流降级链

> 全部接口实测可用（2026-09）。纯 `urllib` 即可，无需第三方库。
> 通用约定：`ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE`
> Header：`User-Agent: Mozilla/5.0`，东财接口额外带 `Referer: https://quote.eastmoney.com/`

---

## 0. 东财 ut token 与主机

- **当前可用 ut**：`fa5fd1943c7b386f172d6893dbfba10b`
- **旧 ut（已限流）**：`b2884a393a59ad64002292a3e90d46a5`
- ⚠️ **旧 ut 返回 `rc:102 / data:null` ≠ IP 被拉黑** → 换新 ut 即恢复（rc:0）。ut 失效就换 token，不要急着换 IP/主机。
- **主机切换顺序**：`push2.eastmoney.com` ↔ `push2delay.eastmoney.com` ↔ `push2his.eastmoney.com`
  - 板块**列表**接口（`m:90+t:3` 概念 / `m:90+t:2` 行业）在 `push2` 上可能返回空，`push2delay` 可用。
  - 板块**成分**接口（`fs=b:BKxxxx`）两个主机都可能可用，**需双向重试**。

---

## 1. 实时快照 —— 腾讯 qt.gtimg.cn

```
http://qt.gtimg.cn/q=sh601169,sz002093,sh000001     # 多只用逗号拼接
```
- **gbk 解码**；返回多段，用 `;` 分割，每段 `~` 分割。
- ⚠️ **按请求顺序对齐返回行**，不要用返回内的代码字段做 key 匹配。

**字段索引（`f = seg.split('~')`）**：

| 索引 | 含义 | 索引 | 含义 |
|---|---|---|---|
| f[1] | 名称 | f[30] | 时间戳 |
| f[2] | 代码 | f[31] | 涨跌额 |
| f[3] | **现价** | f[32] | **涨跌幅 %** |
| f[4] | **昨收** | f[33] | 最高 |
| f[5] | 今开 | f[34] | 最低 |
| f[6] | 成交量（手） | f[37] | 成交额（万元） |
| f[7] | **外盘**（主动买） | f[38] | 换手率 % |
| f[8] | **内盘**（主动卖） | f[49] | **量比** |
| f[9] | 买1价 | f[10] | 买1量（手） |
| f[19] | 卖1价 | f[20] | 卖1量（手） |
| 买2~买5 | f[11..18] | 卖2~卖5 | f[21..28] |

- 涨停判定：主板 `round(昨收*1.1, 2)`；**封死** = `卖1价 == 涨停价 且 卖1量 == 0`（封单量取 f[10]）。
- 竞价阶段内外盘即已有效（f[7]/f[8]）。

---

## 2. 全市场列表 —— 东财 clist

```
https://push2delay.eastmoney.com/api/qt/clist/get
  ?pn=1&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3
  &fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23
  &fields=f12,f14,f2,f3,f6,f8,f10,f20,f62,f100
  &ut=<UT>
```
- `fid=f3` 按涨跌幅降序；`fid=f62` 按主力净额降序。
- **字段**：f12 代码 · f14 名称 · f2 最新价 · f3 涨跌幅 · f6 成交额 · f8 换手率 · f10 量比 · f20 总市值 · **f62 主力净额** · f100 行业
- 分页：`pn=1,2,3...`，`pz` 最大 **100**（**设 2000 不生效，只返回 100 条**）。`data.total` 给总条数。
- 单只资金字段（替代接口，见 §3）：f66 超大单 · f72 大单 · f78 中单 · f84 小单 · f184 主力净占比

---

## 3. 个股实时资金 —— 东财 ulist.np

```
https://push2.eastmoney.com/api/qt/ulist.np/get
  ?secids=0.002093,1.601169
  &fields=f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87
  &ut=<UT>
```
- secid 前缀：沪市 `1.`、深市 `0.`
- f62 主力净额 · f66 超大单净额 · f72 大单净额 · f78 中单净额 · f84 小单净额 · f184 主力净占比(%)
- **用途**：作为 `fflow` 分钟级的交叉验证基准（两者差异 >20% 先怀疑单位/累加错误）。

---

## 4. 资金流时序 —— 东财 fflow

```
# 分钟级（当日累计）
https://push2.eastmoney.com/api/qt/stock/fflow/kline/get
  ?secid=0.002093&fields1=f1,f2,f3,f7
  &fields2=f51,f52,f53,f54,f55,f56&klt=1&lmt=0&ut=<UT>
# 日线级
... &klt=101&lmt=0
# 板块级（secid 用 90.BKxxxx）
... &secid=90.BK1650 &klt=1
```

- 返回 `data.klines`，每条形如 `"2026-09-16 10:22,24469264,..."`。
- ⚠️⚠️ **【致命坑】`klt=1` 的 f52 是"从开盘累计到该分钟的主力净额"，不是每分钟增量！**
  - **禁止** `for x in klines: cum += float(x.split(',')[1])` —— 会得出超过当日成交额的荒谬值。
  - **正确**：取时间戳最接近目标时点的那条，或用最后一条（= 当日累计）。
- 字段顺序（常用）：f51 时间 · **f52 主力净额(累计)** · f53 小单 · f54 中单 · f55 大单 · f56 超大单。（以实测为准，注意与 ulist.np 交叉验证）

---

## 5. 板块归属 —— 东财 slist（禁止猜 BK 代码）

```
https://push2delay.eastmoney.com/api/qt/slist/get
  ?spt=3&fltt=2&invt=2&fields=f12,f13,f14
  &secid=0.002093&ut=<UT>&pi=0&pz=60&po=1&np=1
```
- 返回该股所属的**全部**板块（行业 + 概念 + 地域），每项含 BK 代码。
- 实测：002093 → BK1650 通信技术 / BK0714 5G概念 / BK0554 物联网 / BK0800 人工智能 / BK0953 鸿蒙概念 / BK1104 信创 / BK1061 数字经济
- **已验证的正确 BK**：`BK1650` 通信技术 · `BK0714` 5G概念 · `BK0815` 昨日涨停 · `BK1630` 昨日首板 · `BK0816` 昨日连板 · `BK0475` 银行 · `BK0151` 福建板块 · `BK0424` 水泥 · `BK1464` 水泥制造 · `BK1208` 建筑材料
- ⚠️ **猜错代码会返回完全错误的成分表**（实测 BK0894 猜作"光通信模块"却返回医药股）。若返回成分与预期行业完全不符 → **立即放弃该代码并在输出中标注数据缺失**。

**板块成分 + 排名**：
```
https://push2delay.eastmoney.com/api/qt/clist/get
  ?fs=b:BK1650&fid=f3&fields=f12,f14,f2,f3,f62&pn=1&pz=100&ut=<UT>
```
- ⚠️ **必须分页取全量**：`pz=100` 只给涨幅前 100 名，目标票不在其中会"查不到"从而误报排名。
- 分页遍历（pn=1,2,3…）直至某页返回不足 100 条，再定位目标代码的**绝对排名**。

---

## 6. K 线三源

| 优先级 | 源 | 说明 |
|---|---|---|
| 1 | 东财 `push2his.eastmoney.com/api/qt/stock/kline/get?secid=X.XXXXXX&klt=101&fqt=1&lmt=260&ut=<UT>` | fields2=f51..f56 → 日期/开/收/高/低/量 |
| 2 | 新浪 `money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol=sz002093&scale=240&ma=no&datalen=80` | **末根是前一交易日**，判当日位置须把 live 价 append 进序列 |
| 3 | 同花顺 `d.10jqka.com.cn/v6/line/hs_XXXXXX/01/last.js`（jsonp） | 最后兜底 |

- ⚠️ **东财 push2his 盘中会限流，且三主机可能同时失效**（返回 `klines: []`，报错特征 `max() iterable argument is empty`）→ **直接切新浪，不要在原源上重试超过 3 次**。
- ⚠️ **新浪返回的是 JS 对象字面量（非严格 JSON）**：`day:` / `open:` 等键无引号。
  ```python
  import re
  txt = re.sub(r'([{,])(\w+):', r'\1"\2":', txt)
  arr = json.loads(txt)
  ```

---

## 7. 腾讯前复权日 K（回测首选，单次可取 800 根）

```
https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sz002093,day,,,800,qfq
```
- 路径：`data.<code>.qfqday`（部分情况下为 `.day`）
- 元素 = `[date, open, close, high, low, volume]`，**volume 单位：手**
- 稳定性好、无限流，**做历史回测优先用它**。

---

## 8. 新浪板块资金流（东财限流时兜底）

```
https://vip.stock.finance.sina.com.cn/q/view/newFLJK.php?param=class
```
- **gbk 编码**；用 `regex = \s*(\{.*\})\s*;?\s*$` 提取 JSON。
- 每条按 `,` 切：`p[1]` 名称 · `p[5]` 涨跌% · `p[6]` 主力净流入(元)
- 一个调用同时给流入与流出，按净额排序取两端。
- 弃用：`MoneyFlow.ssl_qsfx_zjlrqs`（盘前返回空数组）。

---

## 9. 不可用源（已实测，勿再尝试）

- 网易 `api.money.126.net`、百度 `finance.pangqiu.com`（代理 502）
- 雪球（需有效 cookie）
- 同花顺 `data.10jqka.com.cn/funds/ggzjl/`（HTML 可访问但需解析，仅作最后兜底）

---

## 10. 通用健壮性写法

```python
import urllib.request, json, ssl, time
ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE

def get(url, tries=3, enc='utf-8', ref='https://quote.eastmoney.com/'):
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                'Referer': ref})
            return urllib.request.urlopen(req, timeout=12, context=ctx).read().decode(enc, 'ignore')
        except Exception:
            time.sleep(0.8)
    return None
```

- 429 / RemoteDisconnected **不是** IP 被封 → 换 ut / 换主机 / 加 0.8~1.5s 间隔重试，通常 3 次内成功。
- 限流时应放缓到 **每次请求间隔 ≥0.5s**，批量任务加 0.3~0.5s 休眠。
