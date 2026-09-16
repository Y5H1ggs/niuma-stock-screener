# -*- coding: utf-8 -*-
"""
backtest.py — T+1 隔日模式形态回测

用法：
    python backtest.py 002093            # 回测该股全部内置形态
    python backtest.py 002093 --upper 5  # 额外输出"最接近今日参数"的历史案例

输出每个形态：N | 次日收盘卖胜率 | 均值 | 次日开盘卖胜率 | 次日最高均值 | 次日最低均值 | 破-3%概率
⚠️ 样本量 <10 一律标注"不可外推"。
"""
import sys
from ds import kline_qq, _fl

CUT = -0.04          # 亏损截断口径
BREAK = -0.03        # 破位阈值


def feats(k, i):
    """第 i 根（信号日）的特征。"""
    prev = k[i - 1]['c']
    c, o, h, l, v = k[i]['c'], k[i]['o'], k[i]['h'], k[i]['l'], k[i]['v']
    lim = round(prev * 1.1, 2)
    vols = [k[j]['v'] for j in range(max(0, i - 20), i)]
    avgv = sum(vols) / len(vols) if vols else 0
    cl = [k[j]['c'] for j in range(0, i + 1)]
    ma = lambda n: sum(cl[-n:]) / n if len(cl) >= n else None
    ma5, ma10, ma20, ma60 = ma(5), ma(10), ma(20), ma(60)
    f = {
        'chg': (c / prev - 1) * 100,
        'touch': h >= lim - 0.011,
        'seal': c >= lim - 0.001,
        'upper': (h - c) / c * 100,
        'vr': (v / avgv) if avgv else 0,
        'new_high60': c >= max(k[j]['h'] for j in range(max(0, i - 59), i + 1)),
        'bull': bool(ma5 and ma10 and ma20 and ma60 and ma5 > ma10 > ma20 > ma60),
        'lowopen': o < prev * 0.98,
        'yang': c > o,
        'chg3': (c / k[i - 3]['c'] - 1) * 100 if i >= 3 else 0,
    }
    # 前5日内是否有涨停
    f['lim5'] = any(k[j]['c'] >= round(k[j - 1]['c'] * 1.1, 2) - 0.001 for j in range(max(1, i - 4), i + 1))
    return f


def backtest(k, rule, name, base=False):
    res = []
    for i in range(60, len(k) - 1):
        try:
            f = feats(k, i)
        except Exception:
            continue
        if base or rule(f):
            buy = k[i]['c']
            nx = k[i + 1]
            r_open = (nx['o'] / buy - 1) * 100
            r_close = (nx['c'] / buy - 1) * 100
            r_high = (nx['h'] / buy - 1) * 100
            r_low = (nx['l'] / buy - 1) * 100
            res.append({'d': nx['d'], 'open': r_open, 'close': r_close,
                        'high': r_high, 'low': r_low, 'f': f})
    if not res:
        return None
    n = len(res)
    win_c = sum(1 for r in res if r['close'] > 0) / n * 100
    win_o = sum(1 for r in res if r['open'] > 0) / n * 100
    avg_c = sum(r['close'] for r in res) / n
    avg_o = sum(r['open'] for r in res) / n
    avg_h = sum(r['high'] for r in res) / n
    avg_l = sum(r['low'] for r in res) / n
    p_break = sum(1 for r in res if r['low'] <= BREAK * 100) / n * 100
    p_2 = sum(1 for r in res if r['high'] >= 2.0) / n * 100
    return {'name': name, 'n': n, 'win_close': win_c, 'avg_close': avg_c,
            'win_open': win_o, 'avg_open': avg_o, 'avg_high': avg_h, 'avg_low': avg_l,
            'p_break': p_break, 'p_2': p_2, 'cases': res}


RULES = [
    ('触板未封(炸板)', lambda f: f['touch'] and not f['seal']),
    ('涨停封板', lambda f: f['seal']),
    ('首板', lambda f: f['seal'] and not f['lim5']),
    ('涨>3%但未触板', lambda f: 3 <= f['chg'] < 9.5 and not f['touch']),
    ('多头排列+涨>=3%', lambda f: f['bull'] and f['chg'] >= 3),
    ('多头+放量>2x', lambda f: f['bull'] and f['vr'] >= 2),
    ('近3日累计涨>8%(过热)', lambda f: f['chg3'] > 8),
    ('创60日新高', lambda f: f['new_high60']),
    ('创60日新高+上影>3%', lambda f: f['new_high60'] and f['upper'] > 3),
    ('低开>2%后收阳', lambda f: f['lowopen'] and f['yang']),
    ('前5日有涨停+今日收阳', lambda f: f['lim5'] and f['yang']),
    ('上影>=5%且收阳', lambda f: f['upper'] >= 5 and f['yang']),
]


def fmt(r):
    warn = ' ⚠️样本不足·不可外推' if r['n'] < 10 else ''
    return (f"{r['name']:<22}N={r['n']:<4} 收胜{r['win_close']:>5.0f}% 均{r['avg_close']:>+6.2f}% | "
            f"开胜{r['win_open']:>5.0f}% 开均{r['avg_open']:>+6.2f}% | "
            f"高{r['avg_high']:>+5.2f}% 低{r['avg_low']:>+6.2f}% | 冲2%={r['p_2']:>3.0f}% 破-3%={r['p_break']:>3.0f}%{warn}")


def main(code):
    k = kline_qq(code, 800)
    if len(k) < 120:
        raise SystemExit(f'{code} K线数据不足({len(k)}根)')
    print(f'\n=== {code} 形态回测（腾讯前复权 {len(k)} 根，{k[0]["d"]} ~ {k[-1]["d"]}） ===')
    print('口径：信号日收盘买入，次日卖出；单位 %；不含费用（双边约0.15~0.2%）\n')
    hits = []
    for nm, rule in RULES:
        r = backtest(k, rule, nm)
        if r:
            print(fmt(r))
            hits.append(r)
    b = backtest(k, None, '基准(任意日)', base=True)
    if b:
        print('-' * 130)
        print(fmt(b))

    # 最接近今日的历史案例
    print('\n--- 最近 5 个"触板未封"案例明细 ---')
    tb = next((h for h in hits if h['name'].startswith('触板未封')), None)
    if tb:
        for c in tb['cases'][-5:]:
            f = c['f']
            print(f"  次日{c['d']}  当日涨{f['chg']:+.2f}% 上影{f['upper']:.1f}% 量比{f['vr']:.2f}x "
                  f"→ 开{c['open']:+.2f}% 收{c['close']:+.2f}% 高{c['high']:+.2f}% 低{c['low']:+.2f}%")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit('用法: python backtest.py <代码>')
    main(sys.argv[1])
