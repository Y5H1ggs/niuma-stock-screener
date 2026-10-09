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

    results, failed, srcs, qsrcs = [], [], set(), set()
    import ds as _ds          # 延迟导入：只用它的腾讯快照回退，避免改动本文件的 import 行
    for s in sector:
        bk, nm = s['bk'], s['name']
        if s.get('_src'):
            srcs.add(s['_src'])
        if is_attr(nm):
            continue
        rows = sector_members(bk, pages=8)
        if not rows:
            # ★ P83：取数失败的板块**必须报出来**，不能静默丢 ——
            #   原实现 `continue` 之后，"这个板块不存在"和"这个板块取数失败"
            #   在报告里长得一模一样（实测：个别票整张表空白却毫无提示）。
            failed.append('%s(%s)' % (nm, bk))
            continue
        # ★ P83 补：降级源 `board_members_dc` 只给「代码+名称」、**不带行情**，
        #   直接过滤 f3 会让整张表空白（实测限流期 17/17 板块全空）。
        #   此处用**腾讯快照**把涨跌幅补回来，并把行情来源如实记进 qsrcs ——
        #   腾讯行情 ≠ 东财行情，口径必须可见（不得把腾讯数据冒充成东财口径）。
        _miss = [x for x in rows if x.get('f3') is None and x.get('f12')]
        if _miss:
            _fills = _ds._snap_chg([str(x['f12']) for x in _miss])
            for x in _miss:
                _f = _fills.get(str(x['f12']))
                if _f and _f.get('chg') is not None:
                    x['f3'] = _f['chg']
            if any(x.get('f3') is not None for x in _miss):
                qsrcs.add('qq')
        if not any(x.get('f3') is not None for x in rows):
            qsrcs.add('em')
        chg = [_fl(x.get('f3')) for x in rows if x.get('f3') is not None]
        if not chg:
            failed.append('%s(%s)行情整块取不到' % (nm, bk))
            continue
        # ★ 主力净额：无 f62 时置 **None**，不得用 0 冒充 ——
        #   `_fl(None)` 会变 0.0，于是限流期整列印成 "+0.00 亿"，
        #   读起来像"资金平衡"，实际是"这个字段根本没取到"（P81 同族）。
        _zlv = [_fl(x.get('f62')) for x in rows if _ds._ok(x.get('f62'))]
        zl = (sum(_zlv) / 1e8) if _zlv else None
        chg_sorted = sorted(chg, reverse=True)
        med = chg_sorted[len(chg_sorted) // 2]
        codes_sorted = sorted(rows, key=lambda x: -_fl(x.get('f3')))
        rank = next((i + 1 for i, x in enumerate(codes_sorted) if str(x.get('f12')) == code), None)
        ups = [x for x in rows if _fl(x.get('f3')) >= LIMIT_THRESHOLD]
        results.append({
            'bk': bk, 'name': nm, 'n': len(rows), 'up': sum(1 for c in chg if c > 0),
            'zl': zl, 'med': med, 'rank': rank,
            'rel': round(tgt_chg - med, 2),
            # 成分数正好落在整页边界 → **疑似分页截断**（P79：截断被伪装成"就这么多"）
            'trunc': (len(rows) >= 100 and len(rows) % 100 == 0),
            'limit': [(str(x.get('f12')), x.get('f14')) for x in ups[:6]],
        })

    # 有主力数据的按资金降序排前；无数据的排后面（**不拿 0 参与排序**）
    results.sort(key=lambda r: -(r['zl'] if r['zl'] is not None else -1e9))
    results = results[:show_top]

    print(f"{'板块':<12}{'成分':>5}{'红盘':>5}{'主力亿':>9}{'中位%':>8}{'个股%':>8}{'相对':>7}{'排名':>7}  涨停")
    for r in results:
        flag = '🔴跑输' if r['rel'] < -0.5 else ('✅领先' if r['rel'] > 0.5 else '持平')
        lim = ' '.join(f'{n}' for _, n in r['limit'])
        zls = ('%+.2f' % r['zl']) if r['zl'] is not None else '—'
        nstr = ('%d~' % r['n']) if r['trunc'] else str(r['n'])
        print(f"{r['name'][:11]:<12}{nstr:>5}{r['up']:>5}{zls:>9}{r['med']:>+8.2f}"
              f"{tgt_chg:>+8.2f}{r['rel']:>+7.2f}{str(r['rank']):>7}  {flag} {lim}")

    print('\n口径与完整性：')
    print('  · 板块归属来源 _src：%s' % ('、'.join(sorted(srcs)) or '未标注'))
    print('  · 成分行情来源：%s%s' % ('、'.join(sorted(qsrcs)) or '未标注',
          '（em=东财 clist 自带；qq=腾讯快照回退，**无 f62** → 主力列显示「—」而不是 0）'
          if 'qq' in qsrcs else ''))
    print('  · 主力亿 = Σ成分 f62（**与"板块级口径"不可交叉比较**，同名 ≠ 同物）；'
          '「—」= 该字段未取到，**不等于 0**')
    print('  · 成分数带「~」= 正好落在整页边界，**疑似分页截断**（排名/中位数按截断样本算，偏乐观）')
    if failed:
        print('  · ⚠️ %d 个板块**取数失败被跳过**（不是"该股不属于它"）：%s'
              % (len(failed), '、'.join(failed[:10])))
        print('    缺失会让"相对强度/涨停名单"这一栏偏乐观，**不得据此断定"没有别的板块"**。')
    else:
        print('  · 全部 %d 个板块取数成功，无跳过。' % len(results))

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
