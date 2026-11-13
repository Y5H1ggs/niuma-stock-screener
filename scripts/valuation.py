# -*- coding: utf-8 -*-
"""valuation.py — 估值建模：历史分位 / 相对估值 / DCF / 反向 DCF / PEG

## 这个模块的定位

回答三个问题，且**只回答这三个**：

1. **现在贵不贵**——当前 PE/PB 在该股自己的历史上处于什么位置（分位）
2. **比同行贵还是便宜**——同行业成分的估值中位数对比
3. **当前价格隐含了什么预期**——反向 DCF 倒推市场要求的增长率

## 刻意不做的事

- ❌ **不给目标价**。DCF 输出的是「在一组假设下的内在价值」，假设变了结论就变。
  所以 DCF 一律输出**敏感性表**，而不是单点数值。
- ❌ **不用模型预测股价**。估值分位是描述性的：它说「历史上这个位置后续如何」，
  不说「所以现在会涨」。
- ❌ **不把亏损股的 PE 当低估值**。PE ≤ 0 直接标记为「不适用」，不参与分位。

## 口径（踩过的坑，见 pitfalls）

- **eps 是累计口径**（年初至今）。TTM = 最新累计 + (上年年报 − 上年同期累计)。
  直接把 Q3 的 eps 当年度用，会**高估 ROE 约 1/3**（P128）。
- **历史分位必须用 PIT**：每个报告期的 PE 用该期**实际公告日之后**的股价算，
  不能用今天的价格回填历史（否则分位是假的）。
- 亏损期（eps ≤ 0）不进 PE 序列——负 PE 排序会把「越亏越便宜」算成低估。

用法
----
    python valuation.py 600000
    python valuation.py 600000 --dcf
    python valuation.py 600000 --json
"""
import io
import sys
import os
import math

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ds  # noqa: E402


# ------------------------------------------------------------------ TTM
def ttm(fins):
    """把「累计口径」的财报序列转成 TTM 序列。

    返回 list[dict]，按 report 倒序，每项：
      {'report','notice','eps_ttm','rev_ttm','np_ttm','bps','roe_ttm'}

    ⚠️ 累计口径的坑（P128）：
      `RPT_LICO_FN_CPD` 的 EPS / 营收 / 净利都是**年初至今累计**。
      2026Q3 的 eps=1.17 是前 9 个月，不是 Q3 单季。
      直接拿它算 PE，会把分母放大 1.33 倍 → PE 系统性偏低。
    """
    if not fins:
        return []
    by = {x['report']: x for x in fins}

    def _prev_year(d):
        """'2026-09-30' -> '2025-09-30'"""
        y, md = d.split('-', 1)
        return '%d-%s' % (int(y) - 1, md)

    def _annual(d):
        """'2026-09-30' -> '2025-12-31'（上一个完整年度）"""
        y = int(d.split('-')[0])
        return '%d-12-31' % (y - 1)

    out = []
    for x in fins:
        d = x['report']
        eps, rev, npf = x.get('eps'), x.get('revenue'), x.get('netprofit')
        py_same, py_ann = by.get(_prev_year(d)), by.get(_annual(d))

        def _ttm(cur, same, ann):
            """TTM = 本期累计 + (上年年报 − 上年同期累计)"""
            if cur is None:
                return None
            if same is None or ann is None:
                return cur          # 缺同期数据 → 退回累计（并在 warn 里标注）
            return cur + (ann - same)

        out.append({
            'report': d, 'notice': x.get('notice'),
            'eps_ttm': _ttm(eps, (py_same or {}).get('eps'), (py_ann or {}).get('eps')),
            'rev_ttm': _ttm(rev, (py_same or {}).get('revenue'), (py_ann or {}).get('revenue')),
            'np_ttm': _ttm(npf, (py_same or {}).get('netprofit'), (py_ann or {}).get('netprofit')),
            'bps': x.get('bps'), 'roe': x.get('roe'),
            'raw_eps': eps, 'raw_rev': rev,
            'ttm_complete': (py_same is not None and py_ann is not None),
        })
    return out


# ------------------------------------------------------------------ 估值序列
def _price_after(bars, date):
    """取 date 当日或之后第一个交易日的收盘价（PIT）"""
    for b in bars:
        if b['d'] >= date:
            return b['c']
    return None


