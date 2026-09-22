# -*- coding: utf-8 -*-
"""pit.py — PIT（Point-in-Time）时点回测：消除财务因子的前视偏差

用法：
    python pit.py 601169              # 单票：公告日历 + 每个时点"当时能看到什么"
    python pit.py --demo              # 前视偏差量化实验（PIT vs 非 PIT 的收益落差）
    python pit.py --demo --universe 40 --horizon 5

背景（README 路线图 P0-1）：
  没有 PIT，任何带财务因子的回测都会**高估收益**——因为回测在 7 月就用上了 8 月才公告的中报。
  这是 Gap（纸面收益 vs 真实收益）的第一大来源。

本脚本做两件事：
  ① 提供/验证 PIT 取值（`ds.fundamentals_at`）：某日只允许看到"公告日 ≤ 该日"的财报；
  ② 用同一套因子、同一批股票，跑 PIT 与非 PIT 两条选股路径，**把偏差量化出来**。
     两条路径的唯一差别就是"当时能不能看到那份财报"——差异即前视偏差。
"""
import sys
import io
import time

import ds

# ⚠️ 不要在模块顶层重绑 sys.stdout（import 时会关闭调用方的 stdout），重绑放 __main__。
from ds import (kline_qq, clist, allowed, MARKET_MAIN, _fl, _ok,
                financials, fundamentals_at, announce_calendar)


# ------------------------------------------------------------------ 单票视图
def show_one(code):
    print('=' * 100)
    print('【%s 公告日历 · PIT 视角】' % code)
    print('=' * 100)
    fin = financials(code)
    if not fin:
        print('  财报取数失败')
        return
    cal = announce_calendar(code, fin)
    print('  %-12s %-12s %10s %8s %10s %10s %9s' % (
        '报告期', '公告日', 'EPS', 'ROE%', '营收(亿)', '净利(亿)', '毛利率%'))
    for f in sorted(fin, key=lambda x: x['report'], reverse=True)[:10]:
        print('  %-12s %-12s %10.4f %8.2f %10.2f %10.4f %9.2f' % (
            f['report'], f['notice'] or '(缺)', f['eps'], f['roe'],
            f['revenue'] / 1e8, f['netprofit'] / 1e8, f['gross_margin']))

    print('\n  --- 时点对照（"那天我到底能看到什么"）---')
    probes = []
    for i in range(len(cal) - 1):
        # 每个公告日前 1 天 / 当天 / 后 1 天
        probes.append(cal[i][0])
    for d in sorted(set(probes))[-6:]:
        import datetime as _dt
        dd = _dt.date.fromisoformat(d)
        for tag, day in (('公告前1日', (dd - _dt.timedelta(days=1)).isoformat()),
                         ('公告当日  ', d)):
            f = fundamentals_at(code, day, fin)
            gap = ''
            # 若"公告前"能看到与"公告后"相同的最新报告期 → 说明有前视偏差风险
            print('    %s %s → 可见最新报告期 %s (公告 %s)  ROE %.2f' % (
                day, tag, f['report'] if f else '无', (f or {}).get('notice') or '-',
                (f or {}).get('roe', 0)))


