# -*- coding: utf-8 -*-
"""fundamentals.py — 财报深度：杜邦分解 / 三表勾稽 / 盈利质量 / 财务健康 / 成长质量

## 为什么单独做一个模块

`financials()` 只给表面指标（ROE、毛利率、增速）。判断「这个 ROE 是真本事还是加了杠杆」
「利润有没有变成现金」「增长是不是靠赊销堆出来的」，必须回到三表。

## 口径红线（这一节比代码重要）

**① 累计口径必须年化。**
`RPT_DMSK_FN_*` 的营收/净利/现金流都是**年初至今累计**。
Q3 报表是 9 个月的数据，直接拿它除以总资产算周转率，会**系统性偏低 25%**。
本模块统一乘 `360 / 已过天数` 年化，并在输出里标 `annualized=True`。

**② 杜邦分解必须用「平均总资产」，不是期末总资产。**
周转率 = 营收 / 平均总资产（期初+期末）/2。用期末值会在扩表年份高估效率。

**③ 现金流与利润的背离要看「方向 + 幅度 + 持续性」三个维度。**
单看 OCF/净利润 < 1 就喊「盈利质量差」是错的——
扩张期备货、预收款减少都会让 OCF 暂时低于利润，要看连续 4 期趋势。

## 输出约定

所有函数返回 dict，**取不到就给 None 并在 `warn` 里说明**，不填 0、不猜。

用法
----
    python fundamentals.py 600000
    python fundamentals.py 600000 --json
"""
import io
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ds  # noqa: E402


# ------------------------------------------------------------------ 三表读取
def _days_of(report_date):
    """报告期 → 已过天数（用于年化）"""
    md = report_date[5:]
    return {'03-31': 90, '06-30': 180, '09-30': 270, '12-31': 360}.get(md, 360)


def load3(code, periods=8):
    """读三大报表并对齐日期。

    返回 {'dates': [...倒序], 'B': {d: row}, 'I': {d: row}, 'C': {d: row}}
    """
    def _load(rpt):
        r = ds.dce_get(rpt, pz=periods + 6, flt='(SECURITY_CODE="%s")' % code,
                       sort='REPORT_DATE', stype=-1)
        return {str(x.get('REPORT_DATE'))[:10]: x for x in (r or [])}

    B, I, C = _load('RPT_DMSK_FN_BALANCE'), _load('RPT_DMSK_FN_INCOME'), _load('RPT_DMSK_FN_CASHFLOW')
    dates = sorted(set(B) & set(I), reverse=True)[:periods]
    return {'dates': dates, 'B': B, 'I': I, 'C': C}


def _g(d, src, k, scale=1e8):
    v = (src.get(d) or {}).get(k)
    return (v / scale) if isinstance(v, (int, float)) else None


def _div(a, b):
    return (a / b) if (a is not None and b not in (None, 0)) else None


# 金融业（银行/保险/证券）的报表结构与工商企业**根本不同**：
#   - 没有「应收账款 / 存货」，周转率天然极低（资产主要是贷款）
#   - 不披露「流动比率」，监管指标是 LCR/流动性覆盖率
#   - 经营现金流含存款增减，波动巨大（OCF/净利润 可以是 6 倍或负）
# 用工商企业的标准解读金融股 = **每一条都是错读数**，所以必须显式分流。
_FIN_KW = ('银行', '保险', '证券', '多元金融', '信托', '期货', '金融')
_fin_cache = {}


def is_financial(code):
    """是否金融业。返回 True/False；取不到板块归属时返回 None（未知）。"""
    if code in _fin_cache:
        return _fin_cache[code]
    try:
        rows = ds.board_of(code) or []
        names = ' '.join((r.get('name') or '') for r in rows if r.get('type') == '行业')
        v = any(k in names for k in _FIN_KW) if names else None
    except Exception:
        v = None
    _fin_cache[code] = v
    return v


