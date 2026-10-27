"""factor_report.py — 个股报告的「多因子分析」章节。

=== 设计原则 ===
1. **只用做过检验的因子**：内置一份 IC 表（来源见 VALIDITY_SRC），
   未过 Bonferroni 校正的因子**不进报告** —— 免得把噪声当信号。
2. **分位口径必须写清（P84）**：这里给的是**该股自身近 N 日的历史分位**，
   **不是"全市场横截面分位"**。两者含义完全不同，混用就是 P84。
3. **有效性状态与数值同屏（P88）**：每个因子都带它的 IC / t 值 / 判定档位，
   不允许"只给因子值不给可靠性"。
4. **这不是方向判断**：因子 IC 是**横截面**统计（"同一天里谁比谁强"），
   用在单票上只能说明"该票在该维度上处于自己历史的什么位置"，**不构成买卖建议**。

用法：
    python factor_report.py 000973          # 打印文本版
    from factor_report import snapshot, html_block
"""
import io, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ds
import factors as FA

VALIDITY_SRC = ('固定池 60 只（主板/非ST，固定种子抽样）+ 逐时点价格过滤(≤15元) · '
                '500 个交易日 · 持有期 5 日 · Bonferroni 校正 |t|≥3.43（84 项检验）')

# ---- 内置 IC 表：`因子名|类别|方向修正IC|t值`（仅收录**过校正**的）----
_IC_RAW = """
amt_log|量能|-0.1050|-11.15
limit_up20|涨停|-0.0877|-9.61
rs20|相对强度|-0.0746|-8.44
atr_ratio|风险|+0.0718|+8.43
mom20|动量|-0.0745|-8.41
vol_cv20|量能|+0.0618|+8.25
vol_hi_ratio|量能|-0.0674|-8.10
vr|量能|-0.0666|-7.68
trend_slope|趋势|-0.0687|-7.61
dist_ma60|趋势|+0.0732|+7.59
bull|均线|-0.0637|-7.46
devma20|均线|+0.0693|+7.44
turn_ratio|资金|+0.0604|+7.16
pos250|位置|+0.0605|+6.94
alpha20|相对强度|-0.0599|-6.94
dist_lo20|位置|-0.0638|-6.81
above20|均线|-0.0556|-6.69
ret_skew20|波动|-0.0458|-6.48
turnover|量能|+0.0701|+6.33
amt_ratio|量价|-0.0527|-6.26
days_since_lu|涨停|-0.0595|-6.11
new_high60|位置|-0.0540|-5.28
pctb|摆动|+0.0545|+6.05
rsi14|摆动|+0.0520|+5.95
mom10|动量|-0.0560|-5.91
rs60|相对强度|-0.0560|-5.88
ff_up_ratio|资金|-0.0467|-5.78
mfi14|量价|+0.0469|+5.73
cci20|摆动|+0.0491|+5.52
streak|涨停|-0.0525|-3.62
ff5_ratio|资金|-0.0465|-5.45
ma_align|均线|-0.0447|-5.31
range_ratio|波动|+0.0433|+5.21
rs5|相对强度|-0.0507|-5.20
mom5|动量|-0.0507|-5.20
hh_days|位置|-0.0475|-5.15
new_high20|位置|-0.0436|-4.86
consec_up|形态|+0.0431|+4.81
ff3_ratio|资金|-0.0410|-4.81
dist_hi250|位置|-0.0472|-4.67
ff_streak|资金|-0.0361|-4.64
devma5|均线|+0.0451|+4.62
ma20_slope|趋势|-0.0429|-4.57
vr5|量能|-0.0360|-4.41
wr14|摆动|-0.0375|-4.24
vol20|波动|+0.0504|+4.22
vp_corr|量价|-0.0302|-4.15
atr14|波动|+0.0516|+4.12
pos60|位置|+0.0341|+4.10
gap_up20|形态|-0.0314|-3.92
body_ratio|形态|-0.0322|-3.84
vol_ratio_sl|波动|+0.0304|+3.65
body|形态|-0.0370|-3.63
"""

IC_TABLE = {}
for _ln in _IC_RAW.strip().split('\n'):
    _n, _c, _ic, _t = _ln.split('|')
    IC_TABLE[_n] = {'cat': _c, 'ic': float(_ic), 't': float(_t)}

