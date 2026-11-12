# -*- coding: utf-8 -*-
"""prob.py — **先分布、后概率**（P109）。

为什么要有这个模块
------------------
把多组基准率（本股形态 / P4 / 指数…）**各自报成区间再并列**，彼此没有约束，必然矛盾。
2026-11-12 实景：同一张表里给了「站上 MA5 10.02」45–55% 与「跌破 MA10 9.96」50–62%
（即站上 MA10 只有 38–50%）。但 **10.02 > 9.96，站上更高门槛的概率不可能更大** ——
两条判断数学上互斥，却同时出现在一份报告里。

正确做法：
  ① 条件样本**按日期去重合并**成一个样本集；
  ② 构造次日收益（收/开/高/低）经验分布；
  ③ **所有阈值概率从同一分布读出** —— 单调性由构造保证，不靠事后检查。

本模块只做这三件事，外加决策树与 EV。纯标准库，不依赖 ds（bars 由调用方传入）。

记录字段（**顺序在 `_rec()` 里定死，调用方按名字取值，不要按位置解包**）
--------------------------------------------------------------------
  i        样本在 bars 中的下标
  d        日期 'YYYY-MM-DD'
  chg      当日涨跌幅 %（收/昨收 −1）
  pump     冲高幅度 %（高/开 −1）
  shad     上影占最高价 %（(高−收)/高×100）
  posr     收盘在当日区间的位置 %（(收−低)/(高−低)×100，0=最低）
  vr       量比（当日量 / 前 20 日均量，不含当日）
  ma5/ma10 当日 MA5 / MA10（**含当日**，口径与 ds.ma 一致）
  below5   收盘 < MA5
  above10  收盘 >= MA10
  prev     前一日涨跌幅 %
  gap      今日相对昨收的开盘缺口 %
  r1       次日收盘 %（相对当日收盘）
  open1    次日开盘 %（相对当日收盘）
  hi1      次日最高 %（相对当日收盘）
  lo1      次日最低 %（相对当日收盘）
  amp1     次日振幅 %（hi1 − lo1）

用法
----
    import prob, ds
    bars = ds.kline_list(CODE, 400)
    D = prob.build_dist(bars, [
        lambda r: r['chg'] <= -3.0 and r['prev'] > 0,
        lambda r: r['chg'] <= -2.5 and r['vr'] <= 1.3,
    ])
    print(prob.describe(D))
    print(prob.threshold_probs(D, [10.45, 10.02, 10.00, 9.96], base=10.00))
    print(prob.decision_tree(D, split=-1.0, base=10.00))
    print(prob.ev(D, cost_bps=17))

CLI：
    python prob.py <代码> --cond "chg<=-3 and prev>0" --cond "chg<=-2.5 and vr<=1.3" \
        --base 10.00 --split -1.0 --levels 10.45,10.02,10.00,9.96,9.70 --cost 17
"""
import sys
import math
import json


# ------------------------------------------------------------------ 构造记录
def _rec(bars, i):
    """把第 i 根及其后一根压成一条记录。字段含义见模块 docstring。"""
    b = bars
    p = b[i]
    c, o, h, l, v = p['c'], p['o'], p['h'], p['l'], p['v']
    pc = b[i-1]['c']
    if not all(isinstance(x, (int, float)) and x > 0 for x in (c, o, h, l, pc)):
        return None
    ma5 = sum(x['c'] for x in b[i-4:i+1]) / 5.0
    ma10 = sum(x['c'] for x in b[i-9:i+1]) / 10.0
    base_v = [x['v'] for x in b[max(0, i-20):i]]          # 不含当日
    ma20v = sum(base_v)/len(base_v) if base_v else 0
    r = {
        'i': i, 'd': p['d'],
        'chg': (c/pc-1)*100.0,
        'pump': (h/o-1)*100.0,
        'shad': (h-c)/h*100.0 if h else 0.0,
        'posr': (c-l)/(h-l)*100.0 if h > l else 100.0,
        'vr': (v/ma20v) if ma20v else 0.0,
        'ma5': ma5, 'ma10': ma10,
        'below5': c < ma5,
        'above10': c >= ma10,
        'prev': (pc/b[i-2]['c']-1)*100.0 if i >= 2 and b[i-2]['c'] else 0.0,
        'gap': (o/pc-1)*100.0,
        'r1': None, 'open1': None, 'hi1': None, 'lo1': None, 'amp1': None,
    }
    if i+1 < len(b):
        n = b[i+1]
        r['r1'] = (n['c']/c-1)*100.0
        r['open1'] = (n['o']/c-1)*100.0
        r['hi1'] = (n['h']/c-1)*100.0
        r['lo1'] = (n['l']/c-1)*100.0
        r['amp1'] = r['hi1'] - r['lo1']
    return r


