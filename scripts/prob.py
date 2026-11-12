# -*- coding: utf-8 -*-
"""prob.py — **先分布、后概率**。v2：纳入板块 β + 多周期 + 自动落账（P109/P113）。

为什么要有这个模块
------------------
**P109**：把多组基准率各自报成区间再并列，彼此没有约束，必然矛盾。
2026-11-12 实景：同一张表里给了「站上 MA5 10.02」45–55%
与「跌破 MA10 9.96」50–62%（即站上 MA10 只有 38–50%）。
但 **10.02 > 9.96，站上更高门槛的概率不可能更大** —— 两条判断数学上互斥。

**P113**：概率模型只读个股 K 线，**看不见板块 β**。
同一天实测：成分股中位 −3.28% / 板块指数 −3.31% / 个股 −3.28% → **alpha 仅 +0.03pp**，
即跌幅 **100% 由板块解释**。在这种日子里算「个股次日收涨 48.9%」，
等于在不知道最重要的那个前提下给概率。
v2 起 `build_dist(bars, conds, beta_bars=...)` 会把板块字段注入每条记录。

正确做法（三层）
----------------
  ① 条件样本**按日期去重合并**成一个样本集；
  ② 构造次日/3日/5日收益经验分布（收/开/高/低）；
  ③ **所有阈值概率从同一分布读出** —— 单调性由构造保证，不靠事后检查。

记录字段（**字段名定死在 `_rec()`，调用方按名字取值，不要按位置解包**）
--------------------------------------------------------------------
个股侧：
  i/d       下标 / 日期
  chg       当日涨跌 %       pump  冲高（高/开−1）%     shad  上影占最高 %
  posr      收盘在当日区间位置 %（0=最低）
  vr        量比（当日量 / **前 20 日**均量，不含当日）
  ma5/ma10  当日均线（含当日）  below5 / above10
  prev      前一日涨跌 %        gap   今日开盘缺口 %
  r1/r3/r5  次日 / 3日 / 5日 收盘 %（相对当日收盘）
  open1     次日开盘 %
  hi1/lo1   次日最高/最低 %      amp1  次日振幅 %
  hi3/lo3   3 日内最高/最低 %    hi5/lo5  5 日内最高/最低 %

板块侧（`beta_bars` 存在时注入，否则为 None）：
  bk_chg    板块当日涨跌 %      bk_r1  板块次日涨跌 %
  bk_pos60  板块 60 日位置 %    bk_ma_up  板块多头排列（收盘>MA5>MA20）1/0
  bk_rsi6   板块 RSI6

用法
----
    import prob, ds
    bars   = ds.kline_list(CODE, 400)
    bkBars = ds.board_kline('BK1030', 400)          # ← β 维度
    D = prob.build_dist(bars, [
        lambda r: r['chg'] <= -3.0 and r['prev'] > 0,
        lambda r: r['chg'] <= -2.5 and r['vr'] <= 1.3,
    ], beta_bars=bkBars)
    print(prob.describe(D))
    print(prob.threshold_probs(D, [10.45, 10.02, 10.00, 9.96], base=10.00))
    print(prob.decision_tree(D, split=-1.0, base=10.00))
    print(prob.ev(D, cost_bps=17))

CLI：
    python prob.py <代码> --cond "chg<=-3 and prev>0" --beta BK1030 \
        --base 10.00 --split -1.0 --levels 10.45,10.02,10.00,9.96 --horizon 1 --cost 17
    python prob.py <代码> ... --cond "bk_chg<=-3"        # ← 用板块条件筛样本
    python prob.py <代码> ... --record                    # 自动写预测台账
"""
import sys
import math
import json

HORIZONS = (1, 3, 5)


# ------------------------------------------------------------------ 本地指标
def _ma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else float('nan')


