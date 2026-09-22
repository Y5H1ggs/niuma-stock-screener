# -*- coding: utf-8 -*-
"""predict.py — 次日涨跌概率预测（形态邻域回测法）

用法：
    python predict.py 601169              # 自动用当前盘中/收盘数据算指标 → 找历史邻域 → 输出概率
    python predict.py 601169 --cost 10.00 # 附带"次日达到保本价"的概率

方法（不猜、不拟合，只用历史同形态样本）：
  1. 算当日形态指标：RSI14 / BOLL %B / CCI20 / 250日位置 / 均线结构 / 近5日涨幅；
  2. 在历史 K 线里找**参数邻域**（各指标落在今日值附近的历史信号日）；
  3. 统计这些信号日**次日**的开/收/最高/最低分布 → 转成概率；
  4. 邻域样本 <10 时自动逐级放宽，并在输出中标注实际转速与样本量。

⚠️ 三条铁律：
  · 样本 <10 → 输出「不可外推」，不得当作结论；
  · 概率是**基准分布**，不含板块/资金/情绪修正——必须与 board_flow/fflow/mood 一并判读；
  · 邻域参数是**固定规则**（±带宽），不允许为了得到好看的数字而逐个试带宽。
"""
import sys
import io

import ds
from ds import kline_qq

# ⚠️ 不要在模块顶层重绑 sys.stdout（import 时会关闭调用方的 stdout），重绑放 __main__。


def today_feats(code):
    """当日形态指标（live 价拼入序列）。返回 (feat_dict, snapshot)。"""
    s = ds.snapshot([code]).get(code)
    if not s:
        raise SystemExit('快照失败：%s' % code)
    k = kline_qq(code, 800)
    if not k:
        raise SystemExit('K线失败：%s' % code)
    # 末根若已是当日 → 替换为 live；否则追加
    today = str(s.get('time') or '')[:8]
    cl = [r['c'] for r in k]
    hi = [r['h'] for r in k]
    lo = [r['l'] for r in k]
    if len(today) == 8:
        tds = today[:4] + '-' + today[4:6] + '-' + today[6:8]
        if k[-1]['d'] == tds:
            cl[-1] = hi[-1] = lo[-1] = s['price']
        else:
            cl.append(s['price']); hi.append(s['price']); lo.append(s['price'])
    n = len(cl)
    f = {
        'rsi14': ds.rsi(cl, 14),
        'pctb': ds.boll(cl)['pctb'],
        'cci': ds.cci(hi, lo, cl, 20),
        'mom': ds.mom(cl, 10),
        'pos250': ds.pos(hi, lo, s['price'], 250),
        'ma5': ds.ma(cl, 5), 'ma10': ds.ma(cl, 10), 'ma20': ds.ma(cl, 20),
        'above5': s['price'] > ds.ma(cl, 5),
        'bear': ds.ma(cl, 5) < ds.ma(cl, 10) < ds.ma(cl, 20),
        'bull': ds.ma(cl, 5) > ds.ma(cl, 10) > ds.ma(cl, 20),
        'g5': (s['price'] / cl[-6] - 1) * 100 if n > 6 else 0,
        'chg': s['chg'],
    }
    return f, s


def _hist_feats(k, i):
    c = k[i]['c']
    cl = [k[j]['c'] for j in range(0, i + 1)]
    hi = [k[j]['h'] for j in range(0, i + 1)]
    lo = [k[j]['l'] for j in range(0, i + 1)]
    if i < 60:
        return None
    return {
        'rsi14': ds.rsi(cl, 14), 'pctb': ds.boll(cl)['pctb'],
        'cci': ds.cci(hi, lo, cl, 20), 'mom': ds.mom(cl, 10),
        'pos250': ds.pos(hi, lo, c, 250),
        'above5': c > ds.ma(cl, 5),
        'bear': ds.ma(cl, 5) < ds.ma(cl, 10) < ds.ma(cl, 20),
        'bull': ds.ma(cl, 5) > ds.ma(cl, 10) > ds.ma(cl, 20),
        'g5': (c / cl[-6] - 1) * 100 if i > 6 else 0,
        'chg': (c / k[i - 1]['c'] - 1) * 100,
        'd': k[i]['d'], 'c': c,
    }


