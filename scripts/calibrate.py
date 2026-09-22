# -*- coding: utf-8 -*-
"""calibrate.py — 校准报告（README 路线图 P1-3 的执行体）

「后续都需要校准」要落到四张可复查的表上：

  A. 交易业绩    胜率 / 盈亏比 / EV / 费用占毛利 / 按卖出原因归因
  B. 止损执行率  用 K 线重建"本应止损"的笔数，统计实际执行与漏单损失
  C. 预测校准    predict 给出的概率 vs 实际发生频率（可靠性）+ Brier 分数
  D. 参数建议    基于以上给出可执行的阈值调整**建议**（不自动改参数）

用法：
  python calibrate.py                # 全量
  python calibrate.py settle         # 先结算到期预测（需联网）
  python calibrate.py trades|stops|forecasts

设计原则：只报事实与统计，**不建议者不自动生效**——参数改动必须经人工确认（见 SKILL.md 自升级协议）。
"""
import sys
import io
import os
import json
import datetime

import cfg
import ds
import journal

TODAY = datetime.date.today().isoformat()


# ------------------------------------------------------------------ 结算
def settle_all(verbose=True):
    """把已到期的预测与真实次日行情比对后打分。只结算已收盘的完整交易日。"""
    n = 0
    for f in journal.pending_forecasts():
        code, made, ref = f['code'], f['made_on'], f['ref_price']
        k, _src = ds.kline(code, n=160)
        if not k:
            continue
        nxt = [r for r in k if r['d'] > made]
        if not nxt:
            continue
        r = nxt[0]
        if r['d'] >= TODAY:          # 当天还没收盘，结算不完整 → 跳过
            continue
        act = {
            'date': r['d'],
            'open': round((r['o'] / ref - 1) * 100, 3),
            'close': round((r['c'] / ref - 1) * 100, 3),
            'high': round((r['h'] / ref - 1) * 100, 3),
            'low': round((r['l'] / ref - 1) * 100, 3),
        }
        journal.settle_forecast(f['id'], act)
        n += 1
        if verbose:
            print('  结算 %s %s → 次日 开%+.2f%% 收%+.2f%% 高%+.2f%% 低%+.2f%%' % (
                f['id'], code, act['open'], act['close'], act['high'], act['low']))
    return n


# ------------------------------------------------------------------ A 交易业绩
def report_trades():
    closed = [t for t in journal.list_trades() if t['sell_date'] and t['pnl'] is not None]
    print('=' * 96)
    print('【A. 交易业绩】已平仓 %d 笔' % len(closed))
    print('=' * 96)
    if not closed:
        print('  暂无已平仓交易')
        return
    wins = [t for t in closed if t['pnl'] > 0]
    losses = [t for t in closed if t['pnl'] <= 0]
    gw = sum(t['pnl'] for t in wins)
    gl = abs(sum(t['pnl'] for t in losses)) or 1e-9
    total = sum(t['pnl'] for t in closed)
    fee = sum((t['buy_total'] - t['buy_price'] * t['shares'])
              + (t['sell_price'] * t['shares'] - t['sell_total']) for t in closed)
    gross = sum(max(t['pnl'], 0) for t in closed) or 1e-9
    print('  胜率        %.1f%%（%d 胜 / %d 负）' % (len(wins) / len(closed) * 100, len(wins), len(losses)))
    print('  累计盈亏    %+.2f 元' % total)
    print('  平均盈/亏   %+.2f%% / %+.2f%%' % (
        sum(t['pnl_pct'] for t in wins) / max(len(wins), 1),
        sum(t['pnl_pct'] for t in losses) / max(len(losses), 1)))
    print('  盈亏比      %.2f（毛利 %.2f / 毛亏 %.2f）' % (gw / gl, gw, gl))
    print('  单笔 EV     %+.2f 元（%.3f%%）' % (total / len(closed),
                                             sum(t['pnl_pct'] for t in closed) / len(closed)))
    print('  费用合计    %.2f 元，占毛利 %.1f%%  ← 摩擦成本吃掉多少' % (fee, fee / gross * 100))
    print('  最大单笔亏  %+.2f 元' % min(t['pnl'] for t in closed))
    cd, md = journal.max_drawdown()
    print('  当前/最大回撤 %.2f%% / %.2f%%' % (cd, md))

    print('\n  按卖出原因归因：')
    by = {}
    for t in closed:
        by.setdefault(t.get('sell_reason') or 'unknown', []).append(t)
    for r, ts in sorted(by.items(), key=lambda kv: sum(x['pnl'] for x in kv[1])):
        print('    %-14s %d 笔 | 累计 %+8.2f 元 | 均值 %+7.3f%% | 胜率 %.0f%%' % (
            r, len(ts), sum(x['pnl'] for x in ts),
            sum(x['pnl_pct'] for x in ts) / len(ts),
            sum(1 for x in ts if x['pnl'] > 0) / len(ts) * 100))


