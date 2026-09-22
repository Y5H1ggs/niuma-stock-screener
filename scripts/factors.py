# -*- coding: utf-8 -*-
"""factors.py — 量化因子挖掘（因子有效性检验）

不是"再写一批形态"，而是**用横截面统计检验一个因子到底有没有信息量**。

方法（标准因子研究框架）：

  ① 横截面配对  每个交易日，对股票池算出各因子值，与**未来 N 日收益**配对；
  ② IC 序列     逐日算 Spearman 秩相关（因子值 vs 未来收益）→ 得到 IC 时间序列；
  ③ ICIR        = mean(IC) / std(IC)。**这是判断因子是否可用的核心指标**：
                  |IC| 均值 >0.03 且 ICIR >0.3 才算有边际信息；ICIR >0.5 算优秀；
  ④ IC 胜率     IC 与理论方向一致的天数占比（稳定性，比均值更抗极值）；
  ⑤ 分组单调性  按因子分 5 组，看各组平均未来收益是否**单调**。
                  只挑"最好的一组"＝挑樱桃，必须看整条曲线；
  ⑥ 因子相关性  两两相关矩阵，剔除同源重复（如 mom5 与 mom10 常常高度相关）。

三条纪律（写进代码，不靠自觉）：

  · **财务因子必须走 PIT**：用 `ds.fundamentals_at(code, 日期)` 取"那天能看到"的财报，
    否则 IC 是假的——前视偏差会让一个垃圾因子看起来神准；
  · **必须报样本量与显著性**：|IC| 的 t 值 = ICIR × √天数。t < 2 视为噪声，
    哪怕 IC 均值看起来很漂亮；
  · **分组要看单调性**，不只看首尾差。

用法：
  python factors.py --universe 40 --days 150        # 挖掘（默认）
  python factors.py --list                          # 列出因子库
  python factors.py 601169 --single                 # 单票因子时序（不算 IC）
  python factors.py --universe 60 --horizon 5      # 自定义样本池与持有期
"""
import sys
import io
import os
import math
import time
import random

import ds
import cfg

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


# ------------------------------------------------------------------ 因子库
# 每个因子：名称 / 类别 / 取值函数(f=当日特征) / 理论方向(+1 表示值越大越好，-1 反之)
FACTORS = [
    # ---- 价格动量与反转
    ('mom5',      '动量',   lambda f: f['mom5'],      +1),
    ('mom10',     '动量',   lambda f: f['mom10'],     +1),
    ('mom20',     '动量',   lambda f: f['mom20'],     +1),
    ('rev1',      '反转',   lambda f: -f['chg'],      +1),   # 昨日跌得多 → 期望反弹
    ('mom5_rev',  '反转',   lambda f: -f['mom5'],     +1),
    # ---- 均线位置
    ('devma5',    '均线',   lambda f: f['devma5'],    -1),   # 偏离越大越回归
    ('devma20',   '均线',   lambda f: f['devma20'],   -1),
    ('above20',   '均线',   lambda f: 1.0 if f['above20'] else 0.0, +1),
    ('bull',      '均线',   lambda f: 1.0 if f['bull'] else 0.0,    +1),
    # ---- 摆动指标
    ('rsi14',     '摆动',   lambda f: f['rsi14'],     -1),
    ('cci20',     '摆动',   lambda f: f['cci20'],     -1),
    ('wr14',      '摆动',   lambda f: f['wr14'],      +1),
    ('pctb',      '摆动',   lambda f: f['pctb'],      -1),
    # ---- 位置
    ('pos60',     '位置',   lambda f: f['pos60'],     -1),
    ('pos250',    '位置',   lambda f: f['pos250'],    -1),
    ('dist_hi250', '位置',  lambda f: f['dist_hi250'], +1),  # 距 250 日高越远（负得越多）越可能反抽
    # ---- 量能
    ('vr',        '量能',   lambda f: f['vr'],        +1),
    ('vr5',       '量能',   lambda f: f['vr5'],       +1),
    ('amt_log',   '量能',   lambda f: math.log(f['amt']) if f['amt'] > 0 else None, +1),
    ('turnover',  '量能',   lambda f: f['hs'],        -1),
    # ---- K线形态
    ('upper',     '形态',   lambda f: f['upper'],     -1),   # 上影长 → 抛压
    ('lower',     '形态',   lambda f: f['lower'],     +1),
    ('gap',       '形态',   lambda f: f['gap'],       -1),
    ('yang',      '形态',   lambda f: 1.0 if f['yang'] else 0.0, +1),
    ('body',      '形态',   lambda f: f['body'],      +1),
    ('streak',    '涨停',   lambda f: f['streak'],    +1),
    # ---- 波动
    ('atr14',     '波动',   lambda f: f['atr14'],     -1),
    ('vol20',     '波动',   lambda f: f['vol20'],     -1),   # 收益波动率
    # ---- 财务（PIT）
    ('roe',       '财务',   lambda f: f['roe'],       +1),
    ('rev_yoy',   '财务',   lambda f: f['rev_yoy'],   +1),
    ('np_yoy',    '财务',   lambda f: f['np_yoy'],    +1),
    ('gross_margin', '财务', lambda f: f['gm'],       +1),
    ('ocf_ps',    '财务',   lambda f: f['ocf_ps'],    +1),
]