def val_band(code, years=3, name=None):
    """历史估值分位：当前 PE/PB 在该股自己历史中的位置。

    返回 dict；取不到返回 {}（**不猜、不填 0**）。

    关键口径：
      - 每期 PE = 该期**公告日之后**的股价 / 该期 TTM EPS  ← PIT，不可用今日价回填
      - 亏损期（eps_ttm ≤ 0）**移出序列**，不参与分位
      - 分位 = 当前值在序列中的百分位；PE 分位低 = 相对自身历史便宜
    """
    fins = ds.financials(code, page_size=40)
    if not fins:
        return {}
    ser = ttm(fins)

    # 3 年日线（约 750 交易日，取 900 留余量）
    bars = ds.kline_list(code, 900) or []
    if len(bars) < 60:
        return {}
    cutoff = bars[-1]['d'][:4]
    try:
        cutoff = str(int(cutoff) - years) + bars[-1]['d'][4:]
    except Exception:
        cutoff = bars[0]['d']

    snap = ds.snapshot([code]).get(code) or {}
    price = snap.get('price')
    if not price:
        price = bars[-1]['c']

    pe_list, pb_list, details = [], [], []
    for r in ser:
        if not r.get('notice') or r['report'] < cutoff:
            continue
        px = _price_after(bars, r['notice'])
        if not px:
            continue
        eps, bps = r.get('eps_ttm'), r.get('bps')
        pe = (px / eps) if (eps and eps > 0) else None
        pb = (px / bps) if (bps and bps > 0) else None
        if pe:
            pe_list.append(pe)
        if pb:
            pb_list.append(pb)
        details.append({'report': r['report'], 'notice': r['notice'],
                        'px': round(px, 3), 'eps_ttm': round(eps, 4) if eps else None,
                        'pe': round(pe, 2) if pe else None,
                        'pb': round(pb, 2) if pb else None})

    # 当前值（用最新一期 TTM）
    cur = ser[0] if ser else {}
    cur_eps, cur_bps = cur.get('eps_ttm'), cur.get('bps')
    pe_now = round(price / cur_eps, 2) if (cur_eps and cur_eps > 0) else None
    pb_now = round(price / cur_bps, 2) if (cur_bps and cur_bps > 0) else None

    def _pct(v, arr):
        if v is None or not arr:
            return None
        k = sum(1 for x in arr if x <= v)
        return round(k / len(arr) * 100, 1)

    def _band(arr):
        if len(arr) < 3:
            return None
        a = sorted(arr)
        n = len(a)
        return {'p10': round(a[int(n * .1)], 2), 'p25': round(a[int(n * .25)], 2),
                'p50': round(a[n // 2], 2), 'p75': round(a[int(n * .75)], 2),
                'p90': round(a[min(n - 1, int(n * .9))], 2)}

    warn = []
    if not cur.get('ttm_complete'):
        warn.append('缺上年同期/年报数据 → TTM 退回累计口径，PE 会偏低')
    if len(pe_list) < 8:
        warn.append('有效 PE 样本仅 %d 期（<8），分位不可靠' % len(pe_list))
    if pe_now is None:
        warn.append('当前 EPS(TTM) ≤ 0 → PE 不适用（亏损股无 PE 意义）')

    return {
        'code': code, 'name': name or snap.get('name'), 'price': price,
        'eps_ttm': round(cur_eps, 4) if cur_eps else None,
        'bps': round(cur_bps, 4) if cur_bps else None,
        'report': cur.get('report'), 'notice': cur.get('notice'),
        'pe_ttm': pe_now, 'pb': pb_now,
        'pe_pct': _pct(pe_now, pe_list), 'pb_pct': _pct(pb_now, pb_list),
        'pe_band': _band(pe_list), 'pb_band': _band(pb_list),
        'n_pe': len(pe_list), 'n_pb': len(pb_list),
        'series': details[-8:],
        'warn': warn,
    }


# ------------------------------------------------------------------ 相对估值
def rel_val(code, bk=None):
    """同行业相对估值：本公司 PE/PB vs 行业中位数。

    `bk` 不传时自动用 `ds.board_of(code)` 推断。
    **归属可能不准**（一只票常属于多个板块），报告里必须写清用的是哪个板块。
    """
    if not bk:
        try:
            rows = ds.board_of(code) or []
            # 返回的是 dict 列表（不是元组）：[{'bk','name','type','level'}, ...]
            # 优先「二级行业」：一级太宽（如「银行」含全部银行），三级成分太少算不动中位数
            ind = [r for r in rows if r.get('type') == '行业']
            pick = None
            for want in ('2', '1', '3'):
                c = [r for r in ind if str(r.get('level')) == want]
                if c:
                    pick = c[0]
                    break
            if pick is None and rows:
                pick = rows[0]
            bk = (pick or {}).get('bk')
        except Exception:
            bk = None
    if not bk:
        return {'ok': False, 'msg': '板块归属取不到'}

    chk = ds.board_check(bk)
    if not chk.get('ok') and chk.get('name') is None:
        return {'ok': False, 'msg': chk.get('msg')}

    mem = ds.sector_members(bk, pages=3) or []
    if not mem:
        return {'ok': False, 'msg': '板块 %s 成分取不到' % bk, 'bk': bk}

    # ★ 不拿 `members_usable` 当门槛：它只认 f9/f23 这两个**字段名**，
    #   而批量接口的 PE 藏在 `_days[0].PE_DYNAMIC` 里 —— 用它判定会得到
    #   「返回 42 条但字段全空」的假结论。**判据要按「提取到的有效值个数」，
    #   不按「某个字段名有没有值」。**
    def _pe_of(x):
        v = x.get('f9')
        if isinstance(v, (int, float)) and v > 0:
            return v / 100.0, 'push2:f9'
        days = x.get('_days')
        if isinstance(days, list) and days:
            v = days[0].get('PE_DYNAMIC')
            if isinstance(v, (int, float)) and v > 0:
                return float(v), 'dce:PE_DYNAMIC'
        return None, None

    def _collect(rs):
        vals, srcs = [], set()
        for x in rs:
            pe, src = _pe_of(x)
            if pe:
                vals.append(pe)
                srcs.add(src)
        return vals, srcs

    pes, src_used = _collect(mem)
    if len(pes) < 3:
        codes = [x.get('f12') for x in mem if x.get('f12')]
        if codes:
            u = ds.ulist(codes, fields='f12,f14,f9,f23') or {}
            pes2, src2 = _collect(list(u.values()))
            if len(pes2) > len(pes):
                pes, src_used = pes2, src2

    own = ds.ulist([code], fields='f9,f23,f12,f14').get(code) or {}
    my_pe, _s = _pe_of(own)
    my_pb = (own.get('f23') / 100.0) if isinstance(own.get('f23'), (int, float)) else None

    if len(pes) < 3:
        return {'ok': False, 'bk': bk,
                'msg': '板块 %d 只成分里只提取到 %d 个有效 PE，样本不足' % (len(mem), len(pes))}

    # PB：批量接口拿不到（dce 路径无 PB），且逐只请求成本过高
    #   → **明确声明不可用**，而不是偷偷少给一个维度
    own = ds.ulist([code], fields='f9,f23,f12,f14').get(code) or {}
    my_pe, _s = _pe_of(own)
    my_pb = (own.get('f23') / 100.0) if isinstance(own.get('f23'), (int, float)) else None
    if not pes:
        return {'ok': False, 'bk': bk,
                'msg': '板块 %d 只成分均取不到可用 PE（两路字段都没有）' % len(rows)}

    def _med(a):
        if not a:
            return None
        a = sorted(a)
        return round(a[len(a) // 2], 2)

    med_pe, med_pb = _med(pes), None
    return {
        'ok': True, 'bk': bk, 'bk_name': chk.get('name'),
        'n': len(pes),
        'pe': my_pe, 'pe_med': med_pe, 'pe_src': '+'.join(sorted(src_used)),
        'pb': my_pb, 'pb_med': None,
        'pe_vs': round(my_pe / med_pe, 2) if (my_pe and med_pe) else None,
        'pb_vs': None,
        'pe': round(my_pe, 2) if my_pe else None,
        'msg': '%s：PE %.2f（东财动态口径）vs 行业中位 %.2f（同口径，%d 只，源 %s）；'
               'PB 板块中位不可用（批量接口无此字段）'
               % (chk.get('name'), my_pe or 0, med_pe or 0, len(pes),
                  '+'.join(sorted(src_used))),
        'note': '此处的 PE 是**动态口径**（最新季度年化），与上文 TTM PE 不同口径，不可混比；'
                '相对对比只在同口径内部成立',
    }


# ------------------------------------------------------------------ PEG
def peg(code):
    """PEG = PE(TTM) / 净利增速(%)。增速 ≤0 时 PEG 无意义 → 返回 None"""
    v = val_band(code)
    fins = ds.financials(code, page_size=8)
    if not v.get('pe_ttm') or not fins:
        return {'ok': False, 'msg': '缺 PE 或财报'}
    g = fins[0].get('np_yoy')
    if g is None or g <= 0:
        return {'ok': False, 'msg': '净利增速 ≤0（%s）→ PEG 不适用' % g, 'pe': v['pe_ttm']}
    return {'ok': True, 'pe': v['pe_ttm'], 'g': round(g, 2),
            'peg': round(v['pe_ttm'] / g, 2),
            'msg': 'PE %.2f / 增速 %.2f%% → PEG %.2f' % (v['pe_ttm'], g, v['pe_ttm'] / g)}


# ------------------------------------------------------------------ DCF
def dcf(code, g1=0.10, g2=0.03, wacc=0.09, years=5, margin=None):
    """两段式简化 DCF。**输出敏感性表，不输出单点目标价。**

    口径与免责：
      - FCF 用 `经营现金流净额 − 购建长期资产支付的现金`（近似自由现金流）
      - 前 `years` 年按 `g1` 增长，之后永续 `g2`
      - 明确标注：这是**假设驱动的算术**，不是预测。参数动 1pp 结论就大幅变化，
        所以必须看整张敏感性表，而不是抓一个数。
    """
    prof = ds.cashflow_attribution(code, periods=6)
    rows = (prof or {}).get('rows') or []
    if not rows:
        return {'ok': False, 'msg': '现金流数据取不到'}
    last = rows[0]
    ocf, capex = last.get('ocf'), last.get('capex')
    if ocf is None:
        return {'ok': False, 'msg': 'OCF 取不到'}
    fcf = (ocf - abs(capex or 0)) * 1e8 if abs(ocf) < 1e8 else (ocf - abs(capex or 0))
    # business_profile/cashflow 的金额单位是「亿」，统一到元
    if margin is not None:
        fcf *= margin
    if fcf <= 0:
        return {'ok': False, 'msg': 'FCF ≤ 0（%.2f 亿）→ DCF 不适用（亏损/重资产扩张期）' % fcf}

    v = ds.ulist([code], fields='f20,f12,f14').get(code) or {}
    mcap = (v.get('f20') or 0)
    shares = (mcap / (ds.snapshot([code]).get(code, {}).get('price') or 1)) if mcap else 0

    def _value(g1_, g2_, w_):
        pv, f = 0.0, fcf
        for t in range(1, years + 1):
            f *= (1 + g1_)
            pv += f / ((1 + w_) ** t)
        tv = f * (1 + g2_) / (w_ - g2_) if w_ > g2_ else None
        if tv is None:
            return None
        pv += tv / ((1 + w_) ** years)
        return pv

    grid = []
    for gg in (g1 - 0.04, g1 - 0.02, g1, g1 + 0.02, g1 + 0.04):
        line = []
        for ww in (wacc - 0.02, wacc - 0.01, wacc, wacc + 0.01, wacc + 0.02):
            val = _value(gg, g2, ww)
            line.append(round(val / 1e8, 1) if val else None)
        grid.append({'g1': round(gg * 100, 1), 'vals': line})

    base = _value(g1, g2, wacc)
    return {
        'ok': True,
        'fcf_yi': round(fcf / 1e8, 2),
        'g1': g1 * 100, 'g2': g2 * 100, 'wacc': wacc * 100, 'years': years,
        'base_value_yi': round(base / 1e8, 1) if base else None,
        'mcap_yi': round(mcap / 1e8, 1) if mcap else None,
        'upside': (round((base / mcap - 1) * 100, 1) if (base and mcap) else None),
        'wacc_axis': [(wacc + d) * 100 for d in (-0.02, -0.01, 0, 0.01, 0.02)],
        'grid': grid,
        'msg': 'FCF %.2f 亿；基准假设 g1=%.0f%%/g2=%.0f%%/WACC=%.0f%% → 内在价值 %.1f 亿 vs 市值 %.1f 亿'
               % (fcf / 1e8, g1 * 100, g2 * 100, wacc * 100,
                  (base or 0) / 1e8, (mcap or 0) / 1e8),
    }


def reverse_dcf(code, g2=0.03, wacc=0.09, years=5):
    """反向 DCF：当前市值隐含的前 5 年增长率是多少。

    为什么它比正向 DCF 有用：**正向 DCF 的结论几乎完全由 g1 决定**，
    而 g1 是拍的。反过来问「市场现在假设了多少增长」，这个答案不需要我猜。
    """
    prof = ds.cashflow_attribution(code, periods=6)
    rows = (prof or {}).get('rows') or []
    if not rows:
        return {'ok': False, 'msg': '现金流取不到'}
    last = rows[0]
    ocf, capex = last.get('ocf'), last.get('capex')
    fcf = (ocf or 0) - abs(capex or 0)
    if fcf <= 0:
        return {'ok': False, 'msg': 'FCF ≤ 0 → 反向 DCF 不适用'}
    v = ds.ulist([code], fields='f20').get(code) or {}
    mcap = (v.get('f20') or 0) / 1e8
    if not mcap:
        return {'ok': False, 'msg': '市值取不到'}

    def _val(g):
        pv, f = 0.0, fcf
        for t in range(1, years + 1):
            f *= (1 + g)
            pv += f / ((1 + wacc) ** t)
        tv = f * (1 + g2) / (wacc - g2) if wacc > g2 else None
        return None if tv is None else pv + tv / ((1 + wacc) ** years)

    lo, hi = -0.5, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if (_val(mid) or 0) < mcap:
            lo = mid
        else:
            hi = mid
    implied = (lo + hi) / 2 * 100
    return {'ok': True, 'implied_g1': round(implied, 1),
            'mcap_yi': round(mcap, 1), 'fcf_yi': round(fcf, 2),
            'msg': '当前市值隐含未来 %d 年 FCF 增速约 %.1f%%（永续 %.0f%%，WACC %.0f%%）'
                   % (years, implied, g2 * 100, wacc * 100)}


# ------------------------------------------------------------------ CLI
def _main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    if not args:
        print(__doc__)
        return 2
    code = args[0]
    as_json = '--json' in sys.argv

    v = val_band(code)
    if not v:
        print('  取不到估值数据（财报或 K 线缺失）')
        return 1

    if as_json:
        import json
        print(json.dumps(v, ensure_ascii=False, indent=2, default=str))
        return 0

    print('=== %s %s 估值分位 ===' % (code, v.get('name')))
    print('  最新价 %.2f | EPS(TTM) %s | BPS %s | 报告期 %s（公告 %s）'
          % (v['price'], v['eps_ttm'], v['bps'], v['report'], v['notice']))
    print('  PE(TTM) %s（历史分位 %s%%）| PB %s（历史分位 %s%%）'
          % (v['pe_ttm'], v['pe_pct'], v['pb'], v['pb_pct']))
    if v['pe_band']:
        b = v['pe_band']
        print('  PE 区间 P10-P90: %.1f / %.1f / %.1f / %.1f / %.1f（样本 %d 期）'
              % (b['p10'], b['p25'], b['p50'], b['p75'], b['p90'], v['n_pe']))
    for w in v['warn']:
        print('  ⚠️ ' + w)

    r = rel_val(code)
    print('\n=== 相对估值 ===')
    if r.get('ok'):
        print('  ' + r['msg'])
        if r.get('pe_vs'):
            print('  PE 相对行业中位 %.2fx（>1 溢价）' % r['pe_vs'])
    else:
        # ★ 失败必须说出来：静默跳过会让人以为「这只票没有相对估值」
        print('  ✗ 不可用：' + (r.get('msg') or '未知原因'))

    p = peg(code)
    print('\n=== PEG ===')
    print('  ' + p.get('msg', '取不到'))

    if '--dcf' in sys.argv:
        d = dcf(code)
        print('\n=== DCF（假设驱动，看敏感性表不要抓单点） ===')
        if not d.get('ok'):
            print('  ' + d.get('msg', ''))
        else:
            print('  ' + d['msg'])
            print('  %-8s' % 'g1\\WACC' + ''.join('%8.1f%%' % x for x in d['wacc_axis']))
            for row in d['grid']:
                print('  %7.1f%%' % row['g1']
                      + ''.join(('%8s' % ('%.0f' % x if x else '—')) for x in row['vals']))
            print('  （单位：亿元内在价值；市值 %.1f 亿）' % (d['mcap_yi'] or 0))
        rd = reverse_dcf(code)
        print('\n=== 反向 DCF（市场隐含预期） ===')
        print('  ' + rd.get('msg', ''))
    return 0


if __name__ == '__main__':
    sys.exit(_main())