# ------------------------------------------------------------------ B 止损执行率
def report_stops():
    closed = [t for t in journal.list_trades() if t['sell_date']]
    print('=' * 96)
    print('【B. 止损执行率】用 K 线重建"是否曾触及止损线"')
    print('=' * 96)
    if not closed:
        print('  暂无已平仓交易')
        return
    should = 0
    missed = []
    for t in closed:
        k, _ = ds.kline(t['code'], n=200)
        if not k:
            continue
        seg = [r for r in k if t['buy_date'] < r['d'] <= t['sell_date']]
        touched = [r for r in seg if r['l'] <= t['stop_price']]
        if not touched:
            continue
        should += 1
        # 卖出价低于止损线 = 已执行（但也可能卖得更低）；高于 = 漏单
        if t['sell_price'] > t['stop_price']:
            missed.append((t, touched[0]))
    if should == 0:
        print('  已平仓交易中，**没有任何一笔曾触及止损线** —— 说明止损纪律尚未被真正考验')
    else:
        print('  应止损笔数   %d' % should)
        print('  实际执行     %d 笔（执行率 %.0f%%）' % (should - len(missed), (should - len(missed)) / should * 100))
        for t, r in missed:
            diff = (t['stop_price'] - t['sell_price']) * t['shares']
            verdict = ('多亏 %.2f 元（卖价低于止损线）' % diff) if diff > 0 else \
                      ('**但卖价高于止损线 %.3f → 实际少亏 %.2f 元**（纪律未机械执行，结果反而更好）'
                       % (t['stop_price'], -diff))
            print('    ⚠️ 漏单 %s %s：%s 最低 %.3f 已破止损 %.3f，实际卖在 %.3f → %s' % (
                t['id'], t['name'], r['d'], r['l'], t['stop_price'], t['sell_price'], verdict))
    print('\n  口径：买入日之后、卖出日（含）之前的 K 线最低价 ≤ 止损价 → 视为"本应止损"。')
    print('       未在本工具登记的场外交易不在统计内。')


# ------------------------------------------------------------------ C 预测校准
def report_forecasts():
    fs = [f for f in journal._load(journal.P_FORECASTS, []) if f['settle_date'] and f['actual']]
    print('=' * 96)
    print('【C. 预测校准】已结算 %d / %d 条' % (
        len(fs), len(journal._load(journal.P_FORECASTS, []))))
    print('=' * 96)
    if not fs:
        print('  暂无已结算预测（运行 `python calibrate.py settle` 结算到期项）')
        return
    checks = [
        ('open_up', '开盘上涨', lambda a: a['open'] > 0),
        ('close_up', '收盘上涨', lambda a: a['close'] > 0),
        ('high_ge1', '盘中曾涨≥1%', lambda a: a['high'] >= 1.0),
        ('high_ge2', '盘中曾涨≥2%', lambda a: a['high'] >= 2.0),
        ('low_le1', '盘中曾跌≤-1%', lambda a: a['low'] <= -1.0),
        ('break3', '盘中跌破-3%', lambda a: a['low'] <= -3.0),
    ]
    print('  %-14s %6s %10s %10s %10s' % ('事件', '样本', '平均预测', '实际频率', '偏差'))
    for key, label, fn in checks:
        sub = [f for f in fs if key in f['probs']]
        if not sub:
            continue
        p_avg = sum(f['probs'][key] for f in sub) / len(sub)
        obs = sum(1 for f in sub if fn(f['actual'])) / len(sub) * 100
        flag = '  ⚠️ 高估' if p_avg - obs > 12 else ('  ⚠️ 低估' if obs - p_avg > 12 else '')
        print('  %-14s %6d %9.1f%% %9.1f%% %+9.1fpp%s' % (label, len(sub), p_avg, obs, obs - p_avg, flag))
    bs = [f['brier'] for f in fs if f['brier'] is not None]
    if bs:
        print('\n  平均 Brier 分数 %.4f（0 最好 / 0.25 相当于瞎猜 50%% / 1 最差）' % (sum(bs) / len(bs)))
        print('  参考：对二元事件给恒定 50%% 的 Brier = 0.25；低于 0.25 才算提供了信息。')
    print('\n  明细：')
    for f in fs[-12:]:
        a = f['actual']
        print('    %s %s | 预测 收涨%.0f%% 冲1%.0f%% | 实际 开%+.2f%% 收%+.2f%% 高%+.2f%% 低%+.2f%% | Brier %s' % (
            f['id'], f['code'], f['probs'].get('close_up', 0), f['probs'].get('high_ge1', 0),
            a['open'], a['close'], a['high'], a['low'], f['brier']))