# ------------------------------------------------------------------ 偏差实验
def demo(universe=30, horizon=5, days=250):
    """PIT vs 非 PIT 选股对比。

    因子：净利润同比增速（np_yoy）—— 对财报时点最敏感，前视偏差最严重。
    方法：在每个信号日，按因子取前 30% 分位建组，看之后 horizon 日的平均收益。
          PIT 组用「公告日 ≤ 信号日」的财报；非 PIT 组用「当前能看到的最新财报」。
    """
    print('=' * 100)
    print('【前视偏差量化实验】PIT vs 非 PIT —— 因子：净利润同比增速(np_yoy)')
    print('=' * 100)
    print('  股票池构建中（主板 / 成交额靠前 / 剔除 ST）…')
    rows = clist(fid='f6', fs=MARKET_MAIN, pages=4, fields='f12,f14,f3,f6')
    cand = []
    for x in rows:
        c = str(x.get('f12', ''))
        nm = str(x.get('f14', ''))
        if not allowed(c) or 'ST' in nm.upper() or '退' in nm:
            continue
        if not _ok(x.get('f6')):
            continue
        cand.append((c, nm, _fl(x.get('f6'))))
    cand.sort(key=lambda t: -t[2])
    cand = cand[:universe]
    print('  股票池 %d 只\n' % len(cand))

    data = []
    for i, (c, nm, amt) in enumerate(cand):
        try:
            k = kline_qq(c, 400)
            fin = financials(c)
            if not k or not fin or len(k) < 120:
                continue
            data.append({'code': c, 'name': nm, 'k': k, 'fin': fin,
                         'kmap': {r['d']: j for j, r in enumerate(k)}})
        except Exception:
            pass
        if (i + 1) % 10 == 0:
            print('    已载入 %d/%d' % (i + 1, len(cand)))
        time.sleep(0.25)

    print('  有效样本 %d 只\n' % len(data))
    if len(data) < 5:
        print('  样本不足，退出')
        return

    # 统一信号日：所有票都有 K 线的公共日期区间
    all_dates = sorted(set.intersection(*[set(d['kmap'].keys()) for d in data]))
    all_dates = all_dates[-days:]
    print('  信号日区间 %s ~ %s（%d 个）\n' % (all_dates[0], all_dates[-1], len(all_dates)))

    res = {'pit': [], 'nopit': []}
    for date in all_dates:
        pit_rows, nopit_rows = [], []
        for d in data:
            j = d['kmap'].get(date)
            if j is None or j + horizon >= len(d['k']):
                continue
            f_pit = fundamentals_at(d['code'], date, d['fin'])
            f_latest = max(d['fin'], key=lambda x: x['report'])
            fwd = (d['k'][j + horizon]['c'] / d['k'][j]['c'] - 1) * 100
            if f_pit is not None:
                pit_rows.append((f_pit['np_yoy'], fwd))
            nopit_rows.append((f_latest['np_yoy'], fwd))
        # 取因子前 30% 分位
        for tag, rows_ in (('pit', pit_rows), ('nopit', nopit_rows)):
            if len(rows_) < 6:
                continue
            rows_.sort(key=lambda t: -t[0])
            top = rows_[:max(2, len(rows_) // 3)]
            res[tag].append(sum(t[1] for t in top) / len(top))

    print('  ' + '-' * 96)
    for tag, label in (('nopit', '非 PIT（用"当前最新财报"回填，即典型错误做法）'),
                       ('pit', 'PIT（只用"公告日 ≤ 信号日"的财报）')):
        v = res[tag]
        if not v:
            print('  %-48s 无样本' % label)
            continue
        avg = sum(v) / len(v)
        win = sum(1 for x in v if x > 0) / len(v) * 100
        print('  %-48s 信号数 %4d | 平均 %+6.3f%% | 胜率 %5.1f%%' % (label, len(v), avg, win))
    if res['pit'] and res['nopit']:
        a, b = sum(res['nopit']) / len(res['nopit']), sum(res['pit']) / len(res['pit'])
        print('  ' + '-' * 96)
        print('  → **前视偏差 = %+.3f 个百分点/次**（非 PIT %+.3f%% vs PIT %+.3f%%）' % (a - b, a, b))
        if a - b > 0:
            print('  → 方向符合理论预期：把"今天才看到的财报"回填进历史，会给回测**凭空抬高** %.3f%%/次。'
                  % (a - b))
        else:
            print('  → ⚠️ 本次为**负偏差**，与理论方向相反。**不能据此认为"不存在前视偏差"**：')
            print('     理论上前视偏差只会**抬高**非 PIT 收益；出现负值通常意味着')
            print('     ① 样本期过短（本次 %d 个信号日）② 该因子在观测窗口内恰好失效' % len(all_dates))
            print('     ③ 财报可见性差异未落在关键披露窗口。→ **扩大样本（--days）重跑后再下结论。**')
            print('  → 但口径结论不变：只要将来要用财务因子，**就必须走 PIT** ——')
            print('     用非 PIT 口径跑赢回测，等于用一个当时不存在的世界做决策。')
    print('\n  ⚠️ 口径说明：')
    print('   · 非 PIT 组的做法（用最新财报回填历史）是**业界最常见的隐性错误**，很多人无意中在用；')
    print('   · 本实验只对比"同因子、同池、同日期"的两条路径，差异唯一来源就是财报可见性；')
    print('   · 未含交易成本；horizon=%d 日；样本 %d 只票 × %d 个信号日。' % (horizon, len(data), len(all_dates)))


if __name__ == '__main__':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    if '--demo' in sys.argv:
        u, h, dd = 30, 5, 250
        for i, a in enumerate(sys.argv):
            if a == '--universe':
                u = int(sys.argv[i + 1])
            if a == '--horizon':
                h = int(sys.argv[i + 1])
            if a == '--days':
                dd = int(sys.argv[i + 1])
        demo(universe=u, horizon=h, days=dd)
    else:
        args = [a for a in sys.argv[1:] if not a.startswith('--')]
        if not args:
            raise SystemExit('用法: python pit.py <代码>   |   python pit.py --demo [--universe N] [--horizon D]')
        show_one(args[0])
