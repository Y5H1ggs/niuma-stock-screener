"""winrate_select.py — 形态条件胜率筛选器（★ 量化选股的正门）

★★ 为什么要有这个工具（2026-10-30 血泪，务必先读）
   在此之前，本项目推荐的主口径是「趋势启动型」：
       价>MA5>MA10 ＋ 距MA60 −4%~+8% ＋ MA20≥MA60×0.97 ＋ 250位<45% ＋ 20日涨停=0
   用 146,028 条面板（498 只 × 313 个交易日，5 日前向收益）实测：
       **该口径 5 日胜率 45.8%（基准 47.0%）→ 负期望。**
   再叠加「量比 1.5~4（温和放量）」后更差：**42.9%（−4.1pp）**。
   → **「追强势 / 追放量」在这个池子上是负期望，与 Rank IC 结论一致**
     （`amt` IC −0.0871、`vr` IC −0.0382、`limit_up20` t −8.7）。

   **正期望来自它的反面**：
       低成交额 ＋ 缩量(量比<1) ＋ 250日位置<45% ＋ 价>MA20
       **胜率 52.8~53.1%（+6pp），盈亏比 1.29，扣成本后 5 日净期望 +0.33%**

★ 本工具只做三件事，全部可复现：
   ① `--compute`：在给定面板上**算条件胜率**（含凯利仓位），不猜、不调参
   ② `--scan`：按最优形态**全盘扫主板**（60/000/001/002/003），三级漏斗
   ③ `--enrich`：给入选清单补**三季报 + 上下游周转**（CCC / 应付 vs 应收）

★ 仓位纪律（凯利，硬约束，写进 SKILL）：
   全凯利 f* = (p·b − q)/b；实测 p=0.528、b=1.29 → **f* = 16.3%**
   实务取**半凯利 ≈ 8%**，资金受"一手"约束时**单笔上限 33%**。
   **禁止满仓单吊** —— 满仓是凯利的 6 倍下注，属超额下注，长期必然爆仓。

用法：
    python winrate_select.py --compute --panel _panel_big_flow.json
    python winrate_select.py --scan --cash 5000 --top 6
    python winrate_select.py --scan --cash 5000 --enrich
"""
import io
import json
import math
import os
import statistics as st
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ds

# ---- 形态定义（唯一口径来源，改这里等于改整个策略）----
AMT_Q = 0.33        # 成交额取"低三分位"以下
VR_MAX = 1.0        # 量比上限（<1 ＝ 缩量）
POS_MAX = 45.0      # 250 日位置上限
LU_MAX = 0          # 20 日涨停次数上限
COST_PCT = 0.30     # 双边成本（佣金+印花税+滑点，保守）

GROUPS = [
    ('基准（全样本）', lambda f, t: True),
    ('价>MA5>MA10', lambda f, t: (f.get('ma_align') or 0) >= 2),
    ('★ 旧口径：趋势启动（完整）', lambda f, t: (
        (f.get('ma_align') or 0) >= 2
        and -4 <= (f.get('dist_ma60') or -99) <= 8
        and (f.get('pos250') or 99) < 45
        and (f.get('limit_up20') or 0) == 0)),
    ('✗ 旧口径 + 量比1.5~4（放量）', lambda f, t: (
        (f.get('ma_align') or 0) >= 2
        and -4 <= (f.get('dist_ma60') or -99) <= 8
        and (f.get('pos250') or 99) < 45
        and (f.get('limit_up20') or 0) == 0
        and 1.5 <= (f.get('vr') or 0) <= 4)),
    ('✗ 追高（距MA60 > +15%）', lambda f, t: (f.get('dist_ma60') or -99) > 15),
    ('✗ 炒过（20日涨停≥3）', lambda f, t: (f.get('limit_up20') or 0) >= 3),
    ('成交额低三分位', lambda f, t: isinstance(f.get('amt'), (int, float)) and f['amt'] <= t['amt1']),
    ('成交额高三分位', lambda f, t: isinstance(f.get('amt'), (int, float)) and f['amt'] >= t['amt2']),
    ('量比<1（缩量）', lambda f, t: isinstance(f.get('vr'), (int, float)) and f['vr'] < VR_MAX),
    ('✗ 量比>2（放量）', lambda f, t: isinstance(f.get('vr'), (int, float)) and f['vr'] > 2),
    ('振幅小（低三分位）', lambda f, t: (f.get('range_ratio') or 99) <= t['rr1']),
    ('★★ 最优形态（低额+缩量+低位+价>MA20）', lambda f, t: (
        isinstance(f.get('amt'), (int, float)) and f['amt'] <= t['amt1']
        and isinstance(f.get('vr'), (int, float)) and f['vr'] < VR_MAX
        and (f.get('pos250') or 99) < POS_MAX
        and (f.get('ma_align') or 0) >= 1)),
]