# ------------------------------------------------------------------ 统计工具
def _rank(v):
    order = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def spearman(x, y):
    n = len(x)
    if n < 5:
        return None
    rx, ry = _rank(x), _rank(y)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return num / (dx * dy) if dx > 0 and dy > 0 else None


def mean(v):
    return sum(v) / len(v) if v else 0.0


def std(v):
    if len(v) < 2:
        return 0.0
    m = mean(v)
    return math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))


# ------------------------------------------------------------------ 单日因子快照
def daily_factors(k, i, fin, last_fin=None):
    """第 i 根 K 线收盘时的因子值。财务因子走 PIT（只看到当时已公告的）。"""
    if i < 60:
        return None, last_fin
    c = k[i]['c']
    o = k[i]['o']
    h = k[i]['h']
    l = k[i]['l']
    prev = k[i - 1]['c']
    cl = [k[j]['c'] for j in range(i + 1)]
    hi = [k[j]['h'] for j in range(i + 1)]
    lo = [k[j]['l'] for j in range(i + 1)]
    ma5, ma10, ma20, ma60 = ds.ma(cl, 5), ds.ma(cl, 10), ds.ma(cl, 20), ds.ma(cl, 60)
    b = ds.boll(cl)
    vols = [k[j]['v'] for j in range(max(0, i - 20), i)]
    avgv = mean(vols) if vols else 0

    # 涨停 streak
    st = 0
    for j in range(i, 0, -1):
        if k[j - 1]['c'] and k[j]['c'] / k[j - 1]['c'] - 1 >= 0.095:
            st += 1
        else:
            break
    # ATR / 波动率
    trs = []
    for j in range(max(1, i - 13), i + 1):
        trs.append(max(k[j]['h'] - k[j]['l'],
                       abs(k[j]['h'] - k[j - 1]['c']),
                       abs(k[j]['l'] - k[j - 1]['c'])))
    atr14 = (mean(trs) / c * 100) if c else 0
    rets = [(k[j]['c'] / k[j - 1]['c'] - 1) for j in range(max(1, i - 19), i + 1)]
    vol20 = std(rets) * 100

    f = {
        'chg': (c / prev - 1) * 100 if prev else 0,
        'mom5': (c / cl[-6] - 1) * 100 if len(cl) > 6 else 0,
        'mom10': (c / cl[-11] - 1) * 100 if len(cl) > 11 else 0,
        'mom20': (c / cl[-21] - 1) * 100 if len(cl) > 21 else 0,
        'devma5': (c / ma5 - 1) * 100 if ma5 else 0,
        'devma20': (c / ma20 - 1) * 100 if ma20 else 0,
        'above20': c > ma20, 'bull': ma5 > ma10 > ma20 > ma60,
        'rsi14': ds.rsi(cl, 14), 'cci20': ds.cci(hi, lo, cl, 20),
        'wr14': ds.wr(hi, lo, cl, 14), 'pctb': b['pctb'],
        'pos60': ds.pos(hi, lo, c, 60), 'pos250': ds.pos(hi, lo, c, 250),
        'dist_hi250': (c / max(hi[-250:]) - 1) * 100,
        'vr': (k[i]['v'] / avgv) if avgv else 1.0,
        'vr5': (k[i]['v'] / mean([k[j]['v'] for j in range(max(0, i - 5), i)])) if i >= 6 else 1.0,
        'amt': 0.0, 'hs': 0.0,
        'upper': (h - max(o, c)) / c * 100 if c else 0,
        'lower': (min(o, c) - l) / c * 100 if c else 0,
        'gap': (o / prev - 1) * 100 if prev else 0,
        'yang': c > o, 'body': (c - o) / o * 100 if o else 0,
        'streak': st, 'atr14': atr14, 'vol20': vol20,
    }
    # ---- 财务 PIT（只在"可见财报发生变化"时更新，避免每日重复比较）
    d = k[i]['d']
    nf = _fin_at(fin, d)
    if last_fin is None or last_fin[1] is not nf:
        last_fin = (d, nf)
    cur = nf
    f['roe'] = (cur or {}).get('roe') or None
    f['rev_yoy'] = (cur or {}).get('rev_yoy') or None
    f['np_yoy'] = (cur or {}).get('np_yoy') or None
    f['gm'] = (cur or {}).get('gross_margin') or None
    f['ocf_ps'] = (cur or {}).get('ocf_ps') or None
    return f, last_fin