# 报告展示顺序（挑人读得懂的、覆盖各维度）
SHOW = [
    ('pos250', '250日位置', '%'), ('dist_ma60', '偏离MA60', '%'),
    ('rsi14', 'RSI14', ''), ('pctb', 'BOLL %B', ''), ('cci20', 'CCI20', ''),
    ('vr', '量比', ''), ('turnover', '换手率', '%'), ('amt_log', '成交额(对数)', ''),
    ('vol_cv20', '量能变异', ''), ('vol_hi_ratio', '量能/60日峰值', ''),
    ('rs20', '20日相对大盘', '%'), ('alpha20', '20日超额', '%'),
    ('ma_align', '均线多头度', ''), ('devma20', '偏离MA20', '%'),
    ('atr14', 'ATR%', ''), ('atr_ratio', '波动放大倍数', ''),
    ('ret_skew20', '20日收益偏度', ''), ('mfi14', 'MFI14', ''),
    ('ff5_ratio', '5日资金强度', ''), ('ff_up_ratio', '净流入天数占比', ''),
    ('limit_up20', '20日涨停次数', ''), ('days_since_lu', '距上次涨停', '日'),
    ('new_high60', '创60日新高', ''), ('consec_up', '连续阳线', '日'),
]


def snapshot(code, days=250):
    """算出该股的多因子快照。返回 dict；数据不足返回 None。"""
    k = ds.kline_qq(code, 400)
    if not k or len(k) < 90:
        return None
    fin = ds.financials(code)
    fl = {}
    for x in ds.moneyflow_sina(code, 300):
        if x[0]:
            fl[x[0]] = {'net': x[1], 'r0': x[2], 'turnover': x[5] / 100.0}
    ik = ds.kline_qq('sh000001', 400)
    ix = {x['d']: x['c'] for x in ik} if ik else {}

    today = time.strftime('%Y-%m-%d')
    last = len(k) - 1 - (1 if k[-1]['d'] == today else 0)

    # 逐日算因子（用于自身历史分位）
    hist, cur = [], None
    for i in range(max(60, last - days), last + 1):
        f, _ = FA.daily_factors(k, i, fin, None, fl, ix)
        if not f:
            continue
        hist.append((k[i]['d'], f))
        if i == last:
            cur = f
    if not cur or not hist:
        return None

    items = []
    for nm, label, unit in SHOW:
        v = cur.get(nm)
        if v is None:
            continue
        vals = [f.get(nm) for _, f in hist if f.get(nm) is not None]
        vv = [x for x in vals if isinstance(x, (int, float))]
        if not vv:
            continue
        # ★ v1.2.30：**按因子类型分支，二值/离散不算分位**
        #   2026-10-27 实测：`new_high60` 在 250 天里唯一值只有 0（从未创新高），
        #   却算出「分位 99%」—— 二值/单值变量的分位**没有意义**，
        #   读者会误读成"99% 的历史样本都比它低"，进而以为这是强信号。
        uniq = sorted({round(x, 6) for x in vv})
        nu = len(uniq)
        kind = 'bin' if nu <= 2 else ('disc' if nu <= 8 else 'cont')
        pctile = (sum(1 for x in vv if x <= v) / len(vv) * 100.0) if kind == 'cont' else None
        ic = IC_TABLE.get(nm, {})
        items.append({
            'name': nm, 'label': label, 'value': v, 'unit': unit,
            'pctile': pctile, 'n': len(vv), 'kind': kind,
            'uniq_n': nu, 'uniq': uniq if kind != 'cont' else None,
            'ic': ic.get('ic'), 't': ic.get('t'), 'cat': ic.get('cat', '—'),
        })
    return {'code': code, 'date': k[last]['d'], 'close': k[last]['c'],
            'items': items, 'days': days, 'src': VALIDITY_SRC}


def _f(v, unit):
    if v is None:
        return '—'
    if unit == '%':
        return '%.2f%%' % v
    if isinstance(v, float):
        return '%.3f' % v if abs(v) < 100 else '%.1f' % v
    return str(v)


