# -*- coding: utf-8 -*-
"""
ds.py — A股 T+1 选股便携数据源库（纯标准库，零依赖）

覆盖：实时快照 / 全市场列表 / 个股资金 / 资金流时序 / 板块归属 / 板块成分 /
      K线三源 / 回测用前复权日K / 情绪温度板块 / 技术指标计算。

用法：
    from ds import *
    snap = snapshot(['601169','600000'])
    rows = clist(fid='f3', fs=MARKET_MAIN, pages=8)
    k = kline_qq('600000', 800)
    ind = indicators([...closes...], live=7.77)

所有接口实测可用（2026-09）。限流降级链已内置。
"""
import urllib.request, json, ssl, time, math, re

# ---------------------------------------------------------------- 基础
import cfg                        # 个人参数（资金/权限/费用）全部外置到 config.json

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

UT = 'fa5fd1943c7b386f172d6893dbfba10b'          # 当前可用；失效时换 token
UT_FALLBACK = ['b2884a393a59ad64002292a3e90d46a5', 'fa5fd1943c7b386f172d6893dbfba10b']
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0'

EM_HOSTS = ['push2delay.eastmoney.com', 'push2.eastmoney.com', 'push2his.eastmoney.com']
MARKET_MAIN = 'm:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23'   # 全市场（含主板/中小/创业/科创）
MARKET_BOARD = 'm:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23'   # 同上；如需纯主板请在代码层过滤

EMOTION_BK = {'昨日涨停': 'BK0815', '昨日首板': 'BK1630', '昨日连板': 'BK0816'}


def get(url, tries=3, enc='utf-8', ref='https://quote.eastmoney.com/', timeout=12, gap=0.8):
    """带重试的 GET。429/断连不是封 IP → 换 token/主机 + 间隔重试。"""
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Referer': ref})
            return urllib.request.urlopen(req, timeout=timeout, context=CTX).read().decode(enc, 'ignore')
        except Exception as e:
            last = e
            time.sleep(gap)
    return None