def _rsi(vals, n=6):
    """RSI = 100*ag/(ag+al)，勿写补数。"""
    if len(vals) < n + 1:
        return float('nan')
    ag = al = 0.0
    for i in range(-n, 0):
        d = vals[i] - vals[i-1]
        ag += max(d, 0.0)
        al += max(-d, 0.0)
    ag /= n
    al /= n
    return 100*ag/(ag+al) if (ag+al) > 0 else 50.0


# ------------------------------------------------------------------ 板块序列
def beta_series(beta_bars):
    """把板块 K 线压成 `{日期: {板块字段}}`，供按日期对齐注入。

    ⚠️ 板块指标**必须用板块自己的 K 线算**，不能用成分股合成——
       两者口径不同（板块指数是加权，成分中位数是等权），实测今日 −3.31% vs −3.28%。
       但两者方向一致时可互为交叉验证。
    """
    out = {}
    if not beta_bars:
        return out
    c = [b['c'] for b in beta_bars]
    hi = [b['h'] for b in beta_bars]
    lo = [b['l'] for b in beta_bars]
    for i, b in enumerate(beta_bars):
        r = {'bk_chg': None, 'bk_r1': None, 'bk_pos60': None,
             'bk_ma_up': None, 'bk_rsi6': None}
        if i >= 1 and c[i-1] > 0:
            r['bk_chg'] = (c[i]/c[i-1]-1)*100
        if i >= 59:
            h60, l60 = max(hi[i-59:i+1]), min(lo[i-59:i+1])
            r['bk_pos60'] = (c[i]-l60)/(h60-l60)*100 if h60 > l60 else None
        if i >= 19:
            m5, m20 = sum(c[i-4:i+1])/5, sum(c[i-19:i+1])/20
            r['bk_ma_up'] = 1 if c[i] > m5 > m20 else 0
        if i >= 7:
            r['bk_rsi6'] = _rsi(c[:i+1], 6)
        if i+1 < len(beta_bars):
            r['bk_r1'] = (c[i+1]/c[i]-1)*100
        out[b['d']] = r
    return out


# ------------------------------------------------------------------ 构造记录
def _rec(bars, i):
    """把第 i 根及其后 1/3/5 根压成一条记录。字段含义见模块 docstring。"""
    b = bars
    p = b[i]
    c, o, h, l, v = p['c'], p['o'], p['h'], p['l'], p['v']
    pc = b[i-1]['c']
    if not all(isinstance(x, (int, float)) and x > 0 for x in (c, o, h, l, pc)):
        return None
    m5 = _ma([x['c'] for x in b[i-4:i+1]], 5)
    m10 = _ma([x['c'] for x in b[i-9:i+1]], 10)
    r = {
        'i': i, 'd': p['d'],
        'chg': (c/pc-1)*100.0,
        'pump': (h/o-1)*100.0,
        'shad': (h-c)/h*100.0 if h else 0.0,
        'posr': (c-l)/(h-l)*100.0 if h > l else 100.0,
        'ma5': m5, 'ma10': m10,
        'prev': (pc/b[i-2]['c']-1)*100.0 if i >= 2 and b[i-2]['c'] else 0.0,
        'gap': (o/pc-1)*100.0,
        'below5': c < m5,
        'above10': c >= m10,
    }
    base_v = [x['v'] for x in b[max(0, i-20):i]]           # 不含当日
    m20v = sum(base_v)/len(base_v) if base_v else 0
    r['vr'] = (v/m20v) if m20v else 0.0

    # 多周期收盘收益
    for k in HORIZONS:
        r['r%d' % k] = None
        if i+k < len(b) and b[i+k]['c'] > 0:
            r['r%d' % k] = (b[i+k]['c']/c-1)*100.0
    # 次日极值
    r.update({'open1': None, 'hi1': None, 'lo1': None, 'amp1': None})
    if i+1 < len(b):
        n = b[i+1]
        r['open1'] = (n['o']/c-1)*100.0
        r['hi1'] = (n['h']/c-1)*100.0
        r['lo1'] = (n['l']/c-1)*100.0
        r['amp1'] = r['hi1'] - r['lo1']
    # 3/5 日内极值
    for k in (3, 5):
        r['hi%d' % k] = None
        r['lo%d' % k] = None
        seg = b[i+1:i+1+k]
        if len(seg) == k:
            r['hi%d' % k] = (max(x['h'] for x in seg)/c-1)*100.0
            r['lo%d' % k] = (min(x['l'] for x in seg)/c-1)*100.0
    # 板块字段占位
    for f in ('bk_chg', 'bk_r1', 'bk_pos60', 'bk_ma_up', 'bk_rsi6'):
        r[f] = None
    return r


