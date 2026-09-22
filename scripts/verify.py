# -*- coding: utf-8 -*-
"""verify.py — 多接口交叉校验（README 路线图 P1-5）

把「单源 + 降级链」升级为「双源 + 冲突标红」：
  降级链解决的是"取不到怎么办"，交叉校验解决的是"**取到了但可能是错的**怎么办"。
  盘中实测过多次：某源返回占位符被 `_fl()` 读成 0，看起来完全正常，但数字是假的。

校验三组：
  ① 快照：腾讯 qt.gtimg.cn  vs  东财 push2 stock/get（现价/开高低/昨收/量额）
  ② K线：东财 push2his  vs  新浪 getKLineData  vs  腾讯 kline（末几根收盘）
  ③ 板块资金：board_index 的板块主力净额  vs  sector_members 成分 f62 求和

用法：
  python verify.py 601169                 # 单票全量校验
  python verify.py 601169 600519 601169   # 多票
  python verify.py --kline 601169         # 只校验 K 线
  python verify.py --board 数据中心        # 只校验某个板块的资金口径
"""
import sys
import io
import json

import ds

TOL_PX = 0.005     # 价格相对容差 0.5%
TOL_VOL = 0.03     # 成交量相对容差 3%
TOL_AMT = 0.05     # 成交额相对容差 5%


def _em_snapshot(code):
    """东财个股快照（fltt=2 时 f43 等为小数）。"""
    url = ('https://push2.eastmoney.com/api/qt/stock/get?secid=%s'
           '&fields=f43,f44,f45,f46,f47,f48,f60,f58&fltt=2&invt=2&ut=%s'
           % (ds.secid(code), ds.UT))
    r = ds.get(url)
    try:
        d = json.loads(r).get('data') or {}
        return {
            'name': d.get('f58'),
            'price': ds._fl(d.get('f43')), 'high': ds._fl(d.get('f44')),
            'low': ds._fl(d.get('f45')), 'open': ds._fl(d.get('f46')),
            'vol': ds._fl(d.get('f47')), 'amount': ds._fl(d.get('f48')),
            'prev': ds._fl(d.get('f60')),
        }
    except Exception:
        return None


def _row(label, a, b, tol, unit=''):
    if not b:
        return (label, a, b, None, '数据缺失')
    dev = abs(a - b) / max(abs(b), 1e-9)
    return (label, a, b, dev, '✅ 一致' if dev <= tol else '⚠️ **冲突**')


def check_snapshot(code):
    t = ds.snapshot([code]).get(code)
    e = _em_snapshot(code)
    print('  【快照双源】腾讯 vs 东财  %s %s' % (code, (t or {}).get('name') or (e or {}).get('name') or ''))
    if not t or not e:
        print('    ⚠️ 有一侧取数失败（腾讯%s / 东财%s），无法交叉校验' % (
            'OK' if t else 'FAIL', 'OK' if e else 'FAIL'))
        return
    rows = [
        _row('现价', t['price'], e['price'], TOL_PX),
        _row('今开', t['open'], e['open'], TOL_PX),
        _row('最高', t['high'], e['high'], TOL_PX),
        _row('最低', t['low'], e['low'], TOL_PX),
        _row('昨收', t['prev'], e['prev'], TOL_PX),
        _row('成交量(手)', t['vol'], e['vol'], TOL_VOL),
        # ⚠️ 单位不同：腾讯 snapshot 的 amount 是**万元**，东财 f48 是**元** → 必须归一后再比，
        #    否则会报 99.99% 的假冲突（实测 12215 万 vs 122145773 元 其实是同一个数）。
        _row('成交额(万元)', t['amount'], e['amount'] / 1e4, TOL_AMT),
    ]
    print('    %-12s %14s %14s %10s  %s' % ('项目', '腾讯', '东财', '偏差', '判定'))
    bad = 0
    for label, a, b, dev, verdict in rows:
        dv = '—' if dev is None else ('%.3f%%' % (dev * 100))
        print('    %-12s %14s %14s %10s  %s' % (label, ('%.4f' % a) if a else '—',
                                                 ('%.4f' % b) if b else '—', dv, verdict))
        if verdict.startswith('⚠️'):
            bad += 1
    print('    → %s' % ('全部一致' if bad == 0 else '**%d 项冲突，该数据不可信，须以第三源复核**' % bad))