def _q(vals, p):
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p))]


def compute(panel_path):
    D = json.load(io.open(panel_path, encoding='utf-8'))
    panel = D['panel']
    meta = D.get('meta') or {}
    amt_all = [r[2].get('amt') for r in panel if isinstance(r[2].get('amt'), (int, float))]
    rr_all = [r[2].get('range_ratio') for r in panel if isinstance(r[2].get('range_ratio'), (int, float))]
    th = {'amt1': _q(amt_all, AMT_Q), 'amt2': _q(amt_all, 1 - AMT_Q), 'rr1': _q(rr_all, AMT_Q)}
    base = sum(1 for r in panel if r[3] > 0) / len(panel) * 100
    print('面板 %s：%d 条 / %d 只 / %d 个交易日 %s'
          % (panel_path, len(panel), len({r[1] for r in panel}), len({r[0] for r in panel}),
             ('| ' + str(meta)) if meta else ''))
    print('阈值：成交额低三分位 ≤ %.0f 万元 | 振幅低三分位 ≤ %.3f' % (th['amt1'] / 1e4, th['rr1']))
    print('全样本基准胜率 %.2f%%\n' % base)
    print('%-38s %8s %8s %8s %9s %9s' % ('条件', 'N', '胜率', 'vs基准', '均值', '中位'))
    best = None
    for name, fn in GROUPS:
        sub = [r for r in panel if fn(r[2], th)]
        if len(sub) < 300:
            print('%-38s %8d   样本不足' % (name, len(sub)))
            continue
        wr = sum(1 for r in sub if r[3] > 0) / len(sub) * 100
        avg = st.mean([r[3] for r in sub])
        med = st.median([r[3] for r in sub])
        print('%-38s %8d %7.2f%% %+7.1fpp %+8.2f%% %+8.2f%%'
              % (name, len(sub), wr, wr - base, avg, med))
        if name.startswith('★★'):
            best = (name, sub, wr)
    if not best:
        return
    # 凯利
    name, sub, wr = best
    rets = [r[3] for r in sub]
    win = [x for x in rets if x > 0]
    lose = [x for x in rets if x <= 0]
    p = len(win) / len(rets)
    q = 1 - p
    aw = st.mean(win) if win else 0
    al = abs(st.mean(lose)) if lose else 0
    b = (aw / al) if al else 0
    exp_gross = p * aw - q * al
    print('\n=== 最优形态明细：%s ===' % name)
    print('  N=%d  胜率 %.2f%%  平均盈利 +%.2f%%  平均亏损 %.2f%%  盈亏比 b=%.3f'
          % (len(rets), p * 100, aw, al, b))
    print('  5 日期望 %+.3f%%  扣双边成本 %.2f%% → 净期望 %+.3f%%'
          % (exp_gross, COST_PCT, exp_gross - COST_PCT))
    print('  收益标准差 %.2f%%  5%%分位 %+.2f%%  95%%分位 %+.2f%%  最差单笔 %+.2f%%'
          % (st.pstdev(rets),
             sorted(rets)[int(len(rets) * 0.05)],
             sorted(rets)[int(len(rets) * 0.95)],
             min(rets)))
    k = (p * b - q) / b if b else 0
    print('\n=== 凯利仓位（★ 仓位纪律的唯一依据）===')
    print('  全凯利 f* = (p·b − q)/b = %.1f%%' % (k * 100))
    print('  半凯利（实务建议）      = %.1f%%' % (k * 50))
    print('  1/4 凯利               = %.1f%%' % (k * 25))
    print('  **单笔上限（一手约束下的妥协）＝ 33%**')
    print('  **满仓单吊 = 凯利的 %.1f 倍 → 超额下注，禁止**' % (100 / (k * 100) if k else 0))
    return {'p': p, 'b': b, 'kelly': k, 'th': th, 'base': base}