def records(bars, warmup=30, beta_bars=None):
    """把 bars 转成记录列表。给了 beta_bars 就按日期注入板块字段。"""
    bser = beta_series(beta_bars) if beta_bars else {}
    out = []
    for i in range(max(warmup, 2), len(bars)):
        r = _rec(bars, i)
        if not r:
            continue
        if bser:
            hit = bser.get(r['d'])
            if hit:
                r.update(hit)
        out.append(r)
    return out


# ------------------------------------------------------------------ 分布
def build_dist(bars, conds, warmup=30, beta_bars=None, require_all=True):
    """按条件筛样本、**按日期去重合并**，返回分布 dict。

    conds: list[callable] —— 每个接收记录 dict，返回 bool。多个条件是**并集**（OR）。
           需要交集请写在一个 lambda 里。条件里可以引用板块字段（`bk_chg` 等）。
    beta_bars: 板块 K 线数组（`ds.board_kline`）。给了就能用 `bk_*` 字段作条件。
    require_all: True 时只保留 1/3/5 日数据齐全的样本（多周期统计要求）。
    """
    assert conds, '至少给一个条件'
    rows = records(bars, warmup, beta_bars)
    by_date = {}
    err_n = 0
    for r in rows:
        if require_all and (r['r1'] is None or r['r3'] is None or r['r5'] is None):
            continue
        hits = []
        for k, fn in enumerate(conds):
            try:
                if fn(r):
                    hits.append(k)
            except Exception:
                # ⚠️ 条件里引用了该样本缺失的字段（如板块数据未覆盖的日期 → bk_chg=None，
                #    `None <= -3` 会抛 TypeError）。**异常必须算"不匹配"并计数**，
                #    不能静默吞掉——否则「板块条件筛出 0 条」会被误读成「历史上没出现过」。
                err_n += 1
        if not hits:
            continue
        if r['d'] in by_date:
            for k in hits:
                if k not in by_date[r['d']]['tags']:
                    by_date[r['d']]['tags'].append(k)
        else:
            r2 = dict(r)
            r2['tags'] = list(hits)
            by_date[r['d']] = r2
    recs = [by_date[d] for d in sorted(by_date)]
    out = {'n': len(recs), 'dates': [r['d'] for r in recs],
           'tags': {r['d']: r['tags'] for r in recs}, 'recs': recs,
           'cond_errors': err_n, 'rows_scanned': len(rows)}
    for k in ('r1', 'r3', 'r5', 'open1', 'hi1', 'lo1', 'amp1', 'hi3', 'lo3', 'hi5', 'lo5'):
        out[k] = [r.get(k) for r in recs if r.get(k) is not None]
    return out


# ------------------------------------------------------------------ 统计
def _q(vals, k):
    s = sorted(vals)
    if not s:
        return float('nan')
    x = (len(s)-1)*k
    f = int(x)
    c = min(f+1, len(s)-1)
    return s[f] + (s[c]-s[f])*(x-f)


def quantiles(dist, qs=(0.1, 0.25, 0.5, 0.75, 0.9), horizon=1):
    return {q: _q(dist['r%d' % horizon], q) for q in qs}