def check_kline(code):
    res = {}
    for src, fn in (('东财', ds.kline_em), ('新浪', ds.kline_sina), ('腾讯', ds.kline_qq)):
        try:
            k = fn(code, 30) if src != '腾讯' else ds.kline_qq(code, 30)
            res[src] = k or []
        except Exception:
            res[src] = []
    print('  【K线三源】%s  条数：%s' % (code, ' / '.join('%s=%d' % (k, len(v)) for k, v in res.items())))
    tags = [k for k, v in res.items() if v]
    if len(tags) < 2:
        print('    ⚠️ 可用源不足 2 个，无法交叉校验')
        return
    base = tags[0]
    for other in tags[1:]:
        ok = conflict = skipped = 0
        for i in range(1, 4):
            try:
                a, b = res[base][-i], res[other][-i]
                if a['d'] != b['d']:
                    skipped += 1
                    continue
                dev = abs(a['c'] - b['c']) / max(b['c'], 1e-9)
                if dev <= TOL_PX:
                    ok += 1
                else:
                    conflict += 1
                    print('    ⚠️ **冲突** %s vs %s @%s：%.3f vs %.3f（%.2f%%）'
                          % (base, other, a['d'], a['c'], b['c'], dev * 100))
            except Exception:
                continue
        note = '（%d 根日期不对齐 —— 末根口径可能不同，见 P37）' % skipped if skipped else ''
        print('    %s vs %s：一致 %d / 冲突 %d%s' % (base, other, ok, conflict, note))


def check_board(name):
    idx = ds.board_index(refresh=True)
    rec = idx['by_name'].get(name)
    if not rec:
        print('  【板块资金】%s → 无索引' % name)
        return
    bk, kind, zl, chg = rec
    print('  【板块资金口径】%s [%s %s]  board_index 报主力 %.2f 亿' % (name, bk, kind, zl or 0))
    rows = ds.sector_members(bk, pages=6)
    s = sum(ds._fl(x.get('f62')) for x in rows if ds._ok(x.get('f62')))
    n_ok = sum(1 for x in rows if ds._ok(x.get('f62')))
    print('    sector_members 求和：%.2f 亿（%d/%d 只成分有资金字段）' % (s / 1e8, n_ok, len(rows)))
    if not zl:
        print('    ⚠️ board_index 侧无量，无法比对')
        return
    dev = abs(s / 1e8 - zl) / max(abs(zl), 1e-9)
    print('    偏差 %.2f%% → %s' % (dev * 100, '✅ 一致' if dev <= 0.15 else '⚠️ **口径差异较大**'))
    print('    说明：板块资金通常按"全部成分的资金净额"汇总，成交额小的成分贡献有限；')
    print('          偏差 >15%% 多半是成分分页没取全（尤其概念板块 300+ 只）。')


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    only = None
    for flag in ('--kline', '--snapshot', '--board'):
        if flag in sys.argv:
            only = flag
    if only == '--board':
        for nm in args or ['数据中心']:
            check_board(nm)
        return
    if not args:
        raise SystemExit('用法: python verify.py <代码...> [--kline|--snapshot|--board <板块名>]')
    for code in args:
        print('=' * 96)
        if only in (None, '--snapshot'):
            check_snapshot(code)
        if only in (None, '--kline'):
            check_kline(code)
        if only is None:
            prof = ds.stock_profile(code)
            if prof.get('industry'):
                check_board(prof['industry'])
        print()


if __name__ == '__main__':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    main()
