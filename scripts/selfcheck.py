# -*- coding: utf-8 -*-
"""selfcheck.py — 自证校验常驻化（不依赖第二数据源）

核心思想：**靠数据内部的恒等关系自证**，而不是等第二个数据源来对比。

  交叉校验（`verify.py`）需要**两个源都可用**才能做；自证校验只需要**一个源**，
  检查它的数字在内部是否自洽 —— 这就是它能"每次取数都跑"的原因。

它能抓住什么（全部是实际踩过的坑）：
  · 占位符 `"-"` 被 `_fl()` 静默读成 `0`        → 值域/恒等式立刻不成立（P40/P43）
  · 单位错误（万元当元、手当股）                → 量额关系不成立（P47）
  · 字段顺序读错                                → 恒等式不成立（P42 同类）
  · K 线末根是"昨日"还是"今日盘中"混淆            → 与快照对不上（P37）
  · 涨停价用的前收不对（除权/口径错）            → 与 prev 不符
  · 费用模型方向反了（同价卖出反而不亏）          → 一眼可查

用法：
  python selfcheck.py 601169            # 单票全量自证
  python selfcheck.py 601169 600519     # 多票
  python selfcheck.py 601169 --json     # JSON 输出（供其他模块消费）

被调用：`report.py` 采集完数据后自动跑一遍，失败项进入报告「数据完整度」卡。
"""
import sys
import io
import os
import json

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ds
import cfg

FAIL, WARN, OK = 'FAIL', 'WARN', 'OK'


def _r(x, n=3):
    return round(x, n) if isinstance(x, (int, float)) else x


# ------------------------------------------------------------------ 各段断言
def check_snapshot(s, limit_pct=None):
    """快照内部自洽：价格区间 / 涨跌幅 / 涨停价 / 内外盘 / 量额关系 / 量比。

    ⚠️ 设计要点：**每项断言无论通过与否都留一条记录**。
    只记录失败项会导致"全通过时显示 0 项断言"，看起来像"根本没检查"——
    校验器必须自证它跑过了。
    """
    if not s:
        return [(FAIL, 'snapshot', '快照为空或取数失败')]
    out = []
    lim_pct = float(limit_pct if limit_pct is not None else cfg.limit_pct())
    px, prev, o, h, l = s['price'], s['prev'], s['open'], s['high'], s['low']
    vol, amt = s['vol'], s['amount']

    ok = (l - 1e-9 <= min(o, px)) and (max(o, px) <= h + 1e-9)
    out.append((OK, '价格区间', 'low ≤ open/price ≤ high') if ok else
               (FAIL, '价格区间', '低%.2f 开%.2f 现%.2f 高%.2f —— 不满足 low ≤ open,price ≤ high' % (l, o, px, h)))

    if prev:
        calc = (px / prev - 1) * 100
        ok = abs(calc - s['chg']) <= 0.06
        out.append((OK, '涨跌幅', '手算 %+.3f%% = 接口' % calc) if ok else
                   (FAIL, '涨跌幅', '手算 %+.3f%% vs 接口 %+.3f%%（差 %.3fpp）' % (
                       calc, s['chg'], abs(calc - s['chg']))))
    else:
        out.append((WARN, '昨收', 'prev = 0，无法校验涨跌幅与涨停价'))

    if prev and s.get('limit_up'):
        lim = round(prev * (1 + lim_pct / 100.0), 2)
        ok = abs(lim - s['limit_up']) <= 0.02
        out.append((OK, '涨停价', '%.2f = prev %.2f × %.0f%%' % (lim, prev, lim_pct)) if ok else
                   (WARN, '涨停价', '手算 %.2f vs 接口 %.2f —— 可能除权或市场档不符' % (lim, s['limit_up'])))

    if vol and ((s.get('outer') or 0) + (s.get('inner') or 0)) > 0:
        tot = s['outer'] + s['inner']
        ok = abs(tot / vol - 1) <= 0.02
        out.append((OK, '内外盘', '外+内 = 成交量（%d）' % tot) if ok else
                   (WARN, '内外盘', '外+内 %d vs 成交量 %d（差 %.1f%%；多见于竞价或停牌）' % (
                       tot, vol, (tot / vol - 1) * 100)))
    else:
        out.append((WARN, '内外盘', '外盘/内盘均为 0（未开盘或字段缺失）'))

    if vol and amt:
        avg = amt * 1e4 / (vol * 100)
        ok = l * 0.92 <= avg <= h * 1.08
        out.append((OK, '量额关系', '隐含均价 %.3f 落在 [%.2f, %.2f]' % (avg, l, h)) if ok else
                   (FAIL, '量额关系', '隐含均价 %.3f 不在 [%.2f, %.2f] → 单位或口径可能错' % (avg, l, h)))
    else:
        out.append((WARN, '量额关系', '成交量或成交额为 0，无法校验'))

    lb = s.get('lb')
    if lb is None or lb != lb:
        out.append((WARN, '量比', '缺失或 NaN'))
    elif lb <= 0:
        out.append((WARN, '量比', '%.2f ≤ 0（未开盘或取数失败）' % lb))
    else:
        out.append((OK, '量比', '%.2f' % lb))
    return out


