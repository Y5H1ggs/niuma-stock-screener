# -*- coding: utf-8 -*-
"""
scan_pool.py — 全市场候选池扫描（三种口径）

用法：
    python scan_pool.py strong    # 强势口径（涨停/准涨停，T+1 打板接力）
    python scan_pool.py trend     # 趋势口径（排除涨停，走势稳定向上）
    python scan_pool.py potential # 潜力口径（未涨停+量能+资金，次日补涨）
    python scan_pool.py strong --cash 100000 --maxpx 15   # --cash 省略则取 config.json
输出：标准输出表格 + pool.json
"""
import sys, json, time
import cfg
from ds import (snapshot, clist, kline, indicators, allowed, n100, MARKET_MAIN, _fl, _ok)

CASH = None          # None = 用 config.json 的 cash（个人资金不写死在代码里）
MAXPX = None         # None = 用 config.json 的 max_price（可再被 --maxpx 覆盖）
ALL = False          # False = 启用 P39 可交易性闸门（剔除已封板票）；True = --all 显示全部
LIMIT_UP = 9.5       # 主板涨停 10%，≥9.5% 视为已贴板/封板，散户实际买不进


def load_rows(pages=10):
    rows = clist(fid='f3', fs=MARKET_MAIN, pages=pages,
                 fields='f12,f14,f2,f3,f6,f8,f10,f20,f62,f100')
    out = [x for x in rows if allowed(str(x.get('f12', '')))]
    if cfg.get('exclude_st', True):
        out = [x for x in out
               if 'ST' not in str(x.get('f14', '')).upper() and '退' not in str(x.get('f14', ''))]
    return out


def base(x):
    zl_raw, zl_ok = x.get('f62'), _ok(x.get('f62'))
    chg_raw, chg_ok = x.get('f3'), _ok(x.get('f3'))
    return {'code': str(x['f12']), 'name': x['f14'], 'price': _fl(x['f2']),
            'chg': _fl(chg_raw) if chg_ok else 0.0, 'chg_ok': chg_ok,
            'amt': _fl(x['f6']) / 1e8, 'hs': _fl(x['f8']), 'lb': _fl(x['f10']),
            'zl': _fl(zl_raw) / 1e8 if zl_ok else None, 'zl_ok': zl_ok,
            'blank': x.get('f100', '')}


def screen(rows, mode):
    """口径过滤。⚠️ 资金字段取数失败时**不据此剔除**（P23：把"取数失败"当"资金流出"
    会静默制造利空），而是保留并显式警告。"""
    if mode not in ('strong', 'trend', 'potential'):
        raise SystemExit('mode: strong | trend | potential')
    out, no_zl, no_trade = [], 0, 0
    for x in rows:
        try:
            r = base(x)
        except Exception:
            continue
        if r['price'] <= 0:
            continue
        if MAXPX and r['price'] > MAXPX:       # MAXPX 为 None 时不得直接比较（会 TypeError）
            continue
        if not (r['lb'] >= 1.5 or r['hs'] >= 3.0):
            continue
        if not r['chg_ok']:
            continue                            # 涨幅取不到 → 无法判口径
        if r['zl'] is None:
            no_zl += 1
            r['zl'] = 0.0                       # 占位显示，不参与下面的资金过滤
        elif r['zl'] <= 0:
            continue
        if mode == 'strong':
            if r['chg'] < 6.0:
                continue
            if not ALL and r['chg'] >= LIMIT_UP:    # ★ P39 可交易性闸门
                no_trade += 1
                continue
        if mode == 'potential' and not (2.0 <= r['chg'] < 9.5):
            continue
        if mode == 'trend' and r['chg'] < 1.0:
            continue
        r['shares'] = n100(r['price'], CASH)
        out.append(r)
    if no_zl:
        print(f'⚠️ 有 {no_zl} 只候选的主力净额字段取数失败（东财 f62 返回占位符 "-"），'
              f'已**跳过资金过滤**保留在池中 —— 结果仅供观察，资金面须另行验证。')
    if no_trade:
        print(f'⚠️ {no_trade} 只已涨停/贴板（涨幅 ≥{LIMIT_UP}%）按 P39「可交易性闸门」剔除 ——'
              f'买一队列成交概率极低，留在清单里等于不可执行。用 --all 可强制显示。')
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
        zl = f"{r['zl']:>+8.2f}" if r.get('zl_ok', True) else f"{'—':>8}"
        print(f"{r['code']:<7}{r['name']:<9}{r['price']:>7.2f}{r['chg']:>+7.2f}%"
              f"{r['lb']:>6.1f}{r['hs']:>7.1f}{zl}{r['shares']:>6}  {r['blank']}")


if __name__ == '__main__':
    cfg.require()                      # 首次使用必须先做基础数据录入
    mode = sys.argv[1] if len(sys.argv) > 1 else 'strong'
    CASH = cfg.get('cash')
    MAXPX = cfg.get('max_price')
    for i, a in enumerate(sys.argv):
        if a == '--cash':
            CASH = int(sys.argv[i + 1])
        if a == '--maxpx':
            MAXPX = float(sys.argv[i + 1])
        if a == '--all':
            ALL = True                     # 跳过 P39 闸门，显示含已封板的全部候选
    rows = load_rows()
    print(f'候选池(涨幅降序抓取，已按 config 的板块权限过滤): {len(rows)} 只'
          f'　资金 {CASH:.0f} 元' + (f'　价格上限 {MAXPX:g} 元' if MAXPX else '　不限价'))
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