_FIN_CACHE = {}


def _fin_at(fin, date):
    """PIT 取值（带缓存）。返回 date 当天可见的最新一期财报。"""
    if not fin:
        return None
    key = (id(fin), date)
    if key in _FIN_CACHE:
        return _FIN_CACHE[key]
    cands = [x for x in fin if (x['notice'] or '9999') <= date]
    r = max(cands, key=lambda x: x['report']) if cands else None
    if len(_FIN_CACHE) > 5000:
        _FIN_CACHE.clear()
    _FIN_CACHE[key] = r
    return r


# ------------------------------------------------------------------ 主流程
def build_panel(universe=40, days=150, horizon=5, verbose=True):
    """构建因子面板：[(date, code, {因子名:值}, 未来收益)]"""
    if verbose:
        print('  构建股票池（主板 / 成交额靠前 / 剔除 ST）…')
    rows = ds.clist(fid='f6', fs=ds.MARKET_MAIN, pages=4, fields='f12,f14,f2,f3,f6')
    cand = []
    for x in rows:
        c, nm = str(x.get('f12', '')), str(x.get('f14', ''))
        if not ds.allowed(c) or 'ST' in nm.upper() or '退' in nm:
            continue
        if not ds._ok(x.get('f6')):
            continue
        cand.append((c, nm))
    random.seed(20260922)          # 固定种子 → 结果可复现（因子研究必须可复现）
    random.shuffle(cand)
    cand = cand[:universe]

    panel = []
    for n, (code, nm) in enumerate(cand):
        try:
            k = ds.kline_qq(code, 400)
            fin = ds.financials(code)
            if not k or len(k) < 120:
                continue
            idx_from = max(60, len(k) - days - horizon - 1)
            last_fin = None
            for i in range(idx_from, len(k) - horizon):
                f, last_fin = daily_factors(k, i, fin, last_fin)
                if not f:
                    continue
                fwd = (k[i + horizon]['c'] / k[i]['c'] - 1) * 100
                panel.append((k[i]['d'], code, f, fwd))
        except Exception:
            pass
        if verbose and (n + 1) % 10 == 0:
            print('    已载入 %d/%d（累计样本 %d）' % (n + 1, universe, len(panel)))
        time.sleep(0.2)
    return panel, cand