def scan(cash, top=10, enrich=False, panel_path=None):
    """三级漏斗：全市场 → 主板+买得起+估值 → 低额+缩量 → 低位+站上MA20+无涨停。"""
    th_amt = 8905.6      # 万元，来自 --compute 的实测低三分位
    if panel_path and os.path.exists(panel_path):
        D = json.load(io.open(panel_path, encoding='utf-8'))
        amt_all = [r[2].get('amt') for r in D['panel'] if isinstance(r[2].get('amt'), (int, float))]
        if amt_all:
            th_amt = _q(amt_all, AMT_Q) / 1e4
    print('单股价格上限 %.2f 元（现金 %.2f / 100 股）| 成交额上限 %.0f 万元' % (cash / 100, cash, th_amt))

    pool = {}
    for pg in range(1, 13):
        r = ds.dce_get('RPT_DMSK_TS_STOCKNEW', pz=500, page=pg)
        if not r:
            break
        for x in r:
            c = str(x.get('SECURITY_CODE') or '')
            if c:
                pool[c] = x
        if len(r) < 500:
            break
    print('全市场资金池 %d 只' % len(pool))

    s1 = []
    for c, x in pool.items():
        if not ds.allowed(c):
            continue
        nm = str(x.get('SECURITY_NAME_ABBR') or '')
        if 'ST' in nm.upper() or '退' in nm:
            continue
        px, pe = x.get('CLOSE_PRICE'), x.get('PE_DYNAMIC')
        if not isinstance(px, (int, float)) or px <= 0 or px * 100 > cash:
            continue
        if not isinstance(pe, (int, float)) or pe <= 0 or pe > 60:
            continue
        s1.append((c, nm, px, x.get('PRIME_INFLOW'), pe))
    print('一级（主板+价≤%.2f+0<PE≤60）：%d 只' % (cash / 100, len(s1)))

    codes = [x[0] for x in s1]
    snap = {}
    for i in range(0, len(codes), 400):
        snap.update(ds.snapshot(codes[i:i + 400]) or {})
    s2 = []
    for c, nm, px, zl, pe in s1:
        v = snap.get(c) or {}
        amt, lb, chg = v.get('amount'), v.get('lb'), v.get('chg')
        if not amt or lb is None:
            continue
        if amt > th_amt or lb >= VR_MAX:
            continue
        if chg is None or chg < -6:
            continue
        s2.append({'c': c, 'nm': nm, 'px': v.get('price') or px, 'chg': chg,
                   'lb': lb, 'amt': amt, 'pe': pe, 'zl': (zl or 0) / 1e8})
    print('二级（+成交额≤%.0f万 +量比<%.1f）：%d 只' % (th_amt, VR_MAX, len(s2)))

    from concurrent.futures import ThreadPoolExecutor

    def one(r):
        k = ds.kline_qq(r['c'], 320)
        if not k or len(k) < 70:
            return None
        cs = [x['c'] for x in k]
        base = cs[:-1] if k[-1]['d'] >= __import__('time').strftime('%Y-%m-%d') else cs
        ma20 = sum(base[-20:]) / 20
        ma60 = sum(base[-60:]) / 60
        hi, lo = max(base[-250:]), min(base[-250:])
        px = r['px']
        lu = sum(1 for i in range(max(1, len(k) - 21), len(k) - 1)
                 if k[i - 1]['c'] and k[i]['c'] / k[i - 1]['c'] >= 1.095)
        d = dict(r)
        d.update({'pos': (px - lo) / ((hi - lo) or 1e-9) * 100, 'ma20': ma20, 'ma60': ma60,
                  'd60': (px / ma60 - 1) * 100, 'lu': lu})
        return d

    out = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for r in ex.map(one, s2):
            if r:
                out.append(r)
    sel = [r for r in out if r['pos'] < POS_MAX and r['px'] > r['ma20'] and r['lu'] <= LU_MAX]
    sel.sort(key=lambda r: -r['zl'])
    print('三级（+250位<%.0f%% +价>MA20 +20日涨停≤%d）：%d 只\n' % (POS_MAX, LU_MAX, len(sel)))
    if not sel:
        print('  （今日无标的 —— 空仓是被允许的结论）')
        return []
    print('%-8s %-8s %7s %7s %6s %8s %6s %8s %6s %9s %7s'
          % ('代码', '名称', '现价', '今日', '量比', '成交额万', '250位', '距MA60', 'PE', '昨主力亿', '100股'))
    for r in sel[:top]:
        print('%-8s %-8s %7.2f %+6.2f%% %6.2f %8.0f %5.0f%% %+7.1f%% %6.1f %+9.2f %7.0f'
              % (r['c'], r['nm'][:6], r['px'], r['chg'], r['lb'], r['amt'],
                 r['pos'], r['d60'], r['pe'], r['zl'], r['px'] * 100))
    if enrich:
        print('\n=== 三季报 + 上下游（只在入选清单上做）===')
        for r in sel[:top]:
            _enrich_one(r)
    return sel[:top]