def neighbourhood(k, t, rsi_bw=8, pctb_bw=15, cci_bw=80, require_ma=True):
    """固定规则的邻域筛选：各指标落在今日值 ± 带宽内，且均线结构一致。"""
    res = []
    for i in range(60, len(k) - 1):
        f = _hist_feats(k, i)
        if not f:
            continue
        if abs(f['rsi14'] - t['rsi14']) > rsi_bw:
            continue
        if abs(f['pctb'] - t['pctb']) > pctb_bw:
            continue
        if abs(f['cci'] - t['cci']) > cci_bw:
            continue
        if require_ma and (f['above5'] != t['above5'] or f['bear'] != t['bear']):
            continue
        buy = k[i]['c']
        nx = k[i + 1]
        res.append(((nx['o'] / buy - 1) * 100, (nx['c'] / buy - 1) * 100,
                    (nx['h'] / buy - 1) * 100, (nx['l'] / buy - 1) * 100, nx['d'], f))
    return res


def stats(res, s, cost=None, shares=100):
    n = len(res)
    p = lambda f: sum(1 for r in res if f(r)) / n * 100
    out = {
        'n': n,
        # ★ 样本充足性标记（v1.2.5 新增）：n<10 时概率**不可外推**。
        #   实测：某票邻域只剩 N=2，却照样输出"P(收盘涨)=0%"——2 个样本的 0%
        #   会被读成"必跌"，是典型的**用噪声冒充结论**。调用方必须据此降级展示。
        'reliable': n >= 10,
        'P_open_up': p(lambda r: r[0] > 0),
        'P_close_up': p(lambda r: r[1] > 0),
        'P_high_ge1': p(lambda r: r[2] >= 1),
        'P_high_ge2': p(lambda r: r[2] >= 2),
        'P_high_ge3': p(lambda r: r[2] >= 3),
        'P_low_le_1': p(lambda r: r[3] <= -1),
        'P_low_le_3': p(lambda r: r[3] <= -3),
        'avg_open': sum(r[0] for r in res) / n,
        'avg_close': sum(r[1] for r in res) / n,
        'avg_high': sum(r[2] for r in res) / n,
        'avg_low': sum(r[3] for r in res) / n,
    }
    import statistics as st
    out['med_close'] = st.median([r[1] for r in res])
    out['med_high'] = st.median([r[2] for r in res])
    if cost:
        # 次日盘中最高价能否覆盖"成本 + 双边费用"
        ph = (s['price'] * 1.0)
        need = None
        try:
            import cfg
            need = cfg.break_even(cost, shares) / s['price'] - 1
        except Exception:
            pass
        if need is not None:
            out['P_touch_breakeven'] = p(lambda r: r[2] / 100 >= need)
            out['need_pct'] = need * 100
    return out


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if not args:
        raise SystemExit('用法: python predict.py <代码> [--cost 成本价]')
    code = args[0]
    cost = None
    shares = 100          # ⚠️ 必须传实际股数：最低佣金(5元/边)摊薄程度随股数变化，
    for i, a in enumerate(sys.argv):   # 用 100 股算会高估保本价、低估"触及保本"概率
        if a == '--cost':
            cost = float(sys.argv[i + 1])
        if a == '--shares':
            shares = int(sys.argv[i + 1])

    t, s = today_feats(code)
    k = kline_qq(code, 800)
    print('=' * 104)
    print('【%s 次日涨跌概率预测 · 形态邻域回测法】' % code)
    print('=' * 104)
    print('  现价 %.2f (%+.2f%%)  样本区间 %s ~ %s (%d 根)' % (
        s['price'], s['chg'], k[0]['d'], k[-1]['d'], len(k)))
    print('  今日形态: RSI14 %.1f | BOLL %%B %.1f | CCI20 %.1f | MOM10 %+.1f%% | pos250 %.1f%%' % (
        t['rsi14'], t['pctb'], t['cci'], t['mom'], t['pos250']))
    print('  均线结构: %s | 站上MA5: %s | 近5日 %+.2f%%' % (
        '空头排列' if t['bear'] else ('多头排列' if t['bull'] else '纠缠'),
        t['above5'], t['g5']))

    # 分级邻域：固定收缩序列，不允许逐个试
    tiers = [
        ('A 严格', dict(rsi_bw=6, pctb_bw=10, cci_bw=60)),
        ('B 标准', dict(rsi_bw=8, pctb_bw=15, cci_bw=80)),
        ('C 放宽', dict(rsi_bw=12, pctb_bw=25, cci_bw=120)),
        ('D 宽泛', dict(rsi_bw=18, pctb_bw=40, cci_bw=180)),
    ]
    chosen = None
    print('\n  邻域等级 → 样本量：')
    for nm, kw in tiers:
        res = neighbourhood(k, t, **kw)
        print('    %-6s %s  N=%d' % (nm, kw, len(res)))
        if chosen is None and len(res) >= 15:
            chosen = (nm, kw, res)
    if chosen is None:
        res = neighbourhood(k, t, rsi_bw=18, pctb_bw=40, cci_bw=180)
        chosen = ('D 宽泛(样本仍不足)', {}, res)
    nm, kw, res = chosen

    if len(res) < 10:
        print('\n  ⚠️ 邻域样本 N=%d <10 → **不可外推**，以下数字仅供参照。' % len(res))
    st = stats(res, s, cost, shares)
    print('\n' + '=' * 104)
    print('【采用邻域 %s · N=%d%s】次日概率分布' % (
        nm, st['n'], '' if st.get('reliable', True) else '  ⚠️ 样本不足(需≥10)·概率不可外推'))
    print('=' * 104)
    print('  P(开盘上涨)          %5.0f%%      平均开盘 %+.2f%%' % (st['P_open_up'], st['avg_open']))
    print('  P(收盘上涨)          %5.0f%%      平均收盘 %+.2f%%   中位 %+.2f%%' % (
        st['P_close_up'], st['avg_close'], st['med_close']))
    print('  P(盘中曾涨 ≥1%%)      %5.0f%%      平均最高 %+.2f%%   中位 %+.2f%%' % (
        st['P_high_ge1'], st['avg_high'], st['med_high']))
    print('  P(盘中曾涨 ≥2%%)      %5.0f%%' % st['P_high_ge2'])
    print('  P(盘中曾涨 ≥3%%)      %5.0f%%' % st['P_high_ge3'])
    print('  P(盘中曾跌 ≤-1%%)     %5.0f%%      平均最低 %+.2f%%' % (st['P_low_le_1'], st['avg_low']))
    print('  P(盘中跌破 -3%%)      %5.0f%%' % st['P_low_le_3'])
    if 'P_touch_breakeven' in st:
        print('  P(次日盘中触及保本价 %.2f%%) %5.0f%%   (成本 %.3f)' % (
            st['need_pct'], st['P_touch_breakeven'], cost))
    print('\n  最近 10 个邻域案例：')
    for o, c2, h, l2, d, f in res[-10:]:
        print('    %s 涨%+6.2f%% RSI%5.1f %%B%5.1f → 次日 开%+6.2f%% 收%+6.2f%% 高%+6.2f%% 低%+6.2f%%'
              % (d, f['chg'], f['rsi14'], f['pctb'], o, c2, h, l2))
    print('\n  ⚠️ 以上为**基准分布**，未含板块资金/情绪修正。必须与 fflow(资金) / board_index(板块) / mood(情绪) 一并判读。')
    print('  ⚠️ 样本量 %d，区间含多段不同市况，换环境适用性下降；不含费用（双边约 0.15~0.2%%）。' % st['n'])


if __name__ == '__main__':
    main()