def compute_ic(panel, direction):
    """逐日横截面 IC。返回 (ic 序列, 有效因子值计数)"""
    by_day = {}
    for d, code, f, fwd in panel:
        by_day.setdefault(d, []).append((f, fwd))
    ics = []
    for d, lst in sorted(by_day.items()):
        if len(lst) < 8:
            continue
        vals, rets = [], []
        for f, fwd in lst:
            v = direction['fn'](f)
            if v is None or (isinstance(v, float) and (v != v)):
                continue
            vals.append(v)
            rets.append(fwd)
        if len(vals) < 8:
            continue
        ic = spearman(vals, rets)
        if ic is not None:
            ics.append(ic)
    return ics


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]

    if '--list' in sys.argv:
        print('=' * 92)
        print('【因子库】共 %d 个' % len(FACTORS))
        print('=' * 92)
        print('  %-14s %-6s %s' % ('因子', '类别', '理论方向'))
        for nm, cat, fn, dr in FACTORS:
            print('  %-14s %-6s %s' % (nm, cat, '值越大越好' if dr > 0 else '值越小越好'))
        return

    universe, days, horizon = 40, 150, 5
    for i, a in enumerate(sys.argv):
        if a == '--universe':
            universe = int(sys.argv[i + 1])
        if a == '--days':
            days = int(sys.argv[i + 1])
        if a == '--horizon':
            horizon = int(sys.argv[i + 1])

    if args and '--single' in sys.argv:
        code = args[0]
        k = ds.kline_qq(code, 400)
        fin = ds.financials(code)
        last_fin = None
        rows = []
        for i in range(60, len(k) - horizon):
            f, last_fin = daily_factors(k, i, fin, last_fin)
            if f:
                rows.append((k[i]['d'], f, (k[i + horizon]['c'] / k[i]['c'] - 1) * 100))
        print('【%s 单票因子时序】%d 个交易日，未来 %d 日收益' % (code, len(rows), horizon))
        print('  %-12s %10s %10s %10s %10s %10s %10s' % (
            '日期', 'RSI14', '%B', 'CCI20', '量比', 'ROE', '未来收益%'))
        for d, f, r in rows[-25:]:
            print('  %-12s %10.1f %10.1f %10.1f %10.2f %10s %+10.2f' % (
                d, f['rsi14'], f['pctb'], f['cci20'], f['vr'],
                ('%.2f' % f['roe']) if f['roe'] is not None else '—', r))
        return

    print('=' * 104)
    print('【量化因子挖掘】股票池 %d 只 × 最近 %d 个交易日，持有期 %d 日' % (universe, days, horizon))
    print('=' * 104)
    print('  财务因子走 PIT（只用当时已公告的财报）；种子固定为 20260922 以保证可复现')
    panel, picks = build_panel(universe, days, horizon)
    print('  样本总数 %d（%d 只票）' % (len(panel), len(picks)))
    n_days = len(set(p[0] for p in panel))
    print('  横截面天数 %d' % n_days)

    print()
    print('=' * 104)
    print('【因子 IC 排行】IC = 因子值与未来收益的逐日 Spearman 秩相关')
    print('=' * 104)
    print('  %-14s %-5s %8s %8s %7s %7s %6s  %s' % (
        '因子', '类别', 'IC均值', 'IC标准差', 'ICIR', 't值', '胜率', '判定'))

    results = []
    for nm, cat, fn, dr in FACTORS:
        ics = compute_ic(panel, {'fn': fn, 'dir': dr})
        if len(ics) < 10:
            continue
        m, s = mean(ics), std(ics)
        icir = m / s if s else 0.0
        t = icir * math.sqrt(len(ics))
        win = sum(1 for x in ics if x * dr > 0) / len(ics) * 100
        # 方向修正：把 IC 乘理论方向，便于横向比较（正 = 与理论一致）
        signed = m * dr
        results.append({'nm': nm, 'cat': cat, 'm': m, 's': s, 'icir': icir * dr,
                        't': t * dr, 'win': win, 'signed': signed, 'n': len(ics)})

    results.sort(key=lambda r: -abs(r['icir']))
    for r in results:
        if abs(r['t']) >= 3 and abs(r['signed']) >= 0.03:
            verdict = '✅ 有效'
        elif abs(r['t']) >= 2:
            verdict = '○ 边际'
        else:
            verdict = '× 噪声'
        print('  %-14s %-5s %+8.4f %8.4f %+7.3f %+7.2f %5.0f%%  %s' % (
            r['nm'], r['cat'], r['signed'], r['s'], r['icir'], r['t'], r['win'], verdict))

    print()
    print('  判据：|IC|≥0.03 且 |ICIR|≥0.3 且 |t|≥3 → 有效；|t|≥2 → 边际；否则视为噪声。')
    print('  ⚠️ IC 是**横截面**相关性，衡量"同一天里谁比谁强"，不是"能不能赚钱"；')
    print('     且未含交易成本、涨跌停无法成交、滑点——这些都要在策略层单独扣。')

    # 分组单调性（对 ICIR 最高的 5 个因子）
    print()
    print('=' * 104)
    print('【分组单调性检验】按因子分 5 组，看各组未来收益是否单调（只挑最好组 = 挑樱桃）')
    print('=' * 104)
    for r in results[:5]:
        fn = next(x[2] for x in FACTORS if x[0] == r['nm'])
        dr = next(x[3] for x in FACTORS if x[0] == r['nm'])
        by_day = {}
        for d, code, f, fwd in panel:
            by_day.setdefault(d, []).append((f, fwd))
        buckets = [[] for _ in range(5)]
        for d, lst in by_day.items():
            vv = []
            for f, fwd in lst:
                v = fn(f)
                if v is None or (isinstance(v, float) and v != v):
                    continue
                vv.append((v * dr, fwd))
            if len(vv) < 10:
                continue
            vv.sort(key=lambda t: t[0])
            step = len(vv) / 5.0
            for bi in range(5):
                seg = vv[int(bi * step):int((bi + 1) * step)]
                if seg:
                    buckets[bi].append(mean([x[1] for x in seg]))
        line = ' '.join('G%d:%+.2f%%' % (i + 1, mean(b)) for i, b in enumerate(buckets))
        mono = all(mean(buckets[i]) <= mean(buckets[i + 1]) for i in range(4)) or \
               all(mean(buckets[i]) >= mean(buckets[i + 1]) for i in range(4))
        print('  %-14s %s   %s' % (r['nm'], line, '✅单调' if mono else '⚠️非单调'))
    print('\n  ⚠️ 样本天数 %d，ICIR 的置信度随天数上升；本结论仅供筛选候选因子，' % n_days)
    print('     要用于实盘前需扩大股票池与时间跨度，并做样本外验证（见 references/calibration.md）。')


if __name__ == '__main__':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    main()