# ------------------------------------------------------------------ 杜邦
def dupont(code, periods=6):
    """杜邦分解：ROE = 净利率 × 总资产周转率 × 权益乘数（全部年化）

    返回 list[dict]，按报告期倒序。每期：
      roe / net_margin / turnover / leverage / days / annualized

    ⚠️ 三个坑：
      ① 累计口径 → 分子分母都要年化，否则周转率偏低 25%
      ② 周转率用**平均总资产**（期初+期末)/2，不是期末
      ③ 权益乘数 = 总资产/净资产；净资产为负（资不抵债）时 ROE 无意义 → 标 None
    """
    t = load3(code, periods + 2)
    if not t['dates']:
        return []
    out = []
    for i, d in enumerate(t['dates'][:periods]):
        days = _days_of(d)
        rev = _g(d, t['I'], 'TOTAL_OPERATE_INCOME')
        npf = _g(d, t['I'], 'PARENT_NETPROFIT')
        if rev is None and npf is None:
            continue
        ta = _g(d, t['B'], 'TOTAL_ASSETS')
        te = _g(d, t['B'], 'TOTAL_EQUITY')
        # 平均总资产：需要上一期
        ta_prev = None
        if i + 1 < len(t['dates']):
            ta_prev = _g(t['dates'][i + 1], t['B'], 'TOTAL_ASSETS')
        ta_avg = ((ta + ta_prev) / 2) if (ta is not None and ta_prev is not None) else ta

        ann = 360.0 / days if days else 1.0
        roe = _div(npf * ann, te)
        margin = _div(npf, rev)
        turn = _div(rev * ann, ta_avg)
        lev = _div(ta, te) if (te and te > 0) else None
        out.append({
            'report': d, 'days': days, 'annualized': days != 360,
            'rev': rev, 'np': npf, 'assets': ta, 'equity': te,
            'roe': round(roe * 100, 2) if roe is not None else None,
            'net_margin': round(margin * 100, 2) if margin is not None else None,
            'turnover': round(turn, 3) if turn is not None else None,
            'leverage': round(lev, 2) if lev is not None else None,
            'warn': ('净资产 ≤ 0 → 权益乘数与 ROE 无意义' if (te is not None and te <= 0) else None),
        })
    return out