# 维度分组（用于「汇总解读」按维度归拢，而不是罗列 22 个数）
_GROUPS = [
    ('位置', ['pos250', 'dist_hi250', 'dist_lo20', 'new_high60', 'dist_ma60', 'pos60']),
    ('波动', ['atr14', 'atr_ratio', 'vol_cv20', 'ret_skew20', 'range_ratio', 'vol20']),
    ('动量', ['rsi14', 'pctb', 'cci20', 'mfi14', 'mom5', 'mom10', 'mom20', 'wr14']),
    ('量能', ['vr', 'vr5', 'turnover', 'amt_ratio', 'vol_hi_ratio']),
    ('资金', ['ff3_ratio', 'ff5_ratio', 'ff_up_ratio', 'turnover']),
    ('相对强度', ['rs5', 'rs20', 'rs60', 'alpha20']),
    ('均线形态', ['bull', 'above20', 'ma_align', 'devma20', 'consec_up']),
    ('涨停结构', ['limit_up20', 'days_since_lu']),
]


def interpret(s):
    """把因子快照**机械汇总**成几条要点。返回 `[(维度, 描述), …]`。

    ⚠️ **这是机械汇总，不是方向判断**。它只做一件事：
      把分位极端的因子按维度归拢成一句话，方便人快速定位"这只票现在长什么样"。
      它**不预测涨跌**：因子的 IC 是横截面统计，单票上无法据此推出方向。

    判据（刻意简单、可复核）：某维度内**分位 ≤25% 或 ≥75%** 的多数因子决定该维度的描述。
    """
    if not s or not s['items']:
        return []
    by = {it['name']: it for it in s['items']}
    out = []
    for gname, names in _GROUPS:
        got = [by[n] for n in names if n in by]
        if not got:
            continue
        got = [it for it in got if it.get('pctile') is not None]   # ★ 跳过二值/离散
        if not got:
            continue
        ps = [it['pctile'] for it in got]
        avg = sum(ps) / len(ps)
        lo = sorted(got, key=lambda x: x['pctile'])[:2]
        hi = sorted(got, key=lambda x: -x['pctile'])[:2]
        if avg <= 30:
            tone, tag = 'low', '偏低'
        elif avg >= 70:
            tone, tag = 'high', '偏高'
        else:
            tone, tag = 'mid', '居中'
        detail = '、'.join('%s %.0f%%' % (it['label'], it['pctile'])
                          for it in (lo if tone == 'low' else hi if tone == 'high' else got[:2]))
        out.append((gname, tag, detail, tone, avg))
    # 排序：先按"偏离中位程度"降序，极端维度自然浮到最前
    out.sort(key=lambda x: -abs(x[4] - 50))
    return out


def html_interpret(s):
    """渲染汇总解读块：一句总述 + 分维度明细。"""
    pts = interpret(s)
    if not pts:
        return ('<p class="tiny">各维度分位均在中间区间，'
                '<b>无明显的极端读数</b>——即"这只票在多因子视角下形态平淡"。</p>')
    lows = [p for p in pts if p[3] == 'low']
    highs = [p for p in pts if p[3] == 'high']
    mids = [p for p in pts if p[3] == 'mid']
    # 一句总述
    bits = []
    if lows:
        bits.append('「%s」偏低' % '、'.join(x[0] for x in lows[:2]))
    if highs:
        bits.append('「%s」偏高' % '、'.join(x[0] for x in highs[:2]))
    if not bits:
        lead = ('各维度分位均落在中间区间，<b>形态平淡、无明显极值</b>。')
    else:
        lead = ('该股当前：%s%s。' % ('；'.join(bits),
                '，其余维度居中' if mids else '，无维度居中'))
    li = ''.join('<li><b>%s</b>（%s）：%s</li>' % (g, t, d) for g, t, d, _tn, _a in pts[:8])
    return ('<p><b>机械汇总</b>（按维度归拢分位，<b>不预测涨跌</b>）：%s</p><ul>%s</ul>'
            '<p class="tiny">⚠️ 本汇总只做一件事：把 22 个因子的分位按维度归类，'
            '帮你快速看清"这只票现在处于什么状态"。'
            '<b>它不是方向判断，也推不出买卖结论</b> —— '
            '因子的统计意义在<b>横截面排序能力</b>（见下表 IC），不在单票读数的绝对高低。</p>' % (lead, li))