def check_flow_seq(ff, tag='fflow'):
    """资金流时间序列恒等式：主力 == 大单 + 超大单；主力 + 中单 + 小单 == 0。"""
    if not ff:
        # ★ 2026-10-27 改：旧文案只写「无数据」，读者会以为**报告缺数据**，
        #   但实际是 `ds.fflow`（东财 push2）**瞬时抖动**，
        #   而 `flow_days_any()` 有降级链（→ 新浪 MoneyFlow），**资金面并不缺**。
        #   实测：13:16 生成报告时 fflow 空，13:31 再查就有 152 行 —— 纯瞬时故障。
        return [(WARN, tag,
                 'fflow(push2) 本次取不到 —— 若报告资金面正常，说明已自动降级（新浪）；'
                 '若资金面也空，才是真的缺数据。push2 抖动通常几秒~几分钟自愈，可重跑')]
    out = []
    t, zl, sm, mid, big, xl = ff[-1]
    ok = abs(zl - (big + xl)) <= max(abs(zl) * 0.02, 1.0)
    out.append((OK, tag + ' 恒等式', '主力 = 大单 + 超大单（%.0f）' % zl) if ok else
               (FAIL, tag + ' 恒等式', '%s 主力 %.0f ≠ 大单 %.0f + 超大单 %.0f（差 %.0f）→ 字段顺序可能读错' % (
                   t[-5:], zl, big, xl, zl - (big + xl))))
    ok2 = abs(zl + mid + sm) <= max(abs(zl) * 0.05, 1e4)
    out.append((OK, tag + ' 归零式', '主力+中单+小单 = 0') if ok2 else
               (WARN, tag + ' 归零式', '%s 主力+中单+小单 = %.0f（理论应为 0，偏差 >5%%）' % (t[-5:], zl + mid + sm)))
    return out


def check_ulist(u, tag='ulist'):
    """个股资金（东财 ulist）：f62 == f66 + f72；f62 + f78 + f84 == 0。"""
    if not u:
        return [(WARN, tag, '无数据')]
    out = []
    f62, f66, f72 = u.get('f62'), u.get('f66'), u.get('f72')
    f78, f84 = u.get('f78'), u.get('f84')
    if None not in (f62, f66, f72):
        ok = abs(f62 - (f66 + f72)) <= max(abs(f62) * 0.02, 1.0)
        out.append((OK, tag + ' 恒等式', '主力 = 超大单 + 大单（%.0f）' % f62) if ok else
                   (FAIL, tag + ' 恒等式', '主力 %.0f ≠ 超大单 %.0f + 大单 %.0f（差 %.0f）' % (
                       f62, f66, f72, f62 - f66 - f72)))
    else:
        out.append((WARN, tag + ' 恒等式', 'f62/f66/f72 缺失'))
    if None not in (f62, f78, f84):
        ok = abs(f62 + f78 + f84) <= max(abs(f62) * 0.05, 1e4)
        out.append((OK, tag + ' 归零式', '主力+中单+小单 = 0') if ok else
                   (WARN, tag + ' 归零式', '主力+中单+小单 = %.0f（理论应为 0）' % (f62 + f78 + f84)))
    return out


