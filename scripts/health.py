# -*- coding: utf-8 -*-
"""health.py — 数据源健康自检（README 路线图 P2-6）

跑一遍全部接口，输出：
  · 各接口可用性 / 耗时 / 返回条数
  · K 线降级链实际命中哪一源
  · 关键字段是否返回占位符（`_fl("-") == 0` 这类**静默失败**）

意义：判断"今天这份结论的置信度"。
  若资金类接口大面积失败，当天报告里的资金维度就只是基准分（满分的 50%），**不能当判断看**。

用法：
  python health.py            # 全量自检
  python health.py --quick    # 只测核心接口
"""
import sys
import io
import time

import ds

CODE = '601169'
MAIN = ['601169', '600519', '600000']


def _t(fn):
    t0 = time.time()
    try:
        r = fn()
        return True, r, (time.time() - t0) * 1000
    except Exception as e:
        return False, repr(e)[:70], (time.time() - t0) * 1000


def _blocks(name, fn, desc, good=None):
    """三态判定，核心原则：**"没抛异常" ≠ "拿到了数据"**（P75）。

    旧实现只用"是否抛异常"判可用，于是下面这些全部被标成 ✅：
      东财 clist → 0 行、ulist → f62=None、fflow → 0 条、
      board_index → 0 个板块、kline_em → 0 根
    结果是 health.py 报"可用 9/9（100%）"，而真相是**东财 push2 族整族被掐断**。
    这直接误导了后续判断（把"整族阻断"当成"偶发抖动"）。

    返回 mark ∈ {'✅' 有可用内容, '⚠️' 请求通但内容为空/不可解析, '❌' 异常或连接被阻断}
    """
    ok, r, ms = _t(fn)
    if not ok:
        return ('❌', name, ms, str(r))
    try:
        detail = desc(r)
    except Exception as e:
        return ('⚠️', name, ms, '解析异常 %s　← 内容不可用' % repr(e)[:40])
    if good is not None:
        try:
            if not good(r):
                return ('⚠️', name, ms, detail + '　← **空返回/降级，不构成可用**')
        except Exception as e:
            return ('⚠️', name, ms, detail + '　← 校验异常 %s' % repr(e)[:30])
    return ('✅', name, ms, detail)