# ------------------------------------------------------------------ 三表勾稽
def three_stmt(code, periods=6):
    """三表勾稽：利润、现金、营运资本之间是否自洽。

    四个判据（每个都给出「数值 + 解读」）：
      ① **现金含量** = OCF / 净利润。持续 <0.8 → 利润没变成现金
      ② **应收增速 − 营收增速**。>+15pp → 收入可能靠赊销堆出来
      ③ **存货增速 − 营收增速**。>+20pp → 备货过度或产品滞销
      ④ **应计项目占比** = (净利润 − OCF) / 总资产。绝对值大 → 利润质量差
    """
    t = load3(code, periods + 2)
    if not t['dates']:
        return {'ok': False, 'msg': '三表取不到'}
    fin = is_financial(code)
    rows = []
    for i, d in enumerate(t['dates'][:periods]):
        days = _days_of(d)
        ann = 360.0 / days if days else 1.0
        rev = _g(d, t['I'], 'TOTAL_OPERATE_INCOME')
        npf = _g(d, t['I'], 'PARENT_NETPROFIT')
        ocf = _g(d, t['C'], 'NETCASH_OPERATE')
        ar = _g(d, t['B'], 'ACCOUNTS_RECE')
        iv = _g(d, t['B'], 'INVENTORY')
        ta = _g(d, t['B'], 'TOTAL_ASSETS')

        prev = t['dates'][i + 1] if i + 1 < len(t['dates']) else None
        # 同比要用「去年同期」，不是上一期（上一期是季度环比，含季节性）
        yoy_key = None
        try:
            y = int(d[:4]) - 1
            cand = '%d-%s' % (y, d[5:])
            if cand in t['I']:
                yoy_key = cand
        except Exception:
            pass

        def _yoy(cur, key):
            if cur is None or yoy_key is None:
                return None
            base = _g(yoy_key, t['I'] if key == 'I' else t['B'], 'TOTAL_OPERATE_INCOME')
            return None

        rev_yoy = ar_yoy = iv_yoy = None
        # ★ 小基数保护（2026-11-13 实测踩到）：
        #   应收/存货本来就极小的公司（如高端白酒应收≈0），同比会算出 **+1327%**
        #   这种天文数字 —— 它不是「收入靠赊销」，是**分母太小**。
        #   判据：上年同期科目金额 < 上年营收的 0.5% → 百分比无经济意义，直接置 None。
        MIN_BASE = 0.005
        if yoy_key:
            r0 = _g(yoy_key, t['I'], 'TOTAL_OPERATE_INCOME')
            a0 = _g(yoy_key, t['B'], 'ACCOUNTS_RECE')
            i0 = _g(yoy_key, t['B'], 'INVENTORY')
            rev_yoy = ((rev / r0 - 1) * 100) if (rev and r0) else None
            if ar is not None and a0 and r0 and (a0 / r0) >= MIN_BASE:
                ar_yoy = (ar / a0 - 1) * 100
            if iv is not None and i0 and r0 and (i0 / r0) >= MIN_BASE:
                iv_yoy = (iv / i0 - 1) * 100

        cash_q = _div(ocf * ann, npf * ann) if (ocf is not None and npf) else None
        accr = _div((npf * ann - ocf * ann), ta) if (npf is not None and ocf is not None and ta) else None

        flags = []
        if fin is True:
            # 金融业不做这四条判据：应收/存货不存在，OCF 含存款波动
            if cash_q is not None and abs(cash_q) > 3:
                flags.append('金融业 OCF 含存款增减（现金含量 %.1f 倍），'
                             '**不可用工商企业标准解读**' % cash_q)
        else:
            if cash_q is not None and cash_q < 0.8:
                flags.append('现金含量 %.2f < 0.8：利润未充分转化为现金' % cash_q)
            if (ar_yoy is not None and rev_yoy is not None) and (ar_yoy - rev_yoy) > 15:
                flags.append('应收增速比营收快 %.0fpp：收入可能靠赊销' % (ar_yoy - rev_yoy))
            if (iv_yoy is not None and rev_yoy is not None) and (iv_yoy - rev_yoy) > 20:
                flags.append('存货增速比营收快 %.0fpp：备货/滞销风险' % (iv_yoy - rev_yoy))
            # ★ 只有**正应计**才是坏信号：应计 = (净利润 − OCF)/总资产，
            #   正值 = 利润没变成现金；**负值 = OCF 高于利润，是好事**。
            #   （2026-11-13 实测：写成 abs() 后，茅台 OCF 远高于利润被报「质量偏弱」，
            #    方向完全相反。同 P115 的「列名/判据方向搞反」是同一族。）
            if accr is not None and accr > 0.10:
                flags.append('正应计占总资产 %.1f%%：利润高于现金，质量偏弱' % (accr * 100))

        rows.append({
            'report': d, 'rev': rev, 'np': npf, 'ocf': ocf,
            'cash_quality': round(cash_q, 2) if cash_q is not None else None,
            'accrual_pct': round(accr * 100, 2) if accr is not None else None,
            'rev_yoy': round(rev_yoy, 1) if rev_yoy is not None else None,
            'ar_yoy': round(ar_yoy, 1) if ar_yoy is not None else None,
            'inv_yoy': round(iv_yoy, 1) if iv_yoy is not None else None,
            'flags': flags,
        })
    return {'ok': True, 'rows': rows}