def check_kline(k, s, tag='kline'):
    """K 线末根与快照对齐；日期必须递增。"""
    if not k or not s:
        return [(WARN, tag, 'K线或快照缺失')]
    out = []
    last = k[-1]
    tm = str(s.get('time') or '')[:8]
    tds = '%s-%s-%s' % (tm[:4], tm[4:6], tm[6:8]) if len(tm) == 8 else None
    if tds:
        if last['d'] == tds:
            ok = (not s['price']) or abs(last['c'] - s['price']) / s['price'] <= 0.02
            out.append((OK, tag + ' 末根', '末根=今日(%s)，收盘 %.3f ≈ 现价 %.3f' % (last['d'], last['c'], s['price'])) if ok else
                       (WARN, tag + ' 末根', '末根=今日 但收盘 %.3f 与现价 %.3f 差 >2%%' % (last['c'], s['price'])))
        else:
            ok = (not s['prev']) or abs(last['c'] - s['prev']) <= 0.02
            out.append((OK, tag + ' 末根', '末根=%s(昨日)，收盘 %.3f = 快照昨收' % (last['d'], last['c'])) if ok else
                       (WARN, tag + ' 末根', '末根 %s 收盘 %.3f ≠ 快照昨收 %.3f → 取数口径不一致（P37）' % (
                           last['d'], last['c'], s['prev'])))
    else:
        out.append((WARN, tag + ' 末根', '快照无时间戳，无法对齐'))
    dates = [x['d'] for x in k[-6:]]
    ok = dates == sorted(dates)
    out.append((OK, tag + ' 顺序', '最近 6 根日期递增') if ok else
               (FAIL, tag + ' 顺序', '最近 6 根日期非递增：%s' % dates))
    return out


def check_fees(price, shares, tag='费用模型'):
    """费用模型方向与量级自洽。"""
    if not price or not shares:
        return [(WARN, tag, '价格或股数缺失')]
    out = []
    bc = cfg.buy_cost(price, shares)
    sp = cfg.sell_proceeds(price, shares)
    be = cfg.break_even(price, shares)
    st = cfg.stop_price(price, shares)
    out.append((OK, tag + ' 方向', '同价卖出净得 %.2f < 支出 %.2f' % (sp, bc)) if sp < bc else
               (FAIL, tag + ' 方向', '同价卖出净得 %.2f ≥ 买入支出 %.2f → 费用方向反了' % (sp, bc)))
    out.append((OK, tag + ' 保本', '保本 %.4f > 买入 %.4f > 止损 %.4f' % (be, price, st)) if (st < be and be > price) else
               (FAIL, tag + ' 保本', '保本 %.4f / 买入 %.4f / 止损 %.4f 量级关系异常' % (be, price, st)))
    fee = cfg.roundtrip_fee(price, shares)
    ok = fee > 0 and fee / (price * shares) <= 0.05
    out.append((OK, tag + ' 量级', '双边 %.2f 元（%.3f%%）' % (fee, fee / (price * shares) * 100)) if ok else
               (WARN, tag + ' 量级', '双边费用 %.2f 占买入额 %.2f%%（异常）' % (fee, fee / (price * shares) * 100)))
    return out