def _fl(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def allowed(code):
    """是否落在 config.json 声明的可交易板块内（默认沪深主板 60/000/001/002/003）。
    注意：这不是"市场规则"，而是**每个使用者自己的交易权限**——所以放在 config 里。"""
    c = str(code)
    pre = cfg.get('allowed_prefixes') or []
    return any(c.startswith(str(p)) for p in pre)


def secid(code):
    """东财 secid 前缀：沪 1. / 深 0. """
    c = str(code)
    return ('1.' if c.startswith('6') else '0.') + c


def tsym(code):
    """腾讯/新浪 symbol：sh/sz + code。
    ⚠️ 已带 sh/sz/bj 前缀的原样返回（指数必须显式前缀：上证 sh000001，
       不带前缀的 000001 会被判成深市 = 平安银行，这是历史踩过的坑）。"""
    c = str(code)
    if c[:2] in ('sh', 'sz', 'bj'):
        return c
    return ('sh' if c.startswith('6') else 'sz') + c


def n100(price, cash=None):
    """可买股数（100 股整手）。cash 缺省时取 config.json 里的 cash（个人资金不在代码里）。"""
    if cash is None:
        cash = cfg.get('cash') or 0
    p = _fl(price)
    return int(cash // (p * 100)) * 100 if p > 0 else 0


# ---------------------------------------------------------------- 1. 实时快照（腾讯）
def snapshot(codes, raw=False):
    """腾讯实时快照。返回 {code: {name,price,chg,prev,open,high,low,vol,amount,hs,lb,outer,inner,limit_up,bids,asks,time,sealed}}
    ⚠️ 按请求顺序对齐，勿用返回内代码做 key。
    ⚠️ 显式 sh/sz/bj 前缀会保留（指数必须显式传 sh000001），无前缀的走自动判断。"""
    codes = [str(c) if str(c)[:2] in ('sh', 'sz', 'bj') else str(c).lstrip('shsz') for c in codes]
    url = 'http://qt.gtimg.cn/q=' + ','.join(tsym(c) for c in codes)
    txt = get(url, enc='gbk', ref='https://gu.qq.com/')
    out = {}
    if not txt:
        return out
    for i, seg in enumerate(txt.strip().split(';')):
        if '~' not in seg:
            continue
        f = seg.split('~')
        if len(f) < 50 or i >= len(codes):
            continue
        code = codes[i]
        prev = _fl(f[4]); px = _fl(f[3]); lim = round(prev * 1.1, 2)
        try:
            bids = [(f[9 + k * 2], int(_fl(f[10 + k * 2]))) for k in range(5)]
            asks = [(f[19 + k * 2], int(_fl(f[20 + k * 2]))) for k in range(5)]
        except Exception:
            bids, asks = [], []
        out[code] = {
            'code': code, 'name': f[1], 'price': px, 'chg': _fl(f[32]), 'prev': prev,
            'open': _fl(f[5]), 'high': _fl(f[33]), 'low': _fl(f[34]),
            'vol': int(_fl(f[6])), 'amount': _fl(f[37]), 'hs': _fl(f[38]), 'lb': _fl(f[49]),
            'outer': int(_fl(f[7])), 'inner': int(_fl(f[8])),
            'limit_up': lim, 'bids': bids, 'asks': asks, 'time': f[30],
            # 封死 = 现价达涨停 + 卖盘五档全空 + 买一有量
            # ⚠️ 原判据 (asks[0][0] == lim) 会漏判：封死时卖档价格字段返回 "0.00" 而非涨停价
            'sealed': (prev > 0 and px >= lim - 1e-6 and bool(asks)
                       and all(q == 0 for _, q in asks) and any(q > 0 for _, q in bids)),
        }
    return out


# ---------------------------------------------------------------- 2. 全市场列表（东财 clist）
def clist(fid='f3', fs=MARKET_MAIN, pages=8, pz=100, fields='f12,f14,f2,f3,f6,f8,f10,f20,f62,f100'):
    """全市场/板块成分列表。fid=f3 涨幅降序，fid=f62 主力净额降序。
    ⚠️ pz 最大 100（设 2000 不生效）。自动分页。"""
    rows = []
    for pn in range(1, pages + 1):
        url = (f'https://{EM_HOSTS[0]}/api/qt/clist/get?pn={pn}&pz={pz}&po=1&np=1&fltt=2&invt=2'
               f'&fid={fid}&fs={fs}&fields={fields}&ut={UT}')
        r = get(url)
        if not r:
            # 换主机重试一次
            r = get(url.replace(EM_HOSTS[0], EM_HOSTS[1]))
        if not r:
            break
        try:
            j = json.loads(r)
            diff = (j.get('data') or {}).get('diff') or []
            if not diff:
                break
            rows.extend(diff)
            total = (j.get('data') or {}).get('total', 0)
            if pn * pz >= total:
                break
        except Exception:
            break
        time.sleep(0.25)
    # ★ G8 去重（2026-09-21）：东财分页在**实时排序**（fid=f3/f62，po=1 按值降序）下页间会重叠
    #   —— 同一只票可能同时出现在 pn=2 与 pn=3（实测 600479/600933/002687/603020 各出现两次）。
    #   不去重会让候选池虚增、同一票重复占据注意力，还会让"筛出 N 只"这类统计失真。
    #   按代码保留首次出现顺序（列表本身有序，去重不得打乱排序语义）。
    seen, uniq = set(), []
    for x in rows:
        k = str(x.get('f12'))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(x)
    return uniq


# ---------------------------------------------------------------- 3. 个股资金（东财 ulist.np）
def ulist(codes, fields='f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87'):
    """实时资金口径，返回 {代码: 字段dict}。f62 主力净额 f66 超大单 f72 大单 f78 中单 f84 小单 f184 净占比%。

    ⚠️ `codes` 既可传**代码列表**，也可传**单个代码字符串**（P58）。
       曾只接受列表：传字符串时它把每个字符当成一个代码去拼 secids，
       接口返回空 data → 返回 `{}`，而调用方 `.get('f62') or 0` 只会看到 **0**，
       表现为"当日主力净额 = 0 万"这种**看起来完全正常的错误数据**
       （与 P55 同类：不报错、数字自洽、只是悄悄错）。字符串现在自动包成单元素列表。
    """
    if isinstance(codes, str):
        codes = [codes]
    secids = ','.join(secid(c) for c in codes)
    url = f'https://push2.eastmoney.com/api/qt/ulist.np/get?secids={secids}&fields={fields}&ut={UT}'
    r = get(url)
    if not r:
        return {}
    try:
        out = {}
        for x in (json.loads(r).get('data') or {}).get('diff') or []:
            out[str(x.get('f12'))] = x
        return out
    except Exception:
        return {}


# ---------------------------------------------------------------- 4. 资金流时序（东财 fflow）
def fflow(code, klt=1, lmt=0, is_sector=False):
    """⚠️⚠️ klt=1 的 f52 是【从开盘累计】的主力净额，不是每分钟增量！禁止累加。
    返回 [(时间, 主力净额累计, 小单, 中单, 大单, 超大单), ...]"""
    sec = ('90.' + code) if is_sector else secid(code)
    url = (f'https://push2.eastmoney.com/api/qt/stock/fflow/kline/get?secid={sec}'
           f'&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56&klt={klt}&lmt={lmt}&ut={UT}')
    r = get(url)
    if not r:
        return []
    try:
        out = []
        for x in (json.loads(r).get('data') or {}).get('klines') or []:
            p = x.split(',')
            out.append((p[0], _fl(p[1]), _fl(p[2]), _fl(p[3]), _fl(p[4]), _fl(p[5])))
        return out
    except Exception:
        return []


def fflow_at(code, hhmm, klt=1, is_sector=False):
    """取最接近某时点的累计主力净额（元）。"""
    ks = fflow(code, klt=klt, is_sector=is_sector)
    pick = None
    for t, zl, *_ in ks:
        ts = t[11:16]
        if ts >= hhmm:
            pick = zl
            break
    if pick is None and ks:
        pick = ks[-1][1]
    return pick


# ---------------------------------------------------------------- 5. 板块归属（东财 slist）
def slist(code, pz=60):
    """反查个股真实所属板块（行业+概念+地域）。返回 [{bk, name}]。
    ⚠️ 禁止猜 BK 代码；只能靠这个接口反查。"""
    url = (f'https://push2delay.eastmoney.com/api/qt/slist/get?spt=3&fltt=2&invt=2'
           f'&fields=f12,f13,f14&secid={secid(code)}&ut={UT}&pi=0&pz={pz}&po=1&np=1')
    r = get(url) or get(url.replace('push2delay', 'push2'))
    if not r:
        return []
    try:
        return [{'bk': x.get('f12'), 'name': x.get('f14')}
                for x in (json.loads(r).get('data') or {}).get('diff') or []]
    except Exception:
        return []


def sector_members(bk, fid='f3', pages=6):
    """板块成分（分页取全量，避免"只取前100名导致误报排名"）。"""
    return clist(fid=fid, fs=f'b:{bk}', pages=pages,
                 fields='f12,f14,f2,f3,f62,f100')


def stock_profile(code):
    """东财**客观分类**：行业名(f127) + 概念名列表(f129) + 地域(f128) + 板块名(f128)。

    ⚠️ 为什么要用它（2026-09-21 新增，P35）：
      此前板块基准靠「拉该股所属板块 → 按资金排序 → 挑最热 / 按成分数挑最细」，
      任何"挑"的动作都可能挑出对自己最有利的解释 —— 同一只票能拿到 +0.53pp 或 −1.18pp，
      全看算法挑了哪个板块。行业是交易所口径的分类，**每只票恰好一个、不参与挑选**，
      因此它才是可信的相对强度锚点。
      实证：标的乙 f127=电力（而其涨停故事在"碳化硅"题材里）；
            标的甲 f127=教育（主营 81% 是教育，此前却拿"通信技术"做对比 —— G9）。
    返回 {} 表示取数失败（调用方必须按"不可用"处理，不得伪造）。"""
    u = (f'https://{EM_HOSTS[0]}/api/qt/stock/get?secid={secid(code)}'
         f'&fields=f57,f58,f127,f128,f129&ut={UT}')
    r = get(u) or get(u.replace(EM_HOSTS[0], EM_HOSTS[1]))
    if not r:
        return {}
    try:
        d = json.loads(r).get('data') or {}
    except Exception:
        return {}
    if not d:
        return {}
    return {
        'code': str(d.get('f57') or code),
        'name': d.get('f58'),
        'industry': d.get('f127'),                       # 客观行业（每票恰一个）
        'area': d.get('f128'),
        'concepts': [c for c in str(d.get('f129') or '').split(',') if c.strip()],
    }


_BOARD_IDX = {}


def main_business(code, n=6):
    """主营构成（东财 F10，最新报告期）。用于 G9「板块对比前先核对主业」。

    返回 {'date':'2026-06-30', 'by_product':[(名称,占比%),…], 'by_industry':[…], 'by_region':[…]}；
    返回 {} 表示取数失败 —— 调用方必须按「不可用」处理，**不得据此臆断主业**。
    MAINOP_TYPE：1=按行业、2=按产品、3=按地区。**只有"按产品"能直接回答"它靠什么赚钱"**，
    行业/地区口径会被"华东 93%"这类信息稀释。
    为什么必须看它（G9 实证）：
      · 标的甲主营 教育 81.21% / 物联网 18.11%，却被拿"通信技术"当板块对比；
      · 标的乙主营 漆包线 64.87% / 光伏发电 18.16%，而涨停故事在"碳化硅"
        —— 碳化硅连主营前列都没进，涨的是**题材标签**，不是业绩。
    """
    mkt = 'SH' if str(code)[0] == '6' else ('BJ' if str(code)[0] in '48' else 'SZ')
    u = ('https://datacenter.eastmoney.com/securities/api/data/v1/get'
         '?reportName=RPT_F10_FN_MAINOP'
         '&columns=SECUCODE,REPORT_DATE,MAINOP_TYPE,ITEM_NAME,MAIN_BUSINESS_INCOME,MBI_RATIO'
         f'&filter=(SECUCODE%3D%22{code}.{mkt}%22)&pageNumber=1&pageSize=60'
         '&sortColumns=REPORT_DATE&sortTypes=-1&source=HSF10&client=PC')
    r = get(u, ref='https://emweb.securities.eastmoney.com/')
    if not r:
        return {}
    try:
        res = (json.loads(r).get('result') or {}).get('data') or []
    except Exception:
        return {}
    if not res:
        return {}
    d0 = str(res[0].get('REPORT_DATE') or '')[:10]
    keys = {'1': 'by_industry', '2': 'by_product', '3': 'by_region'}
    out = {'date': d0}
    for x in res:
        if str(x.get('REPORT_DATE') or '')[:10] != d0:
            continue
        k = keys.get(str(x.get('MAINOP_TYPE')))
        nm = str(x.get('ITEM_NAME') or '').strip()
        if not k or not nm or nm.startswith('其他(补充)'):
            continue
        ratio = (_fl(x.get('MBI_RATIO')) * 100) if _ok(x.get('MBI_RATIO')) else None
        out.setdefault(k, []).append((nm, round(ratio, 2) if ratio is not None else None))
    for k in ('by_product', 'by_industry', 'by_region'):
        if out.get(k):
            out[k] = sorted(out[k], key=lambda t: -(t[1] or 0))[:n]
    return out


def board_index(refresh=False):
    """全市场板块索引：行业(m:90+t:2) + 概念(m:90+t:3)，进程内缓存。

    返回 {'by_name': {名称: (bk, kind, zl亿, chg%)}, 'by_bk': {bk: (名称, kind, zl亿, chg%)}}
      kind ∈ {'行业','概念'}；zl/chg 可能为 None（盘前或该字段限流），
      **但名称与代码始终有效** —— 盘前也要能解析出板块，只是不参与资金打分。
    """
    if _BOARD_IDX and not refresh:
        return _BOARD_IDX
    by_name, by_bk = {}, {}
    meta = {'pages_ok': 0, 'pages_failed': 0, 'truncated': []}
    for fs, kind in (('m:90+t:2', '行业'), ('m:90+t:3', '概念')):
        seen = set()
        for pn in range(1, 9):
            diff, total = None, 0
            # 每页最多换主机重试 4 次 —— **失败页不能 break**：
            # 该索引按 fid=f62 降序取（排序随资金榜实时变化），若某页失败就 break，
            # 索引会被**静默截断**，表现为"同一板块名两次查询一次命中一次 None"，
            # 且失败页之后的所有板块整体消失。这与 P68 同族：跳过失败页 = 制造有偏样本。
            for attempt, host in enumerate((EM_HOSTS[0], EM_HOSTS[1], EM_HOSTS[0], EM_HOSTS[1])):
                url = (f'https://{host}/api/qt/clist/get?pn={pn}&pz=100&po=1&np=1'
                       f'&fltt=2&invt=2&fid=f62&fs={fs}&fields=f12,f14,f3,f62&ut={UT}')
                r = get(url)
                if r:
                    try:
                        dd = (json.loads(r).get('data') or {})
                        diff = dd.get('diff') or []
                        total = dd.get('total') or 0
                        break
                    except Exception:
                        diff = None
                time.sleep(0.4 * (attempt + 1))
            if diff is None or (not diff and pn * 100 - 100 < total):
                meta['pages_failed'] += 1
                meta['truncated'].append('%s p%d' % (kind, pn))
                continue
            if not diff:
                break
            meta['pages_ok'] += 1
            for x in diff:
                bk = str(x.get('f12'))
                if not bk.startswith('BK') or bk in seen:
                    continue
                seen.add(bk)
                nm = x.get('f14')
                zl = _fl(x.get('f62')) / 1e8 if _ok(x.get('f62')) else None
                chg = _fl(x.get('f3')) if _ok(x.get('f3')) else None
                rec = (bk, kind, zl, chg)
                by_bk[bk] = (nm, kind, zl, chg)
                by_name.setdefault(nm, rec)      # 同名以先出现者为准（行业优先于概念）
            if pn * 100 >= total:
                break
            time.sleep(0.2)
    _BOARD_IDX.clear()
    _BOARD_IDX.update({'by_name': by_name, 'by_bk': by_bk, 'meta': meta})
    return _BOARD_IDX


def _ok(v):
    """字段是否为有效数值。
    ⚠️ 东财 clist 在盘前（09:15 前）与限流时会把 f2/f3/f62 全返回 "-" 占位符，
       而 _fl("-") == 0.0 —— 不校验就会被静默伪造成"全市场平盘、板块普跌 0%"，
       进而让评分把"取数失败"当成"利空"扣分（这是最危险的一类 bug）。"""
    if v is None:
        return False
    s = str(v).strip()
    return s != '' and s != '-' and s.lower() != 'nan'


def _snap_chg(codes, batch=60):
    """腾讯批量快照取行情（东财 clist 行情字段失效时的降级源）。
    返回 {code: {'chg':涨跌幅, 'price':现价}}；仅处理沪/深主板与创业板代码。
    ⚠️ 盘前（09:15 前）腾讯/新浪只返回"今日未开盘"空壳：开=0、量=0、涨幅恒为 0。
       这类快照无信息量，必须剔除，否则会把整块板块算成"全员 0.00%、红盘率 0%"。
    """
    out = {}
    cl = [str(c) for c in codes if str(c)[:1] in '036']
    for i in range(0, len(cl), batch):
        try:
            sp = snapshot(cl[i:i + batch])
        except Exception:
            continue
        for k, v in sp.items():
            if _fl(v.get('vol')) <= 0:
                continue          # 盘前空壳 / 停牌：无昨收涨跌幅信息
            if v.get('chg') is not None:
                out[str(k)] = {'chg': v['chg'], 'price': v['price']}
    return out


def sector_stats(bk, fallback=True, pre=False):
    """板块统计：家数/红盘数/主力合计/涨幅中位数/涨停名单/个股绝对排名。
    实现铁律 1（相对强度）+ 情绪温度。
    ⚠️ 东财 clist 盘前/限流时 f3 返回 "-"，此时自动降级用腾讯快照补行情
       （成分代码名单仍来自东财，这部分一直是有效的）。
    ⚠️ pre=True（本报告已回退到"上一交易日收盘"口径）时**强制禁用所有实时字段**：
       否则会出现"个股用昨收、板块用今日竞价"的**口径混用**，相对强度直接算错。
       返回 ok=False 表示本板块统计不可用，调用方必须按"中性、不加减分"处理。"""
    if pre:
        # 盘前/竞价口径下，唯一能给的只有"上一交易日各成分涨跌幅"，而那需要逐只拉 K 线
        # （全量成分 × 多板块 = 数百次请求，不现实）。因此如实返回不可用，绝不伪造 0。
        return {'n': 0, 'up': 0, 'zl_yi': None, 'median_chg': None, 'limit_up': [],
                'rows': [], 'ok': False, 'src': 'pre', 'n_valid': 0}

    rows = sector_members(bk, pages=8)
    if not rows:
        return {'n': 0, 'up': 0, 'zl_yi': None, 'median_chg': None, 'limit_up': [],
                'rows': [], 'ok': False, 'src': 'none', 'n_valid': 0}

    valid = [] if pre else [x for x in rows if _ok(x.get('f3'))]
    src = 'em'
    chg_by = {}
    if valid and len(valid) >= len(rows) * 0.5:
        chg_by = {str(x.get('f12')): _fl(x.get('f3')) for x in valid}
    elif fallback:
        snaps = _snap_chg([x.get('f12') for x in rows])
        chg_by = {k: v['chg'] for k, v in snaps.items()}
        src = 'qq'

    if not chg_by:
        return {'n': len(rows), 'up': 0, 'zl_yi': None, 'median_chg': None,
                'limit_up': [], 'rows': rows, 'ok': False, 'src': 'fail', 'n_valid': 0}

    chg = list(chg_by.values())
    zl_valid = [_fl(x.get('f62')) for x in rows if _ok(x.get('f62'))]
    zl = (sum(zl_valid) / 1e8) if zl_valid else None
    chg_sorted = sorted(chg)
    med = chg_sorted[len(chg_sorted) // 2]
    lim = [(x.get('f12'), x.get('f14'), chg_by.get(str(x.get('f12'))))
           for x in rows if (chg_by.get(str(x.get('f12'))) or 0) >= 9.7]
    return {'n': len(rows), 'up': sum(1 for c in chg if c > 0), 'zl_yi': zl,
            'median_chg': round(med, 2), 'limit_up': lim, 'rows': rows,
            'ok': True, 'src': src, 'n_valid': len(chg_by), 'chg_by': chg_by}


# ---------------------------------------------------------------- 6. K线三源
def kline_em(code, lmt=260, klt=101, fqt=1):
    """东财 K 线。返回 [{d,o,c,h,l,v}]。⚠️ 盘中三主机可能同时限流(klines=[])→直接切 sina。"""
    url = (f'https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid(code)}'
           f'&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56&klt={klt}&fqt={fqt}'
           f'&end=20500101&lmt={lmt}&ut={UT}')
    r = get(url, tries=2)
    if not r:
        return []
    try:
        ks = (json.loads(r).get('data') or {}).get('klines') or []
        if not ks:
            return []
        return [{'d': p[0], 'o': _fl(p[1]), 'c': _fl(p[2]), 'h': _fl(p[3]),
                 'l': _fl(p[4]), 'v': _fl(p[5])} for p in (x.split(',') for x in ks)]
    except Exception:
        return []


def kline_sina(code, datalen=80):
    """新浪日K。⚠️ 末根是前一交易日，判当日位置需 append live 价。
    ⚠️ 新浪 volume 单位是【股】，已 /100 归一到【手】以对齐腾讯口径。返回 [{d,o,c,h,l,v}]。"""
    url = (f'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/'
           f'CN_MarketData.getKLineData?symbol={tsym(code)}&scale=240&ma=no&datalen={datalen}')
    r = get(url, ref='https://finance.sina.com.cn/')
    if not r:
        return []
    try:
        txt = re.sub(r'([{,])(\w+):', r'\1"\2":', r)      # JS 对象字面量 → JSON
        arr = json.loads(txt)
        return [{'d': x['day'], 'o': _fl(x['open']), 'c': _fl(x['close']),
                 'h': _fl(x['high']), 'l': _fl(x['low']), 'v': _fl(x.get('volume')) / 100}
                for x in arr]
    except Exception:
        return []


def kline_qq(code, n=800):
    """腾讯日K（回测首选，稳定）。返回 [{d,o,c,h,l,v}]，v 单位手（已对齐）。

    降级链（2026-09-16 实测）：
      1) fqkline/get?...,qfq   前复权  —— 实测可能返回 HTTP 501
      2) fqkline/get?...,hfq   后复权
      3) kline/kline?...,day   不复权  —— 实测 501 期间仍可用（800 根）
      4) 新浪 getKLineData               —— 兜底
    ⚠️ 走 3)/4) 时是不复权价，若区间内有分红送转，涨停价判定会有误差。
    """
    sym = tsym(code)
    for url in (
        f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{n},qfq',
        f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{n},hfq',
        f'https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param={sym},day,,,{n}',
    ):
        r = get(url, ref='https://gu.qq.com/')
        if not r:
            continue
        try:
            data = json.loads(r).get('data', {}).get(sym, {})
            arr = data.get('qfqday') or data.get('hfqday') or data.get('day') or []
            if arr:
                return [{'d': x[0], 'o': _fl(x[1]), 'c': _fl(x[2]), 'h': _fl(x[3]),
                         'l': _fl(x[4]), 'v': _fl(x[5])} for x in arr if len(x) >= 6]
        except Exception:
            continue
    return kline_sina(code, datalen=min(n, 1023))


def kline(code, n=260):
    """统一入口：东财 → 新浪 → 腾讯，逐级降级。
    返回 **二元组** `([{d,o,c,h,l,v}], 'eastmoney'|'sina'|'tencent')` —— 注意不是列表。

    ⚠️ 本接口是本项目**最容易误用**的一个（P55），因为用错时**不报错**：
       `charts.candlestick_svg(ds.kline(c))` 拿到的是 2 元素元组，长度检查通过，
       遍历时元素是 'eastmoney' 字符串 → 最终**静默渲染出一张空白图**；
       喂给 `indicators()` 则报 `string indices must be integers`（那个报错也看不出真因）。
       选哪个：
         · 只要 K 线数组 → `kline_list(code, n)`
         · 同时要数据源名 → `k, src = kline_src(code, n)`
    """
    k = kline_em(code, lmt=n)
    if k:
        return k, 'eastmoney'
    k = kline_sina(code, datalen=n)
    if k:
        return k, 'sina'
    return kline_qq(code, n=n), 'tencent'


# 语义别名：名字里点明"返回值带数据源名"，避免调用方以为它只给数组
kline_src = kline


def kline_list(code, n=260):
    """统一入口，**只返回 K 线数组** [{d,o,c,h,l,v}]（丢掉源名）。

    等价于 `kline(code, n)[0]`，但返回值类型不可能被误当成元组。
    画图 / 算指标一律用这个。
    """
    return kline(code, n)[0]


# ---------------------------------------------------------------- 7. 情绪温度
def mood(pre=False):
    """情绪温度三指标之一：昨日涨停/首板/连板的板指涨跌 + 主力净额。
    ⚠️ 东财 clist 盘前/限流时 f3/f62 返回 "-"，此时 zl_yi 必须置 None（而非 0），
       否则调用方会把"取数失败"误判成"资金净流入 → 接力环境健康"（把失败当利好，最危险的错法）。
    ⚠️ pre=True 时禁用实时字段（口径须与个股一致：都用上一交易日收盘）。"""
    out = {}
    rows = clist(fid='f3', fs='m:90+t:3', pages=1, fields='f12,f14,f3,f62')
    idx = {str(x.get('f12')): x for x in rows}
    for name, bk in EMOTION_BK.items():
        x = idx.get(bk)
        if x and _ok(x.get('f62')) and not pre:
            out[name] = {'chg': _fl(x.get('f3')) if _ok(x.get('f3')) else None,
                         'zl_yi': _fl(x.get('f62')) / 1e8, 'up': None, 'n': None}
        else:
            # 用成分合计兜底（成分名单仍来自东财，这部分有效）
            try:
                st = sector_stats(bk, pre=pre)
            except Exception:
                st = {'ok': False, 'zl_yi': None, 'up': 0, 'n': 0}
            out[name] = {'chg': None,
                         'zl_yi': st.get('zl_yi') if st.get('ok') else None,
                         'up': st.get('up'), 'n': st.get('n')}
    return out


# ---------------------------------------------------------------- 8. 技术指标
def ma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else float('nan')


def ema(vals, n):
    k = 2 / (n + 1); e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def rsi(vals, n=14):
    """RSI = 100*ag/(ag+al)，勿写补数。"""
    if len(vals) < n + 1:
        return float('nan')
    ag = al = 0.0
    for i in range(-n, 0):
        d = vals[i] - vals[i - 1]
        ag += max(d, 0.0); al += max(-d, 0.0)
    ag /= n; al /= n
    return 100 * ag / (ag + al) if (ag + al) > 0 else 50.0


def boll(vals, n=20, k=2):
    m = ma(vals, n)
    sd = math.sqrt(sum((v - m) ** 2 for v in vals[-n:]) / n) if len(vals) >= n else float('nan')
    return {'mid': m, 'up': m + k * sd, 'low': m - k * sd,
            'pctb': (vals[-1] - (m - k * sd)) / (2 * k * sd) * 100 if sd else float('nan')}


def macd(vals, f=12, s=26, sig=9):
    def ema_series(v, n):
        k = 2 / (n + 1); out = [v[0]]
        for x in v[1:]:
            out.append(x * k + out[-1] * (1 - k))
        return out
    ef, es = ema_series(vals, f), ema_series(vals, s)
    dif = [a - b for a, b in zip(ef, es)]
    dea = ema_series(dif, sig)
    return {'dif': dif[-1], 'dea': dea[-1], 'macd': 2 * (dif[-1] - dea[-1])}


def cci(h, l, c, n=20):
    if len(c) < n:
        return float('nan')
    tp = [(h[i] + l[i] + c[i]) / 3 for i in range(-n, 0)]
    m = sum(tp) / n
    md = sum(abs(x - m) for x in tp) / n
    return (tp[-1] - m) / (0.015 * md) if md else 0.0


def kdj(h, l, c, n=9, m1=3, m2=3):
    """标准 KDJ：K=2/3*K_prev+1/3*RSV, D=2/3*D_prev+1/3*K, J=3K-2D。"""
    if len(c) < n:
        return (float('nan'),) * 3
    K = D = 50.0
    for i in range(n, len(c) + 1):
        hh, ll = max(h[i - n:i]), min(l[i - n:i])
        rsv = (c[i - 1] - ll) / (hh - ll) * 100 if hh != ll else 50.0
        K = (m1 - 1) / m1 * K + 1 / m1 * rsv
        D = (m2 - 1) / m2 * D + 1 / m2 * K
    return K, D, 3 * K - 2 * D


def wr(h, l, c, n=14):
    if len(c) < n:
        return float('nan')
    hh, ll = max(h[-n:]), min(l[-n:])
    return (hh - c[-1]) / (hh - ll) * -100 if hh != ll else -50.0


def mom(c, n=10):
    return (c[-1] / c[-1 - n] - 1) * 100 if len(c) > n else float('nan')


def pos(c_highs, c_lows, live, n=60):
    w_h, w_l = max(c_highs[-n:]), min(c_lows[-n:])
    return (live - w_l) / (w_h - w_l) * 100 if w_h != w_l else 50.0


def indicators(closes, highs=None, lows=None, live=None):
    """一次算全套。live 传当日实时价（拼入序列尾部）。"""
    c = list(closes)
    h = list(highs) if highs else c
    l = list(lows) if lows else c
    if live is not None:
        c.append(live); h.append(live); l.append(live)
    return {
        'ma5': ma(c, 5), 'ma10': ma(c, 10), 'ma20': ma(c, 20), 'ma60': ma(c, 60),
        'bull': (ma(c, 5) > ma(c, 10) > ma(c, 20) > ma(c, 60)) if len(c) >= 60 else None,
        'boll': boll(c), 'macd': macd(c),
        'rsi6': rsi(c, 6), 'rsi14': rsi(c, 14), 'rsi24': rsi(c, 24),
        'cci20': cci(h, l, c, 20), 'wr14': wr(h, l, c, 14), 'mom10': mom(c, 10),
        'pos60': pos(h, l, c[-1], 60), 'pos250': pos(h, l, c[-1], 250),
    }


# ---------------------------------------------------------------- 9. 财务时点数据（PIT）
def financials(code, page_size=40):
    """个股历史业绩报表（含**实际公告日**）——PIT 时点回测的基础。

    返回 list[dict]，按报告期倒序：
      {'report':'2026-06-30', 'notice':'2026-08-20', 'eps':0.075, 'revenue':1.42e9,
       'netprofit':..., 'roe':5.21, 'rev_yoy':..., 'np_yoy':..., 'bps':...,
       'ocf_ps':..., 'gross_margin':...}

    ⚠️ 为什么必须用 NOTICE_DATE 而不是 REPORTDATE（look-ahead bias）：
       2026 中报的"报告期"是 2026-06-30，但**实际公告日是 2026-08-20**。
       若回测在 07-15 用这份财报做决策，用的就是当时根本不存在的未来信息 ——
       这是任何带财务因子的回测高估收益的**第一大来源**。
    """
    url = ('https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_LICO_FN_CPD'
           '&columns=ALL&filter=(SECURITY_CODE=%22' + code + '%22)'
           '&sortColumns=REPORTDATE&sortTypes=-1&pageSize=' + str(page_size) + '&pageNumber=1'
           '&source=WEB&client=WEB')
    r = get(url, ref='https://data.eastmoney.com/')
    if not r:
        return []
    try:
        rows = (json.loads(r).get('result') or {}).get('data') or []
    except Exception:
        return []
    out = []
    for x in rows:
        rd, nd = x.get('REPORTDATE'), x.get('NOTICE_DATE')
        if not rd:
            continue
        out.append({
            'report': str(rd)[:10],
            'notice': str(nd)[:10] if nd else None,
            'eps': _fl(x.get('BASIC_EPS')),
            'revenue': _fl(x.get('TOTAL_OPERATE_INCOME')),
            'netprofit': _fl(x.get('PARENT_NETPROFIT')),
            'roe': _fl(x.get('WEIGHTAVG_ROE')),
            'rev_yoy': _fl(x.get('YSTZ')),
            'np_yoy': _fl(x.get('SJLTZ')),
            'bps': _fl(x.get('BPS')),
            'ocf_ps': _fl(x.get('MGJYXJJE')),
            'gross_margin': _fl(x.get('XSMLL')),
        })
    return out


def fundamentals_at(code, date, fin=None):
    """PIT 取值：返回 date（'YYYY-MM-DD'）当天**已公告**的最新一期财报。

    规则：notice ≤ date 的报告期中 report 最大者。
    公告日缺失时按「报告期 + 45 天」保守估计 —— **宁可晚用，绝不早用**（保守方向才安全）。
    """
    fin = fin if fin is not None else financials(code)
    if not fin:
        return None
    cands = []
    for f in fin:
        nd = f['notice']
        if not nd:
            try:
                import datetime as _dt
                nd = (_dt.date.fromisoformat(f['report']) + _dt.timedelta(days=45)).isoformat()
            except Exception:
                continue
        if nd <= date:
            cands.append(f)
    return max(cands, key=lambda f: f['report']) if cands else None


def announce_calendar(code, fin=None):
    """公告日历 [(公告日, 报告期)] —— 人工核对"某日当时能看到什么"用。"""
    fin = fin if fin is not None else financials(code)
    return sorted([(f['notice'], f['report']) for f in fin if f['notice']])


__all__ = ['snapshot', 'clist', 'ulist', 'fflow', 'fflow_at', 'slist', 'sector_members',
           'sector_stats', 'kline_em', 'kline_sina', 'kline_qq', 'kline', 'kline_src', 'kline_list', 'mood',
           'ma', 'ema', 'rsi', 'boll', 'macd', 'cci', 'kdj', 'wr', 'mom', 'pos',
           'indicators', 'allowed', 'secid', 'tsym', 'n100', 'get', 'UT', 'MARKET_MAIN',
           'financials', 'fundamentals_at', 'announce_calendar']


if __name__ == '__main__':
    # 自检
    s = snapshot(['601169', '600000'])
    for c, v in s.items():
        print(f"{c} {v['name']} {v['price']} {v['chg']:+.2f}% 量比{v['lb']} "
              f"内{v['inner']}/外{v['outer']} 封死={v['sealed']}")
    print('slist(600000):', [x['name'] for x in slist('600000')][:8])
    k, src = kline('600000', 60)
    print(f'kline src={src} n={len(k)} 末根={k[-1] if k else None}')
    print('mood:', mood())