# ------------------------------------------------------------------ 财务健康
def health(code, periods=4):
    """财务健康：偿债能力 + 杠杆 + 利息覆盖"""
    t = load3(code, periods + 2)
    if not t['dates']:
        return {'ok': False, 'msg': '三表取不到'}
    out = []
    for d in t['dates'][:periods]:
        cr = _g(d, t['B'], 'CURRENT_RATIO', scale=1)     # 已是比率，不除亿
        dr = _g(d, t['B'], 'DEBT_ASSET_RATIO', scale=1)  # 已是 %
        ta = _g(d, t['B'], 'TOTAL_ASSETS')
        te = _g(d, t['B'], 'TOTAL_EQUITY')
        cash = _g(d, t['B'], 'MONETARYFUNDS')
        op = _g(d, t['I'], 'OPERATE_PROFIT')
        fin = _g(d, t['I'], 'FINANCE_EXPENSE')
        days = _days_of(d)
        ann = 360.0 / days if days else 1.0
        icr = _div(op * ann, fin * ann) if (op is not None and fin and fin > 0) else None
        out.append({
            'report': d,
            'current_ratio': round(cr, 2) if isinstance(cr, (int, float)) else None,
            'debt_ratio': round(dr, 2) if isinstance(dr, (int, float)) else None,
            'interest_cover': round(icr, 1) if icr is not None else None,
            'cash': round(cash, 1) if cash is not None else None,
            'equity': round(te, 1) if te is not None else None,
        })
    return {'ok': True, 'rows': out}


# ------------------------------------------------------------------ 成长质量
def growth_quality(code, periods=6):
    """成长质量：营收 / 净利 / 经营现金流 三条增速是否同向

    核心判读：
      - **利润增速 >> 营收增速** → 靠费用压缩或非经常损益，不可持续
      - **营收增速 >> 现金流增速** → 增长没收到钱
      - 三者同向且现金流最快 → 最健康的增长
    """
    t = load3(code, periods + 3)
    if not t['dates']:
        return {'ok': False, 'msg': '三表取不到'}
    fin = is_financial(code)
    rows = []
    for d in t['dates'][:periods]:
        try:
            yoy = '%d-%s' % (int(d[:4]) - 1, d[5:])
        except Exception:
            yoy = None
        if yoy not in t['I']:
            continue
        r1, r0 = _g(d, t['I'], 'TOTAL_OPERATE_INCOME'), _g(yoy, t['I'], 'TOTAL_OPERATE_INCOME')
        n1, n0 = _g(d, t['I'], 'PARENT_NETPROFIT'), _g(yoy, t['I'], 'PARENT_NETPROFIT')
        c1, c0 = _g(d, t['C'], 'NETCASH_OPERATE'), _g(yoy, t['C'], 'NETCASH_OPERATE')
        g = lambda a, b: round((a / b - 1) * 100, 1) if (a is not None and b and b != 0) else None
        rev_g, np_g, ocf_g = g(r1, r0), g(n1, n0), g(c1, c0)

        tag = None
        if fin is True:
            # 金融业：OCF 含存款增减，用「现金流增速」判成长质量必然误导
            if None not in (rev_g, np_g):
                tag = ('营收 %.1f%% / 净利 %.1f%%（金融业：现金流增速不参与质量判读）'
                       % (rev_g, np_g))
        elif None not in (rev_g, np_g):
            if np_g - rev_g > 30:
                tag = '利润增速远超营收（%.0fpp）→ 需查是否靠费用压缩/非经常损益' % (np_g - rev_g)
            elif rev_g - (ocf_g if ocf_g is not None else rev_g) > 30:
                tag = '营收增速远超现金流 → 增长未收到现金'
            elif ocf_g is not None and ocf_g >= max(rev_g, np_g):
                tag = '现金流增速最快 → 增长质量较好'
        rows.append({'report': d, 'rev_yoy': rev_g, 'np_yoy': np_g,
                     'ocf_yoy': ocf_g, 'tag': tag})
    return {'ok': True, 'rows': rows}