def check_board_alignment(prof, tag='板块归属'):
    """个股画像与板块索引的一致性。"""
    out = []
    try:
        idx = ds.board_index()['by_name']
    except Exception:
        return [(WARN, tag, '板块索引取数失败')]
    # ⚠️ 必须检查**整条行业候选链**，不能只看最粗一级：
    #   降级源（datacenter）下「电力设备」BK1200 不在行业报表的 128 个板块内，
    #   但它的细分行业「电池」BK1033 在 —— 只看第一级会误报"口径变更"，
    #   而实际上下游已经顺链命中了正确基准（假警报比不报警更消耗信任）。
    inds = prof.get('industries') or ([prof['industry']] if prof.get('industry') else [])
    if inds:
        hit = [x for x in inds if x in idx]
        out.append((OK, tag, '行业候选「%s」命中板块索引（取「%s」为基准）' % ('/'.join(inds), hit[0]))
                   if hit else
                   (WARN, tag, '行业候选「%s」全部未命中板块索引（口径可能变更）' % '/'.join(inds)))
    cs = prof.get('concepts') or []
    miss = [c for c in cs if c not in idx]
    # ★ 2026-10-27 改：**概念未命中属于常态，不该进「自证校验提示」**。
    #   原因：`board_index()` 是东财**行情板块**体系，而 `stock_profile()` 的概念
    #   在 push2 与 datacenter「核心题材」之间会切换，两套命名本就不一致（P83）。
    #   实测 002091：13 个概念里有 3 个（参股保险/超级电容/一带一路）不在行情板块索引里 ——
    #   这是**正常现象**，报 WARN 会让使用者以为数据坏了。
    #   → 只有**大面积未命中（≥50%）**才值得提醒（那才是体系切换或索引过期的信号）。
    if not cs:
        out.append((OK, tag, '无概念字段（不影响结论）'))
    elif len(miss) <= len(cs) * 0.5:
        out.append((OK, tag, '概念命中 %d/%d（%d 个未在行情板块索引，属命名体系差异，非缺失）'
                    % (len(cs) - len(miss), len(cs), len(miss))))
    else:
        out.append((WARN, tag, '概念仅命中 %d/%d（未命中 ≥50%%，可能板块体系切换或索引过期；'
                    '不代表数据缺失，但行业基准可能取错）'
                    % (len(cs) - len(miss), len(cs))))
    return out


# ------------------------------------------------------------------ 汇总
def check_ledger(tag='账本', cash=None):
    """交易台账 ↔ 账户现金 的一致性自证。

    ★ 2026-10-22 补：当天实测到「平仓后现金不回账」——
      `journal._sync_account_from_trades()` 的 docstring 写着"把卖出净得加回现金"，
      但代码只更新 `peak_equity`，`cash` 从未被改；`add_trade` 也不扣现金。
      而 **selfcheck 当时完全没报**，因为它只查行情类恒等式。

      → 账本错了不会让任何行情指标变形，却会让**可买股数 / 费用占比 / 仓位建议**
        全部失真，且**很难自己发现**（现金少了一笔也不会报错）。

    判据（三条，全部可从账本内部推出）：
      ① `cash < 0`                          → FAIL（不可能为负）
      ② 已平仓但未入账（有 `synced_trades`）   → WARN（钱可能没回账）
      ③ `cash + 持仓成本 > 峰值 × 1.5`        → WARN（疑似重复入账）
    """
    out = []
    try:
        import journal as J
        acc = J.account()
        trades = J.list_trades()
    except Exception as e:
        return [(WARN, tag, '台账不可读：%s' % str(e)[:50])]

    c = float(cash if cash is not None else (acc.get('cash') or 0.0))
    peak = float(acc.get('peak_equity') or 0.0)
    held = sum(float(t.get('buy_total') or 0) for t in trades if not t.get('sell_date'))

    out.append((OK, tag + '·现金', '现金 %.2f ≥ 0' % c) if c >= 0 else
               (FAIL, tag + '·现金', '现金 %.2f **为负** —— 账本已失真' % c))

    done = [t for t in trades if t.get('sell_date')]
    synced = set(acc.get('synced_trades') or [])
    if done:
        if not synced:
            out.append((WARN, tag + '·入账',
                        '有 %d 笔已平仓，但 `synced_trades` 为空 —— '
                        '无法确认卖出净得是否已回到 cash' % len(done)))
        else:
            miss = [t['id'] for t in done if t['id'] not in synced]
            out.append((OK, tag + '·入账', '%d/%d 笔已平仓已计入现金'
                        % (len(done) - len(miss), len(done))) if not miss else
                       (WARN, tag + '·入账', '%d 笔已平仓但**未入账**：%s'
                        % (len(miss), ', '.join(miss[:3]))))

    if peak > 0:
        tot = c + held
        out.append((OK, tag + '·量级', '现金+持仓 %.2f ≤ 峰值 %.2f×1.5' % (tot, peak)) if tot <= peak * 1.5 else
                   (WARN, tag + '·量级', '现金+持仓 %.2f > 峰值 %.2f×1.5 → 疑似重复入账'
                    % (tot, peak)))
    return out


