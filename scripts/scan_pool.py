# -*- coding: utf-8 -*-
"""
scan_pool.py — 全市场候选池扫描（三种口径）

用法：
    python scan_pool.py strong    # 强势口径（涨停/准涨停，T+1 打板接力）
    python scan_pool.py trend     # 趋势口径（排除涨停，走势稳定向上）
    python scan_pool.py potential # 潜力口径（未涨停+量能+资金，次日补涨）
    python scan_pool.py strong --cash 6600 --maxpx 15
输出：标准输出表格 + pool.json
"""
import sys, json, time
from ds import (snapshot, clist, kline, indicators, allowed, n100, MARKET_MAIN, _fl)

CASH = 6600
MAXPX = 15.0


def load_rows(pages=10):
    rows = clist(fid='f3', fs=MARKET_MAIN, pages=pages,
                 fields='f12,f14,f2,f3,f6,f8,f10,f20,f62,f100')
    return [x for x in rows if allowed(str(x.get('f12', ''))) and 'ST' not in str(x.get('f14', ''))
            and '退' not in str(x.get('f14', ''))]


def base(x):
    return {'code': str(x['f12']), 'name': x['f14'], 'price': _fl(x['f2']), 'chg': _fl(x['f3']),
            'amt': _fl(x['f6']) / 1e8, 'hs': _fl(x['f8']), 'lb': _fl(x['f10']),
            'zl': _fl(x['f62']) / 1e8, 'blank': x.get('f100', '')}


def screen(rows, mode):
    out = []
    for x in rows:
        try:
            r = base(x)
        except Exception:
            continue
        if r['price'] <= 0 or r['price'] > MAXPX:
            continue
        if not (r['lb'] >= 1.5 or r['hs'] >= 3.0):
            continue
        if mode == 'strong':
            if r['chg'] < 6.0 or r['zl'] <= 0:
                continue
        elif mode == 'potential':
            if r['chg'] < 2.0 or r['chg'] >= 9.5 or r['zl'] <= 0:
                continue
        elif mode == 'trend':
            if r['chg'] < 1.0 or r['zl'] <= 0:
                continue
        else:
            raise SystemExit('mode: strong | trend | potential')
        r['shares'] = n100(r['price'], CASH)
        out.append(r)
    return out


def trend_check(cands, limit=40):
    """趋势口径：K线体检（多头排列 + 站上MA5 + 近10日低点抬高 + 资金符号稳定）。"""
    keep = []
    for r in cands[:limit]:
        k, src = kline(r['code'], 130)
        if len(k) < 60:
            continue
        c = [y['c'] for y in k]; h = [y['h'] for y in k]; l = [y['l'] for y in k]
        ind = indicators(c, h, l, live=r['price'])
        if not ind['bull']:
            continue
        if r['price'] < ind['ma5']:
            continue
        lows10 = [y['l'] for y in k[-10:]]
        rising = all(lows10[i] <= lows10[i + 1] * 1.02 for i in range(len(lows10) - 1))
        if not rising:
            continue
        r.update({k2: round(v, 3) for k2, v in ind.items() if isinstance(v, float)})
        r['source'] = src
        keep.append(r)
        time.sleep(0.3)
    return keep


def show(cands, title):
    print(f'\n=== {title}  {len(cands)} 只 ===')
    print(f"{'代码':<7}{'名称':<9}{'现价':>7}{'涨幅':>8}{'量比':>6}{'换手%':>7}{'主力亿':>8}{'可买':>6}  行业")
    for r in cands:
        print(f"{r['code']:<7}{r['name']:<9}{r['price']:>7.2f}{r['chg']:>+7.2f}%"
              f"{r['lb']:>6.1f}{r['hs']:>7.1f}{r['zl']:>+8.2f}{r['shares']:>6}  {r['blank']}")


if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'strong'
    for i, a in enumerate(sys.argv):
        if a == '--cash':
            CASH = int(sys.argv[i + 1])
        if a == '--maxpx':
            MAXPX = float(sys.argv[i + 1])
    rows = load_rows()
    print(f'主板候选池(涨幅降序抓取): {len(rows)} 只')
    cands = screen(rows, mode)
    cands.sort(key=lambda x: -x['zl'])
    show(cands, f'{mode} 口径')
    if mode == 'trend':
        keep = trend_check(cands)
        show(keep, '趋势口径 · K线体检通过')
        cands = keep
        print('\n⚠️ 仍须做板块横向对比（铁律1）与相对强度校验：python sector_compare.py <代码>')
    json.dump(cands, open('pool.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('\n已写出 pool.json')