def _binom_se(p, n):
    return math.sqrt(p*(1-p)/n) if n else float('nan')


def describe(dist, verbose=True, horizon=1):
    """一段文字摘要。**用中位数不用均值**——均值会被极少数极端样本抬高。"""
    n = dist['n']
    if n == 0:
        return '空分布（条件无匹配样本）'
    r = dist['r%d' % horizon]
    if not r:
        return 'h%d：该周期无数据' % horizon
    m = len(r)
    up = sum(1 for x in r if x > 0)
    dn = sum(1 for x in r if x < 0)
    flat = m - up - dn
    trim = [x for x in r if -5 < x < 5]
    up_t = sum(1 for x in trim if x > 0)
    mean = sum(r)/m
    sd = (sum((x-mean)**2 for x in r)/(m-1))**.5 if m > 1 else 0
    lines = [
        'N=%d  %s ~ %s  持有期 %d 日' % (n, dist['dates'][0], dist['dates'][-1], horizon),
        '收涨 %d(%.0f%%) / 收跌 %d(%.0f%%) / 收平 %d(%.0f%%)' % (
            up, 100*up/m, dn, 100*dn/m, flat, 100*flat/m),
        '中位 %+.2f%%  均值 %+.2f%%  标差 %.2f%%' % (_q(r, .5), mean, sd),
        '剔除 ±5%% 极端后：N=%d 收涨 %.0f%% 均值 %+.2f%%  ← 稳健口径' % (
            len(trim), 100*up_t/len(trim) if trim else 0, sum(trim)/len(trim) if trim else 0),
        'p10 %+.2f%%  p25 %+.2f%%  p50 %+.2f%%  p75 %+.2f%%  p90 %+.2f%%' % tuple(
            _q(r, q) for q in (.1, .25, .5, .75, .9)),
        '二项标准误(p≈50%%) %.1fpp → 95%%CI 约 %.0f~%.0f%%' % (
            100*_binom_se(.5, m), 100*(.5-1.96*_binom_se(.5, m)), 100*(.5+1.96*_binom_se(.5, m))),
    ]
    return '\n'.join(lines) if verbose else '; '.join(lines)


def all_horizons(dist):
    """1/3/5 日并列摘要，一眼看周期差异。"""
    out = []
    for k in HORIZONS:
        r = dist.get('r%d' % k) or []
        if not r:
            out.append({'h': k, 'n': 0, 'up': None, 'median': None, 'mean': None})
            continue
        out.append({'h': k, 'n': len(r),
                    'up': 100*sum(1 for x in r if x > 0)/len(r),
                    'median': _q(r, .5), 'mean': sum(r)/len(r)})
    return out


def threshold_probs(dist, levels, base, horizon=1):
    """**所有阈值概率从同一分布读出**。

    levels: [(price, label)] 或 [price]；base: 基准价（通常信号日收盘）。
    返回按 need **降序**（门槛从高到低），此时 prob 必须**单调不增**。
    若出现非单调，**直接抛错**——说明分布被污染，不能静默输出。
    """
    r = dist['r%d' % horizon]
    n = len(r)
    assert n > 0, 'h%d 空分布' % horizon
    items = []
    for x in levels:
        if isinstance(x, (list, tuple)):
            px, lab = x[0], x[1]
        else:
            px, lab = x, '%.4g' % x
        need = (px/base-1)*100.0
        k = sum(1 for v in r if v > need)
        items.append({'label': lab, 'price': px, 'need': need,
                      'prob': 100.0*k/n, 'k': k, 'n': n})
    items.sort(key=lambda z: -z['need'])
    prev = None
    for it in items:
        if prev is not None and it['prob'] < prev - 1e-9:
            raise ValueError(
                '阈值概率非单调：%s(%.1f%%) < 上一项(%.1f%%)。'
                '门槛更低却概率更小，说明分布被污染或 levels 有重复。'
                % (it['label'], it['prob'], prev))
        prev = it['prob']
    return items