def check_all(code, shares=500, snap=None, kline=None, ulist=None, fflow=None,
              prof=None, limit_pct=None):
    """跑全部自证断言。返回 {'code':…, 'fail':[...], 'warn':[...], 'pass':int, 'grade':str}"""
    res = []
    s = snap if snap is not None else ds.snapshot([code]).get(code)
    res += check_snapshot(s, limit_pct)

    if s:
        k = kline if kline is not None else (ds.kline(code, 60)[0] or [])
        res += check_kline(k, s)
    if ulist is None:
        try:
            ulist = ds.ulist([code]).get(code)
        except Exception:
            ulist = None
    res += check_ulist(ulist)
    if fflow is None:
        try:
            fflow = ds.fflow(code, klt=1)
        except Exception:
            fflow = []
    res += check_flow_seq(fflow)
    if prof is None:
        try:
            prof = ds.stock_profile(code)
        except Exception:
            prof = {}
    res += check_board_alignment(prof)
    if s and s.get('price'):
        res += check_fees(s['price'], shares)

    fails = [r for r in res if r[0] == FAIL]
    warns = [r for r in res if r[0] == WARN]
    total = len(res)
    grade = 'A（全部自洽）' if not fails and not warns else (
        'B（有提示）' if not fails else 'C（存在自证失败）')
    return {'code': code, 'fail': fails, 'warn': warns, 'pass': total - len(fails) - len(warns),
            'total': total, 'grade': grade}


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    as_json = '--json' in sys.argv
    shares = 500
    for i, a in enumerate(sys.argv):
        if a == '--shares':
            shares = int(sys.argv[i + 1])
    if not args:
        raise SystemExit('用法: python selfcheck.py <代码...> [--shares N] [--json]')

    results = [check_all(c, shares=shares) for c in args]

    if as_json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
        return

    print('=' * 96)
    print('【自证校验】不依赖第二数据源的内恒等关系检查')
    print('=' * 96)
    for r in results:
        print()
        print('  ── %s  %s  （共 %d 项断言，通过 %d）' % (
            r['code'], r['grade'], r['total'], r['pass']))
        for lvl, item, msg in r['fail']:
            print('     ❌ [%s] %s' % (item, msg))
        for lvl, item, msg in r['warn']:
            print('     ⚠️  [%s] %s' % (item, msg))
        if not r['fail'] and not r['warn']:
            print('     ✅ 全部自洽')

    nf = sum(len(r['fail']) for r in results)
    nw = sum(len(r['warn']) for r in results)
    print()
    print('=' * 96)
    print('  合计：失败 %d 项 / 提示 %d 项' % (nf, nw))
    print('  ⚠️ FAIL = 内部矛盾，该数据**不可用**；WARN = 需人工确认（可能是正常市况，如竞价期内外盘）。')
    print('  ⚠️ 自证只能证明"内部自洽"，不能证明"与真实市场一致"——后者要靠 verify.py 双源比对。')
    print('=' * 96)


if __name__ == '__main__':
    main()