# ------------------------------------------------------------------ 综合
def snapshot(code):
    """把上面四块拼成一份摘要（荐股模块直接用这个）"""
    dp = dupont(code, 1)
    t3 = three_stmt(code, 1)
    hl = health(code, 1)
    gw = growth_quality(code, 1)
    d0 = dp[0] if dp else {}
    r3 = (t3.get('rows') or [{}])[0] if t3.get('ok') else {}
    h0 = (hl.get('rows') or [{}])[0] if hl.get('ok') else {}
    g0 = (gw.get('rows') or [{}])[0] if gw.get('ok') else {}
    flags = list(r3.get('flags') or [])
    notes = []
    fin = is_financial(code)
    # ★ 资产负债率阈值只对工商企业成立。银行 90%+ 是**行业常态**（存款即负债），
    #   套用 70% 阈值会把所有银行一票打成红旗 —— 2026-11-13 实测踩到。
    if fin is True:
        notes.append('金融业：资产负债率/周转率/流动比率/应收存货等工商企业指标不适用')
    else:
        if h0.get('interest_cover') is not None and h0['interest_cover'] < 3:
            flags.append('利息保障 %.1f 倍 < 3：偿债压力偏大' % h0['interest_cover'])
        if h0.get('debt_ratio') is not None and h0['debt_ratio'] > 70:
            flags.append('资产负债率 %.0f%% > 70%%' % h0['debt_ratio'])
    return {
        'code': code, 'report': d0.get('report'), 'is_financial': fin,
        'roe': d0.get('roe'), 'net_margin': d0.get('net_margin'),
        'turnover': d0.get('turnover'), 'leverage': d0.get('leverage'),
        'cash_quality': r3.get('cash_quality'),
        'rev_yoy': g0.get('rev_yoy') or r3.get('rev_yoy'),
        'np_yoy': g0.get('np_yoy'),
        'current_ratio': h0.get('current_ratio'),
        'debt_ratio': h0.get('debt_ratio'),
        'interest_cover': h0.get('interest_cover'),
        'flags': flags,
        'notes': notes,
    }


def _main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    if not args:
        print(__doc__)
        return 2
    code = args[0]
    if '--json' in sys.argv:
        import json
        print(json.dumps({'dupont': dupont(code, 4), 'three_stmt': three_stmt(code, 4),
                          'health': health(code, 4), 'growth': growth_quality(code, 4)},
                         ensure_ascii=False, indent=2, default=str))
        return 0

    dp = dupont(code, 5)
    print('=== 杜邦分解（年化口径） ===')
    if not dp:
        print('  取不到')
    else:
        print('  %-12s %8s %8s %9s %8s' % ('报告期', 'ROE%', '净利率%', '周转率', '权益乘数'))
        for x in dp:
            f = lambda v: ('—' if v is None else v)
            print('  %-12s %8s %8s %9s %8s%s' % (
                x['report'], f(x['roe']), f(x['net_margin']), f(x['turnover']), f(x['leverage']),
                '  *年化' if x['annualized'] else ''))
            if x.get('warn'):
                print('    ⚠️ ' + x['warn'])

    t3 = three_stmt(code, 4)
    print('\n=== 三表勾稽 ===')
    if t3.get('ok'):
        for x in t3['rows']:
            f = lambda v: ('—' if v is None else v)
            print('  %-12s 现金含量 %-6s 应计 %-7s 营收 %-7s 应收 %-7s 存货 %s'
                  % (x['report'], f(x['cash_quality']), f(x['accrual_pct']),
                     f(x['rev_yoy']), f(x['ar_yoy']), f(x['inv_yoy'])))
            for f in x['flags']:
                print('    ⚠️ ' + f)
    else:
        print('  ' + t3.get('msg', ''))

    hl = health(code, 2)
    print('\n=== 财务健康 ===')
    if hl.get('ok'):
        for x in hl['rows']:
            f = lambda v: ('—' if v is None else v)
            print('  %-12s 流动比率 %-6s 资产负债率 %-8s 利息保障 %s'
                  % (x['report'], f(x['current_ratio']),
                     (('%.1f%%' % x['debt_ratio']) if x['debt_ratio'] is not None else '—'),
                     (('%.1f 倍' % x['interest_cover']) if x['interest_cover'] is not None else '—')))
    else:
        print('  ' + hl.get('msg', ''))

    gw = growth_quality(code, 4)
    print('\n=== 成长质量 ===')
    if gw.get('ok'):
        for x in gw['rows']:
            f = lambda v: ('—' if v is None else v)
            print('  %-12s 营收 %-7s 净利 %-7s 现金流 %-7s %s'
                  % (x['report'], f(x['rev_yoy']), f(x['np_yoy']), f(x['ocf_yoy']),
                     x['tag'] or ''))
    else:
        print('  ' + gw.get('msg', ''))
    return 0


if __name__ == '__main__':
    sys.exit(_main())
