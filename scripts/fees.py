# -*- coding: utf-8 -*-
"""fees.py — 费用敏感度分析（README 路线图 P1-4）

回答三个问题：
  ① 最小可做资金线：本金低于多少，双边费用 + 最低佣金会吃掉过多收益？
  ② 成本矩阵：不同本金 × 不同价位下的单次交易费用占比
  ③ 临界涨幅：覆盖成本所需的最小涨幅

核心机制：**每边最低佣金（默认 5 元）在小资金下是主要摩擦** ——
  佣金 = max(5, 金额×万2.5)，当金额 < 2 万时**恒定 5 元/边**。
  于是费用占比 ≈ 10/金额 + 0.052%（印花税 0.05% + 过户费 0.002%）。
  → 5000 元：约 0.25%；2000 元：约 0.55%；1000 元：约 1.05%。
  **低于约 4000 元，"去赚 0.2%"这件事在数学上就不成立。**

用法：
  python fees.py                       # 全景分析
  python fees.py --cash 5,000         # 针对特定可用资金
"""
import sys
import io

import cfg

CN = {1: '一', 2: '二', 3: '三', 4: '四', 5: '五'}


def one(cash, price, lot=100):
    shares = int(cash / price / lot) * lot
    if shares <= 0:
        return None
    amt = shares * price
    fee = cfg.roundtrip_fee(price, shares)
    be = cfg.break_even(price, shares)
    st = cfg.stop_price(price, shares)
    return {
        'cash': cash, 'price': price, 'shares': shares, 'amt': amt,
        'leftover': cash - amt, 'fee': fee, 'fee_pct': fee / amt * 100,
        'be': be, 'be_pct': (be / price - 1) * 100,
        'stop': st,
    }


def min_viable_cash(threshold=0.30, lot=100):
    """求"费用占比 ≤ threshold%"所需的最小买入金额（按 10 元价位、整手）。"""
    price = 10.0
    c = 1000
    while c <= 200000:
        r = one(c, price, lot)
        if r and r['fee_pct'] <= threshold:
            return c, r
        c += 100
    return None, None


def main():
    print('=' * 96)
    print('【费用敏感度分析】—— 当前费率：佣金 %.4f%%（最低 %.0f 元/边）、印花税 %.3f%%、过户费 %.4f%%' % (
        float(cfg.get('commission_rate')) * 100, float(cfg.get('commission_min')),
        float(cfg.get('stamp_tax_rate')) * 100, float(cfg.get('transfer_fee_rate')) * 100))
    print('=' * 96)

    # ① 最小可做资金线
    print('\n① 最小可做资金线（按 10 元价位、整手买入）')
    print('  %-10s %-12s %-12s %-12s' % ('目标费用占比', '所需买入金额', '实际费用占比', '保本需涨'))
    for th in (0.50, 0.30, 0.25, 0.20):
        c, r = min_viable_cash(th)
        if c:
            print('  ≤ %-8.2f%% %-12s %-12s %-12s' % (
                th, '%d 元' % c,
                '%.3f%%' % r['fee_pct'], '%+.3f%%' % r['be_pct']))
        else:
            print('  ≤ %-8.2f%% 在 20 万以内无解' % th)

    # ② 成本矩阵
    print('\n② 成本矩阵：单次往返费用占买入金额比（%）')
    prices = [3.0, 5.0, 8.0, 11.0, 15.0, 20.0, 30.0]
    print('  %-12s' % '可用资金', end='')
    for p in prices:
        print('%9s' % ('%.0f元' % p), end='')
    print()
    print('  ' + '-' * 80)
    for cash in (1000, 2000, 3000, 4000, 5000, 6390, 8000, 10000, 20000, 50000):
        print('  %-12s' % ('%d 元' % cash), end='')
        for p in prices:
            r = one(cash, p)
            print('%8s' % ('%.3f' % r['fee_pct'] if r else '—'), end='')
        print()

    # ③ 临界涨幅
    print('\n③ 临界涨幅：覆盖成本所需的最小涨幅（= 保本价相对买入价的涨幅）')
    print('  含义：买入后必须先涨这么多，卖出才不亏。')
    print('  %-12s %-12s %-12s %-12s' % ('可用资金', '11 元买入股数', '保本价', '需涨'))
    for cash in (815, 2000, 4000, 5000, 6390, 10000, 20000, 50000):
        r = one(cash, 11.0)
        if r:
            print('  %-12s %-12s %-12s %-12s' % (
                '%d 元' % cash, '%d 股' % r['shares'], '%.4f' % r['be'], '%+.3f%%' % r['be_pct']))

    # 当前资金
    cash = float(cfg.get('cash') or 0)
    if '--cash' in sys.argv:
        cash = float(sys.argv[sys.argv.index('--cash') + 1])
    print('\n④ 你的当前资金（%.2f 元）在各价位下的处境' % cash)
    print('  %-8s %-10s %-10s %-12s %-12s %-14s' % (
        '价位', '可买股数', '占用金额', '余额', '单次费用', '费用占比 / 需涨'))
    for p in prices:
        r = one(cash, p)
        if not r:
            print('  %-8.2f 买不起一手' % p)
            continue
        print('  %-8.2f %-10d %-10.0f %-12.0f %-12.2f %.3f%% / %+.3f%%' % (
            p, r['shares'], r['amt'], r['leftover'], r['fee'], r['fee_pct'], r['be_pct']))

    print('\n  结论：')
    print('   · 费用占比随**买入金额**下降，与股价高低无关（股价只影响整手后剩多少现金）；')
    print('   · 最低佣金 5 元/边 = 单次固定 10 元，是 5000 元以下账户的**主导成本**；')
    print('   · 本工具默认 take_profit_pct=2.0%，若要"净赚 2%，须股价涨约 2.3%" ——')
    print('     目标收益必须按"含费口径"设定，否则会系统性高估自己的战绩。')


if __name__ == '__main__':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    main()