def _enrich_one(r):
    c, nm = r['c'], r['nm']

    def g(rpt):
        rr = ds.dce_get(rpt, pz=8, flt='(SECURITY_CODE="%s")' % c, sort='REPORT_DATE', stype=-1)
        return {str(x.get('REPORT_DATE'))[:10]: x for x in (rr or [])}
    I, C, B = g('RPT_DMSK_FN_INCOME'), g('RPT_DMSK_FN_CASHFLOW'), g('RPT_DMSK_FN_BALANCE')
    q3, q3p = '2026-09-30', '2025-09-30'

    def v(d, src, k):
        x = src.get(d) or {}
        val = x.get(k)
        return (val / 1e8) if isinstance(val, (int, float)) else None
    print('-' * 72)
    print('%s %s  现价 %.2f  PE %.1f  250位 %.0f%%  距MA60 %+.1f%%'
          % (c, nm, r['px'], r['pe'], r['pos'], r['d60']))
    if q3 not in I:
        print('  ⚠️ 三季报未取到（未披露或数据未更新）')
        return
    rev, rev0 = v(q3, I, 'TOTAL_OPERATE_INCOME'), v(q3p, I, 'TOTAL_OPERATE_INCOME')
    dn, dn0 = v(q3, I, 'DEDUCT_PARENT_NETPROFIT'), v(q3p, I, 'DEDUCT_PARENT_NETPROFIT')
    ocf = v(q3, C, 'NETCASH_OPERATE')
    ar, ap = v(q3, B, 'ACCOUNTS_RECE'), v(q3, B, 'ACCOUNTS_PAYABLE')
    iv, cost = v(q3, B, 'INVENTORY'), v(q3, I, 'OPERATE_COST')
    parts = []
    if rev and rev0:
        parts.append('营收 %.1f亿(%+.1f%%)' % (rev, (rev / rev0 - 1) * 100))
    if dn and dn0:
        parts.append('扣非 %.2f亿(%+.1f%%)' % (dn, (dn / dn0 - 1) * 100))
    if ocf is not None:
        parts.append('OCF %+.2f亿' % ocf)
    print('  ' + ' | '.join(parts))
    if rev and cost and None not in (ar, iv, ap):
        d270 = 270
        ard, ivd, apd = ar / rev * d270, iv / cost * d270, ap / cost * d270
        ccc = ard + ivd - apd
        if apd > ard:
            side = '✅ 占用上游资金 %.0f 天' % (apd - ard)
        else:
            side = '⚠️ 自己垫资 %.0f 天' % (ard - apd)
        print('  上下游：应收 %.0f天 存货 %.0f天 应付 %.0f天 → CCC %.0f 天 毛利率 %.1f%%  %s'
              % (ard, ivd, apd, ccc, (rev - cost) / rev * 100, side))


def main():
    args = sys.argv[1:]
    panel = None
    if '--panel' in args:
        panel = args[args.index('--panel') + 1]
    cash = 5000.0
    if '--cash' in args:
        cash = float(args[args.index('--cash') + 1])
    top = 10
    if '--top' in args:
        top = int(args[args.index('--top') + 1])
    if '--compute' in args:
        compute(panel or '_panel_big_flow.json')
        return
    if '--scan' in args:
        scan(cash, top=top, enrich='--enrich' in args, panel_path=panel)
        return
    print(__doc__)


if __name__ == '__main__':
    main()