# ------------------------------------------------------------------ D 参数建议
def report_advice():
    closed = [t for t in journal.list_trades() if t['sell_date'] and t['pnl'] is not None]
    fs = [f for f in journal._load(journal.P_FORECASTS, []) if f['settle_date']]
    print('=' * 96)
    print('【D. 参数建议】—— 仅供参考，改动须经人工确认后写入 config.json / references')
    print('=' * 96)
    tips = []
    if len(closed) < 10:
        tips.append('样本仅 %d 笔，**任何参数调优都不可靠**（统计上至少 30 笔才有意义）。'
                    '当前阶段的正确做法是：只记录、不调参。' % len(closed))
    else:
        wins = [t for t in closed if t['pnl'] > 0]
        wr = len(wins) / len(closed) * 100
        rr = (sum(t['pnl'] for t in wins) / max(len(wins), 1)) / \
             (abs(sum(t['pnl'] for t in closed if t['pnl'] <= 0)) / max(len(closed) - len(wins), 1) + 1e-9)
        if wr < 45 and rr < 1.5:
            tips.append('胜率 %.0f%% + 盈亏比 %.2f 同时偏低 → 优先收紧**入场条件**（而非放宽止损）。' % (wr, rr))
        if rr >= 1.5 and wr < 45:
            tips.append('盈亏比高但胜率低 → 这是"截断亏损、让利润奔跑"型，**不要**为了提升胜率而提前止盈。')
    if fs:
        avg_b = sum(f['brier'] for f in fs if f['brier'] is not None) / max(len(fs), 1)
        if avg_b and avg_b > 0.25:
            tips.append('预测 Brier %.4f > 0.25（不如恒定 50%%）→ 形态邻域法在该股上暂未提供有效信息，'
                        '降低对 predict 输出的权重。' % avg_b)
        else:
            tips.append('预测 Brier %.4f ≤ 0.25 → 概率输出优于随机，可继续使用。' % (avg_b or 0))
    cost_note = '费用占毛利比是 Gap 的隐性来源：见 `python fees.py` 的最小可做资金线。'
    tips.append(cost_note)
    for i, t in enumerate(tips, 1):
        print('  %d. %s' % (i, t))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'all'
    if cmd in ('settle', 'all'):
        n = settle_all()
        print('已结算预测 %d 条\n' % n)
    if cmd in ('trades', 'all'):
        report_trades()
        print()
    if cmd in ('stops', 'all'):
        report_stops()
        print()
    if cmd in ('forecasts', 'all'):
        report_forecasts()
        print()
    if cmd in ('advice', 'all'):
        report_advice()
    if cmd == 'all':
        print('\n' + '=' * 96)
        print('  校准完成。按 SKILL.md「自升级与校准协议」：把本次结论写入 CHANGELOG，')
        print('  参数改动必须人工确认后再落 config.json —— 校准报告本身不改任何东西。')
        print('=' * 96)


if __name__ == '__main__':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    main()
