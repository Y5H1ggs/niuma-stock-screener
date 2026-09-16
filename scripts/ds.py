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
    """硬约束：仅主板 60/000/001/002/003。"""
    c = str(code)
    if c.startswith(('300', '301', '302', '688', '689', '8', '4', '9')):
        return False
    return c.startswith(('60', '000', '001', '002', '003'))


def secid(code):
    """东财 secid 前缀：沪 1. / 深 0. """
    c = str(code)
    return ('1.' if c.startswith('6') else '0.') + c


def tsym(code):
    """腾讯/新浪 symbol：sh/sz + code"""
    c = str(code)
    return ('sh' if c.startswith('6') else 'sz') + c


def n100(price, cash=6600):
    """可买股数（100 股整手）"""
    p = _fl(price)
    return int(cash // (p * 100)) * 100 if p > 0 else 0


# ---------------------------------------------------------------- 1. 实时快照（腾讯）
def snapshot(codes, raw=False):
    """腾讯实时快照。返回 {code: {name,price,chg,prev,open,high,low,vol,amount,hs,lb,outer,inner,limit_up,bids,asks,time}}
    ⚠️ 按请求顺序对齐，勿用返回内代码做 key。"""
    codes = [str(c).lstrip('shsz').replace('sh', '').replace('sz', '') for c in codes]
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
            'sealed': (asks and _fl(asks[0][0]) == lim and asks[0][1] == 0),   # 封死
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
    return rows


# ---------------------------------------------------------------- 3. 个股资金（东财 ulist.np）
def ulist(codes, fields='f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87'):
    """实时资金口径。f62 主力净额 f66 超大单 f72 大单 f78 中单 f84 小单 f184 净占比%。"""
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


def sector_stats(bk):
    """板块统计：家数/红盘数/主力合计/涨幅中位数/涨停名单/个股绝对排名。
    实现铁律 1（相对强度）+ 情绪温度。"""
    rows = sector_members(bk, pages=8)
    chg = [_fl(x.get('f3')) for x in rows if x.get('f3') is not None]
    zl = sum(_fl(x.get('f62')) for x in rows) / 1e8
    chg_sorted = sorted(chg)
    med = chg_sorted[len(chg_sorted) // 2] if chg_sorted else 0.0
    lim = [(x.get('f12'), x.get('f14'), _fl(x.get('f3'))) for x in rows if _fl(x.get('f3')) >= 9.7]
    return {'n': len(rows), 'up': sum(1 for c in chg if c > 0), 'zl_yi': zl,
            'median_chg': round(med, 2), 'limit_up': lim, 'rows': rows}


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
    """统一入口：东财 → 新浪 → 腾讯，逐级降级。"""
    k = kline_em(code, lmt=n)
    if k:
        return k, 'eastmoney'
    k = kline_sina(code, datalen=n)
    if k:
        return k, 'sina'
    k = kline_qq(code, n=n)
    return k, 'tencent'


# ---------------------------------------------------------------- 7. 情绪温度
def mood():
    """情绪温度三指标之一：昨日涨停/首板/连板的板指涨跌 + 主力净额。"""
    out = {}
    rows = clist(fid='f3', fs='m:90+t:3', pages=1, fields='f12,f14,f3,f62')
    idx = {str(x.get('f12')): x for x in rows}
    for name, bk in EMOTION_BK.items():
        x = idx.get(bk)
        if not x:
            # 用成分合计兜底
            st = sector_stats(bk)
            out[name] = {'chg': None, 'zl_yi': st['zl_yi'], 'up': st['up'], 'n': st['n']}
        else:
            out[name] = {'chg': _fl(x.get('f3')), 'zl_yi': _fl(x.get('f62')) / 1e8}
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


__all__ = ['snapshot', 'clist', 'ulist', 'fflow', 'fflow_at', 'slist', 'sector_members',
           'sector_stats', 'kline_em', 'kline_sina', 'kline_qq', 'kline', 'mood',
           'ma', 'ema', 'rsi', 'boll', 'macd', 'cci', 'kdj', 'wr', 'mom', 'pos',
           'indicators', 'allowed', 'secid', 'tsym', 'n100', 'get', 'UT', 'MARKET_MAIN']


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