def buckets(dist, edges=None, horizon=1):
    """收益分档（互斥完备，和必为 100%）。"""
    if edges is None:
        edges = [(-1e9, -3), (-3, -1), (-1, 0), (0, 1), (1, 3), (3, 1e9)]
    r = dist['r%d' % horizon]
    n = len(r)
    out = []
    for a, b in edges:
        k = sum(1 for x in r if a <= x < b)
        lab = ('<%g%%' % b) if a == -1e9 else (('>=%+g%%' % a) if b == 1e9 else '%g~%g%%' % (a, b))
        out.append({'label': lab, 'k': k, 'prob': 100.0*k/n if n else 0})
    assert sum(x['k'] for x in out) == n, '分档未覆盖全部样本'
    return out


# ------------------------------------------------------------------ 决策树
def _lap(k, n):
    """Laplace 平滑：(k+1)/(n+2)。小样本格子必须平滑，否则会出现 100%。"""
    return (k+1.0)/(n+2.0)


def decision_tree(dist, split, base, split_label=None):
    """两维分叉：① 开盘方向 ② 次日盘中是否跌破 split（相对 base 的 %）。

    返回四格 + **加权回等边际**校验（联合概率 × 各格概率之和必须 ≈ 边际）。
    另给两个边际条件概率：不破线时收涨 / 破线时收涨。
    """
    o, lo, r = dist['open1'], dist['lo1'], dist['r1']
    n = min(len(o), len(lo), len(r))
    assert n > 0, '次日数据不足，无法做决策树'
    o, lo, r = o[:n], lo[:n], r[:n]
    lab = split_label or ('%.3f' % (base*(1+split/100)))
    cells = []
    for on_lab, on_f in (('高开', lambda x: x > 0), ('低开/平开', lambda x: x <= 0)):
        for br_lab, br_f in ((f'不破 {lab}', lambda x: x > split),
                             (f'破 {lab}', lambda x: x <= split)):
            idx = [i for i in range(n) if on_f(o[i]) and br_f(lo[i])]
            up = sum(1 for i in idx if r[i] > 0)
            cells.append({
                'open': on_lab, 'break': br_lab,
                'joint': 100.0*len(idx)/n, 'n': len(idx),
                'up_k': up,
                'up_prob': 100.0*up/len(idx) if idx else float('nan'),
                'up_lap': 100.0*_lap(up, len(idx)),
                'median': _q([r[i] for i in idx], .5) if idx else float('nan'),
            })
    marg_up = 100.0*sum(1 for x in r if x > 0)/n
    weighted = sum(c['joint']/100.0*c['up_prob'] for c in cells if c['n'])
    nb = sum(1 for x in lo if x > split)
    nbr = n - nb
    return {
        'cells': cells, 'marginal_up': marg_up, 'weighted_up': weighted,
        'consistent': abs(weighted-marg_up) < 2.0,
        'cond': {
            'high_open': 100.0*sum(1 for x in o if x > 0)/n,
            'break_split': 100.0*nbr/n,
            'up_if_no_break': 100.0*sum(1 for i in range(n) if lo[i] > split and r[i] > 0)/max(1, nb),
            'up_if_break': 100.0*sum(1 for i in range(n) if lo[i] <= split and r[i] > 0)/max(1, nbr),
        }}


# ------------------------------------------------------------------ EV
def ev(dist, cost_bps, horizon=1):
    """三种口径的净期望（%）。cost_bps 双边费用，单位基点（17 = 0.17%）。

    **决策用中位数**。全样本均值会被极少数极端样本抬高——
    实测 4 个 ≥+5% 样本就能把均值从 −0.09% 抬到 +0.41%，**正负号翻转**。
    """
    r = dist['r%d' % horizon]
    n = len(r)
    assert n > 0, 'h%d 空分布' % horizon
    trim = [x for x in r if -5 < x < 5]
    cost = cost_bps/100.0
    out = {}
    for lab, vals, cnt in (('median', [_q(r, .5)], n),
                           ('trimmed_mean', trim, len(trim)),
                           ('mean', r, n)):
        if not vals:
            continue
        m = vals[0] if lab == 'median' else sum(vals)/len(vals)
        out[lab] = {'gross': m, 'net': m-cost, 'n': cnt}
    return {'cost_pct': cost, 'by': out, 'horizon': horizon,
            'verdict': '负期望' if out.get('median', {}).get('net', 0) < 0 else '正期望'}


