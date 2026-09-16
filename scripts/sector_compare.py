# -*- coding: utf-8 -*-
"""
sector_compare.py — 板块内资金接力横向对比（铁律 1 落地）

用法：
    python sector_compare.py 600000            # 分析个股在其各板块中的相对强度与排名
    python sector_compare.py 600000 --top 5    # 只看前 5 个板块

输出（每板块一行）：
    板块名 | 成分数 | 红盘数 | 板块主力(亿) | 板指涨幅 | 涨幅中位数 | 目标票涨幅 | 相对强度 | 排名 | 涨停名单(部分)
"""
import sys
from ds import slist, sector_members, snapshot, _fl

LIMIT_THRESHOLD = 9.7

# 非题材"属性桶"（机构/指数/股性标签），不构成板块共振判断依据，默认过滤
ATTR_KEYWORDS = ('机构重仓', '融资融券', '沪股通', '深股通', '标准普尔', 'MSCI', '富时罗素',
                 '中证500', '上证50', '央视50', '转债标的', '预盈预增', '预亏预减', '股权激励',
                 '举牌', '昨日涨停', '昨日连板', '昨日触板', '昨日首板', '次新股', '百元股',
                 '低价股', '高送转', '基金重仓', '社保重仓', 'QFII', '券商重仓', '保险重仓',
                 '信托重仓', 'AB股', 'AH股', '破净股', '科创板', '创业板综', 'GDR', '专精特新',
                 '小盘股', '大盘股', '中盘股', '微盘股', '中报', '年报', '季报', '首亏', '扭亏',
                 '预增', '预减', '破增发', '参股', '回购', '解禁', '减持', '增持', '员工持股',
                 '社保', '养老金', '深成500', '中证100', '沪深300', '上证380', '创业成份')


def is_attr(name):
    return any(k in name for k in ATTR_KEYWORDS)


def analyze(code, show_top=8):
    snap = snapshot([code]).get(code)
    if not snap:
        print(f'无法获取 {code} 实时快照（停牌或代码错误）')
        return
    tgt_chg = snap['chg']
    print(f"\n目标票: {code} {snap['name']}  现价 {snap['price']}  涨幅 {tgt_chg:+.2f}%")
    print('=' * 108)

    sector = slist(code)
    if not sector:
        print('slist 未返回板块信息')
        return

    results = []
    for s in sector:
        bk, nm = s['bk'], s['name']
        if is_attr(nm):
            continue
        rows = sector_members(bk, pages=8)
        if not rows:
            continue
        chg = [_fl(x.get('f3')) for x in rows if x.get('f3') is not None]
        if not chg:
            continue
        zl = sum(_fl(x.get('f62')) for x in rows) / 1e8
        chg_sorted = sorted(chg, reverse=True)
        med = chg_sorted[len(chg_sorted) // 2]
        codes_sorted = sorted(rows, key=lambda x: -_fl(x.get('f3')))
        rank = next((i + 1 for i, x in enumerate(codes_sorted) if str(x.get('f12')) == code), None)
        ups = [x for x in rows if _fl(x.get('f3')) >= LIMIT_THRESHOLD]
        results.append({
            'bk': bk, 'name': nm, 'n': len(rows), 'up': sum(1 for c in chg if c > 0),
            'zl': zl, 'med': med, 'rank': rank,
            'rel': round(tgt_chg - med, 2),
            'limit': [(str(x.get('f12')), x.get('f14')) for x in ups[:6]],
        })

    # 按板块主力净额降序（资金最猛的板块优先），取前 N
    results.sort(key=lambda r: -r['zl'])
    results = results[:show_top]

    print(f"{'板块':<12}{'成分':>5}{'红盘':>5}{'主力亿':>9}{'中位%':>8}{'个股%':>8}{'相对':>7}{'排名':>7}  涨停")
    for r in results:
        flag = '🔴跑输' if r['rel'] < -0.5 else ('✅领先' if r['rel'] > 0.5 else '持平')
        lim = ' '.join(f'{n}' for _, n in r['limit'])
        print(f"{r['name'][:11]:<12}{r['n']:>5}{r['up']:>5}{r['zl']:>+9.2f}{r['med']:>+8.2f}"
              f"{tgt_chg:>+8.2f}{r['rel']:>+7.2f}{str(r['rank']):>7}  {flag} {lim}")

    print('\n判读：')
    print('  · 相对强度 < -0.5 个百分点 = 资金在该方向选了别的票（铁律1 高危）')
    print('  · 主力亿 < 0 = 板块资金在撤，个股独木难支')
    print('  · 涨停名单 = 资金接力类型（对照目标票是否在其中）')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit('用法: python sector_compare.py <代码> [--top N]')
    code = sys.argv[1]
    top = 8
    for i, a in enumerate(sys.argv):
        if a == '--top':
            top = int(sys.argv[i + 1])
    analyze(code, top)