def records(bars, warmup=30):
    """把 bars 转成记录列表。warmup 保证 MA10 / 20 日均量都可算。"""
    out = []
    for i in range(max(warmup, 2), len(bars)):
        r = _rec(bars, i)
        if r:
            out.append(r)
    return out


# ------------------------------------------------------------------ 分布
def build_dist(bars, conds, warmup=30, require_next=True):
    """按条件筛样本、**按日期去重合并**，返回分布 dict。

    conds: list[callable] —— 每个 callable 接收记录 dict，返回 bool。
           多个条件是**并集**（OR），不是交集。需要交集请写在一个 lambda 里。
    require_next: 只要次日数据齐全的样本（预测用必须 True）。

    返回 {n, r1, open1, hi1, lo1, amp1, dates, tags, recs}
      tags: {日期: [命中的条件序号]}，用于核对去重是否正确
    """
    assert conds, '至少给一个条件'
    by_date = {}
    for r in records(bars, warmup):
        if require_next and r['r1'] is None:
            continue
        hits = [k for k, fn in enumerate(conds) if fn(r)]
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
    if not recs:
        return {'n': 0, 'r1': [], 'open1': [], 'hi1': [], 'lo1': [], 'amp1': [],
                'dates': [], 'tags': {}, 'recs': []}
    return {
        'n': len(recs),
        'r1': [r['r1'] for r in recs],
        'open1': [r['open1'] for r in recs],
        'hi1': [r['hi1'] for r in recs],
        'lo1': [r['lo1'] for r in recs],
        'amp1': [r['amp1'] for r in recs],
        'dates': [r['d'] for r in recs],
        'tags': {r['d']: r['tags'] for r in recs},
        'recs': recs,
    }


# ------------------------------------------------------------------ 统计
def _q(vals, k):
    s = sorted(vals)
    if not s:
        return float('nan')
    x = (len(s)-1)*k
    f = int(x)
    c = min(f+1, len(s)-1)
    return s[f] + (s[c]-s[f])*(x-f)


def quantiles(dist, qs=(0.1, 0.25, 0.5, 0.75, 0.9)):
    return {q: _q(dist['r1'], q) for q in qs}


def _binom_se(p, n):
    return math.sqrt(p*(1-p)/n) if n else float('nan')


def describe(dist, verbose=True):
    """一段文字摘要。**用中位数不用均值**——均值会被极少数 ≥+5% 样本抬高。"""
    n = dist['n']
    if n == 0:
        return '空分布（条件无匹配样本）'
    r = dist['r1']
    up = sum(1 for x in r if x > 0)
    dn = sum(1 for x in r if x < 0)
    flat = n - up - dn
    trim = [x for x in r if -5 < x < 5]
    up_t = sum(1 for x in trim if x > 0)
    lines = [
        'N=%d  %s ~ %s' % (n, dist['dates'][0], dist['dates'][-1]),
        '收涨 %d(%.0f%%) / 收跌 %d(%.0f%%) / 收平 %d(%.0f%%)' % (
            up, 100*up/n, dn, 100*dn/n, flat, 100*flat/n),
        '中位 %+.2f%%  均值 %+.2f%%  标差 %.2f%%' % (
            _q(r, .5), sum(r)/n, (sum((x-sum(r)/n)**2 for x in r)/(n-1))**.5 if n > 1 else 0),
        '剔除 ±5%% 极端后：N=%d 收涨 %.0f%% 均值 %+.2f%%  ← 稳健口径' % (
            len(trim), 100*up_t/len(trim) if trim else 0,
            sum(trim)/len(trim) if trim else 0),
        'p10 %+.2f%%  p25 %+.2f%%  p50 %+.2f%%  p75 %+.2f%%  p90 %+.2f%%' % tuple(
            _q(r, q) for q in (.1, .25, .5, .75, .9)),
        '二项标准误(p≈50%%) %.1fpp → 95%%CI 约 %.0f~%.0f%%' % (
            100*_binom_se(.5, n), 100*(.5-1.96*_binom_se(.5, n)), 100*(.5+1.96*_binom_se(.5, n))),
    ]
    return '\n'.join(lines) if verbose else '; '.join(lines)