def html_table(s):
    """把 snapshot 渲染成 HTML 片段（供 report.py 的 SC() 卡片用）。"""
    if not s:
        return '<p>数据不足，无法做多因子分析。</p>'
    head = html_interpret(s)
    rows = []
    for it in s['items']:
        val = _f(it['value'], it['unit'])
        p = it['pctile']
        _kd = it.get('kind', 'cont')
        if _kd == 'bin':
            # ★ 二值因子：显示"是/否"，不算分位（分位对 0/1 变量无意义）
            _vv = it.get('value')
            head = '是' if _vv else '否'
            cell = ('<span style="color:#6b7280">二值 · %s</span>' % head)
        elif _kd == 'disc':
            cell = ('<span style="color:#6b7280">离散 · %d 档</span>' % (it.get('uniq_n') or 0))
        else:
            if p >= 95:
                cell = '%.0f%% <span style="color:#c0392b">◆极值高</span>' % p
            elif p <= 5:
                cell = '%.0f%% <span style="color:#1e7a46">◆极值低</span>' % p
            else:
                cell = '%.0f%%' % p
        if p is None:
            bar = '<span style="color:#a9b2be;font-size:11px">不分位</span>'
        else:
            bar = ('<span style="display:inline-block;height:6px;width:%.0fpx;'
                   'background:#A9843F;opacity:.55;vertical-align:middle"></span>'
                   '<span style="display:inline-block;height:6px;width:%.0fpx;'
                   'background:#c8ccd4;vertical-align:middle"></span>' % (p * 0.6, (100 - p) * 0.6))
        if it['ic'] is None:
            eff = '<span style="color:#8a8f98">未收录</span>'
        else:
            tone = '#c0392b' if it['ic'] > 0 else '#1e7a46'
            eff = ('<span style="color:%s;font-weight:600">IC %+.4f</span>'
                   ' <span style="color:#8a8f98">t=%+.2f</span>' % (tone, it['ic'], it['t']))
        rows.append('<tr><td><b>%s</b></td><td>%s</td><td>%s</td>'
                    '<td>%s<br>%s</td><td>%s</td></tr>'
                    % (it['label'], it['cat'], val, cell, bar, eff))
    html = [head, '<table><tr><th>因子</th><th>类别</th><th>当前值</th>'
            '<th>自身历史分位</th><th>市场有效性（已过校正）</th></tr>']
    html += rows
    html.append('</table>')
    html.append(
        '<p style="font-size:12px;color:#6b7280;line-height:1.7">'
        '① <b>「自身分位」是该股近 %d 日的自身历史分位，不是全市场横截面分位</b>（两者含义不同，见 P84）；'
        '<br>② IC 为<b>横截面</b>统计（衡量"同一天里谁比谁强"），用在单票上只能说明'
        '<b>该票在该维度处于自己历史的什么位置</b>，<b>不构成方向判断，更不是买卖建议</b>；'
        '<br>③ 本节只列<b>已过 Bonferroni 校正</b>的因子；有效性来源：%s。'
        '</p>' % (s['days'], s['src']))
    return ''.join(html)


def text_report(code):
    s = snapshot(code)
    if not s:
        return '数据不足，无法做多因子分析。'
    L = []
    L.append('【多因子分析】%s  信号日 %s  收盘 %.2f' % (s['code'], s['date'], s['close']))
    L.append('  有效性来源：%s' % s['src'])
    L.append('  %-14s %-12s %10s %10s   %s' % ('因子', '读数', '当前值', '自身分位', '市场有效性'))
    for it in s['items']:
        val = _f(it['value'], it['unit'])
        ic = ('IC%+.4f t=%+.2f' % (it['ic'], it['t'])) if it['ic'] is not None else '未收录'
        _pp = it.get('pctile')
        L.append('  %-14s %-12s %10s %9s   %s' % (it['label'], it['cat'], val,
                 ('%.0f%%' % _pp) if _pp is not None else '—(不分位)', ic))
    L.append('')
    L.append('  ⚠️ 「自身分位」= 该股近 %d 日**自身历史**分位，**不是全市场横截面分位**（P84）。' % s['days'])
    L.append('  ⚠️ IC 为**横截面**统计（同一天里谁比谁强），单票上仅表示"处于自身历史的什么位置"，')
    L.append('      **不构成方向判断，更不是买卖建议**。')
    return '\n'.join(L)


if __name__ == '__main__':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    code = sys.argv[1] if len(sys.argv) > 1 else '000973'
    print(text_report(code))