def run(quick=False):
    print('=' * 104)
    print('【数据源健康自检】%s' % time.strftime('%Y-%m-%d %H:%M:%S'))
    print('=' * 104)
    checks = [
        ('腾讯快照 snapshot', lambda: ds.snapshot(MAIN), lambda r: '%d/%d 只有效' % (len(r), len(MAIN)),
         lambda r: len(r) == len(MAIN)),
        ('东财 clist 全市场', lambda: ds.clist(pages=1, fields='f12,f14,f3,f6,f62'), lambda r: '%d 行' % len(r),
         lambda r: len(r) > 0),
        ('东财 ulist 个股资金', lambda: ds.ulist([CODE]), lambda r: 'f62=%s' % (r.get(CODE, {}) or {}).get('f62'),
         lambda r: (r.get(CODE, {}) or {}).get('f62') is not None),
        ('东财 fflow 分时资金', lambda: ds.fflow(CODE, klt=1), lambda r: '%d 条' % len(r),
         lambda r: len(r) > 0),
        ('东财 fflow 日线资金', lambda: ds._load_dayflow(CODE) if hasattr(ds, '_load_dayflow') else ds.get(
            'https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?secid=%s&fields1=f1,f2,f3,f7'
            '&fields2=f51,f52,f53,f54,f55,f56&klt=101&lmt=5&ut=%s' % (ds.secid(CODE), ds.UT)),
         lambda r: '%d 字节' % len(r),
         lambda r: isinstance(r, str) and 'klines' in r and len(r) > 80),
        ('东财 board_index 板块', lambda: ds.board_index(refresh=True), lambda r: '%d 个板块' % len(r['by_name']),
         lambda r: len(r['by_name']) > 0),
        ('东财 sector_members', lambda: ds.sector_members('BK0457', pages=2), lambda r: '%d 只成分' % len(r),
         lambda r: len(r) > 0),
        ('东财 stock_profile 画像', lambda: ds.stock_profile(CODE),
         lambda r: '%s / %d 概念' % (r.get('industry'), len(r.get('concepts') or [])),
         lambda r: bool(r.get('industry'))),
        ('东财 main_business 主营', lambda: ds.main_business(CODE), lambda r: '%d 条' % len(r or []),
         lambda r: bool(r)),
        ('东财 financials 财务PIT', lambda: ds.financials(CODE), lambda r: '%d 期财报' % len(r),
         lambda r: len(r) > 0),
        ('东财 kline_em', lambda: ds.kline_em(CODE, 30), lambda r: '%d 根' % len(r or []),
         lambda r: len(r or []) > 0),
        ('新浪 kline_sina', lambda: ds.kline_sina(CODE, 30), lambda r: '%d 根' % len(r or []),
         lambda r: len(r or []) > 0),
        ('腾讯 kline_qq', lambda: ds.kline_qq(CODE, 30), lambda r: '%d 根' % len(r or []),
         lambda r: len(r or []) > 0),
        ('东财 mood 情绪温度', lambda: ds.mood(), lambda r: '/'.join(list(r)[:3]),
         lambda r: bool(r)),
    ]
    if quick:
        # 含 board_index / sector_members —— 否则无法判断"板块面是否可用"
        checks = checks[:6] + checks[10:13]

    rows = []
    for name, fn, desc, good in checks:
        rows.append(_blocks(name, fn, desc, good))
    print('  %-4s %-24s %8s  %s' % ('', '接口', '耗时ms', '结果'))
    print('  ' + '-' * 98)
    for mark, name, ms, detail in rows:
        print('  %-4s %-24s %8.0f  %s' % (mark, name, ms, detail))
    okn = sum(1 for r in rows if r[0] == '✅')
    warn = sum(1 for r in rows if r[0] == '⚠️')
    err = sum(1 for r in rows if r[0] == '❌')
    print('  ' + '-' * 98)
    print('  可用 %d/%d（%.0f%%）　｜　空返回/降级 %d 项　｜　异常 %d 项'
          % (okn, len(rows), okn / len(rows) * 100, warn, err))

    # ---- 东财 push2 族"整族阻断"判定（P75）----
    # 单项为空可能是偶发抖动；**同族多项同时为空/异常**则是连接级阻断。
    # 判据必须能区分这两者，否则会把"整族被掐"当成"今天运气不好"，反复重试并延长阻断。
    _p2keys = ('clist', 'ulist', 'fflow', 'board_index', 'sector_members', 'stock_profile', 'main_business')
    p2 = [r for r in rows
          if r[1].startswith('东财') and r[0] in ('⚠️', '❌')
          and any(k in r[1] for k in _p2keys)]
    if len(p2) >= 4:
        print()
        print('  ⛔ **东财 push2 族疑似连接级阻断**：%d 项同时不可用（%s）'
              % (len(p2), '、'.join(r[1].split()[-1] for r in p2[:7])))
        print('     实测特征：`*.push2*.eastmoney.com`（push2 / push2delay / 82.push2 / push2his）'
              '全部 HTTP 000、**0.2 秒内被 RST** ——')
        print('     不是 429、不是超时；换主机 / 换 ut / 走系统代理 / 直连 **全部无效**（出口 IP 未变）。')
        print('     ✅ 仍可用的东财通道：`datacenter-web`（财务 PIT）、`quote.eastmoney.com`（网页）。')
        print('     → **此时不要反复重试**（只会延长阻断）；替代源：腾讯快照 / 新浪·腾讯 K 线。')
        print('     → 资金面与板块面在这段时间内**没有替代源**，报告应按"数据不足"如实降级。')

    # K 线降级链命中
    k, src = ds.kline(CODE, 60)
    print('\n  K线降级链实际命中：**%s**（%d 根，末根 %s）' % (src, len(k), k[-1]['d'] if k else '—'))

    # 置信度评估
    print('\n  【今日结论置信度评估】')
    fund_tested = any(('ulist' in r[1] or 'fflow' in r[1]) for r in rows)
    board_tested = any('board_index' in r[1] for r in rows)
    fund_ok = any(r[0] == '✅' and ('ulist' in r[1] or 'fflow' in r[1]) for r in rows)
    board_ok = any(r[0] == '✅' and 'board_index' in r[1] for r in rows)
    if not (fund_tested and board_tested):
        print('   ⚠️ 本次为 --quick 模式，未覆盖全部维度 → 置信度评估不完整，请用完整模式复核。')
    elif fund_ok and board_ok:
        print('   ✅ 资金面 + 板块面均可用 → 四维评分可正常出具')
    elif fund_ok or board_ok:
        print('   ⚠️ 资金面/板块面**至少一项不可用** → 该维度会按基准分（50%）计入，')
        print('      报告分数会被拉向"中性档"。请把评级当区间看，并以收盘复核为准。')
    else:
        print('   ⛔ 资金面与板块面**同时不可用** → 按评级口径 v3，应当**不予评级**（显示"数据不足"）。')
    print('\n  ⚠️ 08:30 前与 09:30 前的时段属数据降级窗口，接口可用率会显著低于盘中（见 P40）。')


if __name__ == '__main__':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    run(quick='--quick' in sys.argv)