def threshold_probs(dist, levels, base):
    """**所有阈值概率从同一分布读出**。

    levels: [(price, label)] 或 [price]；base: 今日收盘价。
    返回 [{label, price, need, prob, k, n}]，按 need **降序**排列
    —— 门槛从高到低，概率必须**单调不增**（need 越高 → prob 越小，所以按 need 降序时 prob 递增）。
    若出现非单调，**直接抛错**——说明分布本身有问题，不能静默输出。
    """
    n = dist['n']
    assert n > 0, '空分布'
    items = []
    for x in levels:
        if isinstance(x, (list, tuple)):
            px, lab = x[0], x[1]
        else:
            px, lab = x, '%.4g' % x
        need = (px/base-1)*100.0
        k = sum(1 for v in dist['r1'] if v > need)
        items.append({'label': lab, 'price': px, 'need': need,
                      'prob': 100.0*k/n, 'k': k, 'n': n})
    items.sort(key=lambda z: -z['need'])
    # need 降序（门槛从高到低）→ prob 必须单调**不增**，即序列上表现为递增
    prev = None
    for it in items:
        if prev is not None and it['prob'] < prev - 1e-9:
            raise ValueError(
                '阈值概率非单调：%s(%.1f%%) < 上一项(%.1f%%)。'
                '门槛更低却概率更小，说明分布被污染或 levels 有重复。' % (it['label'], it['prob'], prev))
        prev = it['prob']
    return items


def buckets(dist, edges=None):
    """收盘收益分档（互斥完备，和必为 100%）。edges 为左闭右开边界。"""
    if edges is None:
        edges = [(-1e9, -3), (-3, -1), (-1, 0), (0, 1), (1, 3), (3, 1e9)]
    n = dist['n']
    out = []
    for a, b in edges:
        k = sum(1 for x in dist['r1'] if a <= x < b)
        if a == -1e9:
            lab = '<%g%%' % b
        elif b == 1e9:
            lab = '>=%+g%%' % a
        else:
            lab = '%g~%g%%' % (a, b)
        out.append({'label': lab, 'k': k, 'prob': 100.0*k/n})
    assert sum(x['k'] for x in out) == n, '分档未覆盖全部样本'
    return out


# ------------------------------------------------------------------ 决策树
def _lap(k, n):
    """Laplace 平滑：(k+1)/(n+2)。小样本格子必须平滑，否则会出现 100%。"""
    return (k+1.0)/(n+2.0)


def decision_tree(dist, split, base, split_label='分割价'):
    """两维分叉：① 开盘方向 ② 盘中是否跌破 split。

    split: 相对 base 的百分比阈值，例如 -1.0 表示「跌破 base×0.99」。
    返回四格，并做**加权回等边际**校验（四格联合概率×各格概率之和必须≈边际概率）。
    """
    n = dist['n']
    assert n > 0
    cells = []
    for on_lab, on_f in (('高开', lambda o: o > 0), ('低开/平开', lambda o: o <= 0)):
        for br_lab, br_f in ((f'不破 {split_label}', lambda l: l > split),
                             (f'破 {split_label}', lambda l: l <= split)):
            idx = [i for i in range(n) if on_f(dist['open1'][i]) and br_f(dist['lo1'][i])]
            up = sum(1 for i in idx if dist['r1'][i] > 0)
            cells.append({
                'open': on_lab, 'break': br_lab,
                'joint': 100.0*len(idx)/n, 'n': len(idx),
                'up_k': up, 'up_prob': 100.0*up/len(idx) if idx else float('nan'),
                'up_lap': 100.0*_lap(up, len(idx)),
                'median': _q([dist['r1'][i] for i in idx], .5) if idx else float('nan'),
            })
    marg_up = 100.0*sum(1 for x in dist['r1'] if x > 0)/n
    weighted = sum(c['joint']/100.0*c['up_prob'] for c in cells if c['n'])
    return {'cells': cells, 'marginal_up': marg_up, 'weighted_up': weighted,
            'consistent': abs(weighted-marg_up) < 2.0,
            'cond': {
                'high_open': 100.0*sum(1 for o in dist['open1'] if o > 0)/n,
                'break_split': 100.0*sum(1 for l in dist['lo1'] if l <= split)/n,
                'up_if_no_break': 100.0*sum(1 for i in range(n) if dist['lo1'][i] > split and dist['r1'][i] > 0)
                                   / max(1, sum(1 for l in dist['lo1'] if l > split)),
                'up_if_break': 100.0*sum(1 for i in range(n) if dist['lo1'][i] <= split and dist['r1'][i] > 0)
                                / max(1, sum(1 for l in dist['lo1'] if l <= split)),
            }}