# ------------------------------------------------------------------ CLI
def _safe_eval(expr, r):
    """受限 eval：只暴露记录字段和白名单内建函数。"""
    allowed = dict(r)
    allowed.update({'abs': abs, 'min': min, 'max': max, 'round': round})
    return eval(expr, {'__builtins__': {}}, allowed)  # noqa: S307 本地研究工具


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    code = argv[1]
    A = {'cond': [], 'levels': [], 'base': None, 'split': -1.0, 'cost': 17.0,
         'bars': 400, 'json': False, 'beta': None, 'horizon': 1, 'record': False}
    i = 2
    while i < len(argv):
        a = argv[i]
        if a == '--cond':
            A['cond'].append(argv[i+1]); i += 2
        elif a == '--levels':
            A['levels'] = [float(x) for x in argv[i+1].split(',') if x.strip()]; i += 2
        elif a == '--base':
            A['base'] = float(argv[i+1]); i += 2
        elif a == '--split':
            A['split'] = float(argv[i+1]); i += 2
        elif a == '--cost':
            A['cost'] = float(argv[i+1]); i += 2
        elif a == '--bars':
            A['bars'] = int(argv[i+1]); i += 2
        elif a == '--beta':
            A['beta'] = argv[i+1]; i += 2
        elif a == '--horizon':
            A['horizon'] = int(argv[i+1]); i += 2
        elif a == '--record':
            A['record'] = True; i += 1
        elif a == '--json':
            A['json'] = True; i += 1
        else:
            print('未知参数 %s' % a); return 2
    if not A['cond']:
        print('至少给一个 --cond'); return 2

    import ds
    bars = ds.kline_list(code, A['bars'])
    if not bars:
        print('取数失败：kline_list 返回空'); return 1
    bkBars = None
    if A['beta']:
        bkBars = ds.board_kline(A['beta'], max(A['bars'], 200))
        if not bkBars:
            print('板块 %s K 线取数失败 → β 维度不可用；**不静默降级**，请确认代码' % A['beta'])
            return 1
        chk = ds.board_check(A['beta'])
        print('β = %s（%s）' % (A['beta'], chk['name'] or '名称未取到'))
    base = A['base'] or bars[-1]['c']
    conds = [(lambda e: (lambda r: bool(_safe_eval(e, r))))(e) for e in A['cond']]
    D = build_dist(bars, conds, beta_bars=bkBars)

    if D['n'] == 0:
        print('=== 分布 ===')
        print(describe(D))
        print('\n条件无匹配样本 —— 按失败处理（不要当成「没有信号」）。')
        return 1

    if A['json']:
        print(json.dumps({'n': D['n'], 'dates': D['dates'],
                          'r1': D['r1'], 'r3': D['r3'], 'r5': D['r5'],
                          'h': all_horizons(D)}, ensure_ascii=False, indent=1))
        return 0

    print('=== 分布（持有期 %d 日） ===' % A['horizon'])
    if D.get('cond_errors'):
        print('⚠️ 有 %d 次条件求值异常（多为板块字段缺失的日期，如 bk_chg=None），'
              '已按「不匹配」处理；共扫描 %d 条记录。'
              % (D['cond_errors'], D['rows_scanned']))
    print(describe(D, horizon=A['horizon']))
    print()
    print('=== 多周期并列 ===')
    for x in all_horizons(D):
        if x['n']:
            print('  %d 日：N=%d 收涨 %.1f%% 中位 %+.2f%% 均值 %+.2f%%' % (
                x['h'], x['n'], x['up'], x['median'], x['mean']))
    print()
    if A['levels']:
        print('=== 阈值概率（基准 %.3f，持有期 %d 日） ===' % (base, A['horizon']))
        for it in threshold_probs(D, A['levels'], base, horizon=A['horizon']):
            print('  %-10s 需 %+6.2f%%  %5.1f%%  (%d/%d)' % (
                it['label'], it['need'], it['prob'], it['k'], it['n']))
        print()
    print('=== 收益分档（h%d） ===' % A['horizon'])
    for b in buckets(D, horizon=A['horizon']):
        print('  %-12s %5.1f%%  (%d)' % (b['label'], b['prob'], b['k']))
    print()
    print('=== 决策树（次日，split %+.2f%% = %.3f） ===' % (A['split'], base*(1+A['split']/100)))
    T = decision_tree(D, A['split'], base, '%.3f' % (base*(1+A['split']/100)))
    for c in T['cells']:
        print('  %-8s %-14s 联合 %5.1f%% (%2d) → 收涨 %5.1f%% (平滑 %.0f%%) 中位 %+.2f%%' % (
            c['open'], c['break'], c['joint'], c['n'], c['up_prob'], c['up_lap'], c['median']))
    print('  边际收涨 %.1f%% | 四格加权 %.1f%% | %s' % (
        T['marginal_up'], T['weighted_up'], '自洽 ✓' if T['consistent'] else '**不自洽**'))
    cd = T['cond']
    print('  条件：高开 %.0f%% | 破线 %.0f%% | 不破时收涨 %.0f%% | 破线时收涨 %.0f%%' % (
        cd['high_open'], cd['break_split'], cd['up_if_no_break'], cd['up_if_break']))
    print()
    print('=== EV（费用 %.0f bp，持有期 %d 日） ===' % (A['cost'], A['horizon']))
    E = ev(D, A['cost'], horizon=A['horizon'])
    for lab, nm in (('median', '中位数(决策用)'), ('trimmed_mean', '剔除±5%均值'), ('mean', '全样本均值')):
        if lab in E['by']:
            x = E['by'][lab]
            print('  %-14s 毛 %+0.2f%%  净 %+0.2f%%  (N=%d)' % (nm, x['gross'], x['net'], x['n']))
    print('  结论：%s' % E['verdict'])

    if A['record']:
        print()
        print('=== 写入预测台账 ===')
        try:
            import journal
            import datetime
            probs = {
                'open_up': round(100.0*sum(1 for x in D['open1'] if x > 0)/max(1, len(D['open1'])), 1),
                'close_up': round(100.0*sum(1 for x in D['r1'] if x > 0)/max(1, len(D['r1'])), 1),
                'high_ge1': round(100.0*sum(1 for x in D['hi1'] if x >= 1.0)/max(1, len(D['hi1'])), 1),
                'low_le1': round(100.0*sum(1 for x in D['lo1'] if x <= -1.0)/max(1, len(D['lo1'])), 1),
                'break3': round(100.0*sum(1 for x in D['lo1'] if x <= -3.0)/max(1, len(D['lo1'])), 1),
            }
            rec = journal.record_forecast(
                code, datetime.date.today().isoformat(), probs, base, neighbour_n=D['n'],
                meta={'method': 'prob.py v2 条件分布', 'beta': A['beta'],
                      'conds': A['cond'], 'horizon': A['horizon'],
                      'note': '概率取自条件样本分布，非模型输出'})
            print('  ✓ 已写入 %s' % rec['id'])
            print('  %s' % json.dumps(probs, ensure_ascii=False))
        except Exception as e:
            print('  ✗ 写入失败：%r（不做静默忽略）' % e)
            return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