# ------------------------------------------------------------------ EV
def ev(dist, cost_bps):
    """三种口径的净期望（%）。cost_bps 为双边费用，单位基点（17 = 0.17%）。

    **决策用中位数口径**。全样本均值会被极少数 ≥+5% 样本抬高，
    本股实测：剔除 ±5% 后均值从 +0.43% 掉到 −0.07%，正好跨过 0。
    """
    n = dist['n']
    assert n > 0
    r = dist['r1']
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
    return {'cost_pct': cost, 'by': out,
            'verdict': '负期望' if out.get('median', {}).get('net', 0) < 0 else '正期望'}


# ------------------------------------------------------------------ CLI
def _safe_eval(expr, r):
    """受限 eval：只暴露记录字段和白名单内建函数。"""
    allowed = {k: r[k] for k in r}
    allowed.update({'abs': abs, 'min': min, 'max': max, 'round': round})
    return eval(expr, {'__builtins__': {}}, allowed)  # noqa: S307 本地研究工具


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    code = argv[1]
    args = {'cond': [], 'levels': [], 'base': None, 'split': -1.0, 'cost': 17.0,
            'bars': 400, 'json': False}
    i = 2
    while i < len(argv):
        a = argv[i]
        if a == '--cond':
            args['cond'].append(argv[i+1]); i += 2
        elif a == '--levels':
            args['levels'] = [float(x) for x in argv[i+1].split(',') if x.strip()]; i += 2
        elif a == '--base':
            args['base'] = float(argv[i+1]); i += 2
        elif a == '--split':
            args['split'] = float(argv[i+1]); i += 2
        elif a == '--cost':
            args['cost'] = float(argv[i+1]); i += 2
        elif a == '--bars':
            args['bars'] = int(argv[i+1]); i += 2
        elif a == '--json':
            args['json'] = True; i += 1
        else:
            print('未知参数 %s' % a); return 2
    if not args['cond']:
        print('至少给一个 --cond'); return 2

    import ds
    bars = ds.kline_list(code, args['bars'])
    if not bars:
        print('取数失败：kline_list 返回空'); return 1
    # 若末根是今日（未走完或刚收盘），预测基准用末根收盘
    base = args['base'] or bars[-1]['c']
    conds = [(lambda e: (lambda r: bool(_safe_eval(e, r))))(e) for e in args['cond']]
    D = build_dist(bars, conds)

    if args['json']:
        print(json.dumps({'describe': describe(D, False), 'n': D['n'],
                          'dates': D['dates'], 'r1': D['r1']}, ensure_ascii=False, indent=1))
        return 0

    print('=== 分布 ===')
    print(describe(D))
    if D['n'] == 0:
        print('\n条件无匹配样本 —— 按失败处理（不要当成「没有信号」）。')
        return 1
    print()
    if args['levels']:
        print('=== 阈值概率（基准 %.3f） ===' % base)
        for it in threshold_probs(D, args['levels'], base):
            print('  %-10s 需 %+6.2f%%  %5.1f%%  (%d/%d)' % (
                it['label'], it['need'], it['prob'], it['k'], it['n']))
        print()
    print('=== 收盘分档 ===')
    for b in buckets(D):
        print('  %-12s %5.1f%%  (%d)' % (b['label'], b['prob'], b['k']))
    print()
    print('=== 决策树（split %+.2f%% = %.3f） ===' % (args['split'], base*(1+args['split']/100)))
    T = decision_tree(D, args['split'], base, '%.3f' % (base*(1+args['split']/100)))
    for c in T['cells']:
        print('  %-8s %-14s 联合 %5.1f%% (%2d) → 收涨 %5.1f%% (平滑 %.0f%%) 中位 %+.2f%%' % (
            c['open'], c['break'], c['joint'], c['n'], c['up_prob'], c['up_lap'], c['median']))
    print('  边际收涨 %.1f%% | 四格加权 %.1f%% | %s' % (
        T['marginal_up'], T['weighted_up'], '自洽 ✓' if T['consistent'] else '**不自洽**'))
    cd = T['cond']
    print('  条件：高开 %.0f%% | 破线 %.0f%% | 不破时收涨 %.0f%% | 破线时收涨 %.0f%%' % (
        cd['high_open'], cd['break_split'], cd['up_if_no_break'], cd['up_if_break']))
    print()
    print('=== EV（费用 %.0f bp） ===' % args['cost'])
    E = ev(D, args['cost'])
    for lab, nm in (('median', '中位数(决策用)'), ('trimmed_mean', '剔除±5%均值'), ('mean', '全样本均值')):
        if lab in E['by']:
            x = E['by'][lab]
            print('  %-14s 毛 %+0.2f%%  净 %+0.2f%%  (N=%d)' % (nm, x['gross'], x['net'], x['n']))
    print('  结论：%s' % E['verdict'])
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
