# -*- coding: utf-8 -*-
"""recommend.py — 荐股：三维度筛选 + 分位排名 + 红旗提示

## 先说清它「不是」什么（这一节比代码重要）

它**不是预测模型**。本项目实测 `model.py` 样本外 AUC 0.508~0.528、
Brier 全 ≥ 常数基线 → **模型不可用**，因此这里退回**可解释的规则筛选**。

它**不是打分黑箱**。上一版本曾手写一个 `score()`（`+3 if 多头排列…`），
权重全凭手感、未做规模标准化 —— 那是「换了个名字的 model.py」（P117）。
本模块改成：

| 层 | 做法 | 可解释性 |
|---|---|---|
| ① 硬门槛 | 主板 / 非 ST / 成交额 / PE>0 / 市值 | 每条都能一句话说清理由 |
| ② 分位排名 | 每个维度在本池内的百分位（0~100） | 相对可比，不是绝对好坏 |
| ③ 综合排序 | **各维度分位等权平均** | 权重**等权**是约定，不是拟合结果 |

**`composite` 是「本池内的相对排名分数」，不是预期收益率，不是上涨概率。**

## 三种模式

| 模式 | 看什么 | 适合 |
|---|---|---|
| `value` | PE/PB 历史分位低 + ROE 不为负 + 负债率可控 | 找「跌下来的、基本面没坏」 |
| `quality` | ROE 高 + 现金含量好 + 利息保障足 | 找「赚钱且赚到现金」 |
| `growth` | 营收/净利双增 + 增长质量（现金流跟得上） | 找「在长，且不是靠赊销」 |

## 三条硬性诚实声明

1. **样本是粗筛池，不是全市场。** 默认只用「成交额前 N」的票，
   余额可能更好但没进池 —— 输出里会写明池子大小。
2. **不判断股价方向。** 低估值可以更低，高 ROE 可以回落。
   本模块只回答「值不值得进一步研究」。
3. **红旗比亮点重要。** 每只候选都强制输出 `flags`；
   红旗 ≥2 条的直接降到观察池，不参与推荐。

用法
----
    python recommend.py --mode value
    python recommend.py --mode quality --limit 10
    python recommend.py --mode growth --codes 600000,600519
"""
import io
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ds          # noqa: E402
import valuation   # noqa: E402
import fundamentals  # noqa: E402

MAIN = ('60', '000', '001', '002', '003')


def _num(v, d=None):
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except Exception:
        return d


def coarse_pool(limit=120):
    """粗筛池：主板 + 成交额达标 + PE/PB 有值。

    ⚠️ `clist` 常被截断（拿不到全市场），所以这里是**降序的样本池**，
       不是全市场。调用方必须把 `pool_n` 写进报告。
    """
    rows = ds.clist(fid='f6', fs=ds.MARKET_MAIN, pages=2,
                    fields='f12,f14,f2,f3,f6,f9,f23,f20,f8,f10') or []
    out = []
    for x in rows:
        code = str(x.get('f12') or '')
        if not code.startswith(MAIN):
            continue
        name = x.get('f14') or ''
        if 'ST' in name or '退' in name:
            continue
        amt = _num(x.get('f6'), 0)
        if amt < 1.5e8:                      # 成交额 ≥1.5 亿
            continue
        pe = _num(x.get('f9'))
        pb = _num(x.get('f23'))
        mcap = _num(x.get('f20'), 0) / 1e8
        if pe is None or pe <= 0:            # 亏损股不进入价值/质量池
            continue
        if mcap < 30:                        # 市值 ≥30 亿，过滤微盘
            continue
        out.append({'code': code, 'name': name, 'price': _num(x.get('f2')),
                    'chg': _num(x.get('f3'), 0), 'amt': amt / 1e8,
                    'pe': pe, 'pb': pb, 'mcap': mcap,
                    'hs': _num(x.get('f8'), 0), 'lb': _num(x.get('f10'), 0)})
    out.sort(key=lambda z: -z['amt'])
    return out[:limit], len(rows)


def _pct_rank(vals):
    """把一组数转成 0~100 的分位（越大越靠前）。None 不参与。"""
    idx = [(i, v) for i, v in enumerate(vals) if v is not None]
    if not idx:
        return [None] * len(vals)
    s = sorted(idx, key=lambda z: z[1])
    n = len(s)
    res = [None] * len(vals)
    for rank, (i, _v) in enumerate(s):
        res[i] = round(rank / max(1, n - 1) * 100, 1)
    return res


def deep(code, name=None):
    """对单只做估值 + 财报深挖，合并成一个 dict"""
    v = valuation.val_band(code, name=name) or {}
    f = fundamentals.snapshot(code) or {}
    return {
        'code': code, 'name': name or v.get('name') or f.get('name'),
        'price': v.get('price'),
        'pe_ttm': v.get('pe_ttm'), 'pe_pct': v.get('pe_pct'), 'pb': v.get('pb'),
        'pb_pct': v.get('pb_pct'),
        'roe': f.get('roe'), 'net_margin': f.get('net_margin'),
        'cash_quality': f.get('cash_quality'), 'debt_ratio': f.get('debt_ratio'),
        'interest_cover': f.get('interest_cover'),
        'rev_yoy': f.get('rev_yoy'), 'np_yoy': f.get('np_yoy'),
        'is_financial': f.get('is_financial'),
        'flags': list(f.get('flags') or []) + list(v.get('warn') or []),
        'notes': list(f.get('notes') or []),
        'report': f.get('report') or v.get('report'),
    }


# 每个模式用哪些维度（方向：1 = 越大越好，-1 = 越小越好）
MODES = {
    'value': [('pe_pct', -1), ('pb_pct', -1), ('roe', 1), ('debt_ratio', -1)],
    'quality': [('roe', 1), ('cash_quality', 1), ('interest_cover', 1), ('debt_ratio', -1)],
    'growth': [('rev_yoy', 1), ('np_yoy', 1), ('cash_quality', 1), ('roe', 1)],
}


def screen(mode='value', limit=10, codes=None, gap=0.5):
    """筛选并排名。返回 {'pool_n','deep_n','rows':[...],'mode','note'}"""
    if mode not in MODES:
        return {'ok': False, 'msg': '未知模式 %r（可选 %s）' % (mode, '/'.join(MODES))}

    if codes:
        base = [{'code': c, 'name': None} for c in codes]
        pool_n, scanned = len(base), len(base)
    else:
        base, scanned = coarse_pool(limit=max(limit * 4, 40))
        pool_n = len(base)

    # 粗排：value 模式用低 PE 优先，其余用涨幅温和优先（都不做绝对判断）
    if mode == 'value' and not codes:
        base.sort(key=lambda z: _num(z.get('pe'), 999) or 999)

    rows = []
    for i, b in enumerate(base):
        if len(rows) >= limit * 2:          # 深挖 2 倍于目标数，留出被红旗淘汰的名额
            break
        try:
            rows.append(deep(b['code'], b.get('name')))
        except Exception as e:
            rows.append({'code': b['code'], 'name': b.get('name'),
                         'flags': ['深挖失败：%r' % (e,)]})
        if gap:
            time.sleep(gap)                 # 避免连续请求被限流

    dims = MODES[mode]
    for key, sign in dims:
        vals = []
        for r in rows:
            v = r.get(key)
            if isinstance(v, (int, float)):
                vals.append(v * sign)       # 统一成「越大越好」再排分位
            else:
                vals.append(None)
        for r, p in zip(rows, _pct_rank(vals)):
            r['_p_' + key] = p

    for r in rows:
        ps = [r.get('_p_' + k) for k, _s in dims if r.get('_p_' + k) is not None]
        r['composite'] = round(sum(ps) / len(ps), 1) if ps else None
        r['dims_used'] = len(ps)
        # 红旗 ≥2 → 只进观察池
        r['pool'] = 'observe' if len(r.get('flags') or []) >= 2 else 'recommend'

    rows.sort(key=lambda z: (-(z.get('composite') or -1)))
    return {'ok': True, 'mode': mode, 'pool_n': pool_n, 'deep_n': len(rows),
            'scanned': scanned, 'rows': rows,
            'note': 'composite 是**池内相对排名分数**，不是收益率/上涨概率；'
                    '权重为等权约定，未做参数优化'}


def _fmt(v, nd=2, suffix=''):
    return '—' if v is None else ('%.*f%s' % (nd, v, suffix))


def _main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    mode = 'value'
    limit = 10
    codes = None
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == '--mode' and i + 1 < len(argv):
            mode = argv[i + 1]
        if a == '--limit' and i + 1 < len(argv):
            limit = int(argv[i + 1])
        if a == '--codes' and i + 1 < len(argv):
            codes = [c.strip() for c in argv[i + 1].split(',') if c.strip()]

    if '--help' in argv or not argv:
        print(__doc__)
        return 0

    print('正在筛选（模式 %s，深挖最多 %d 只，每只间隔 0.5s 防限流）…\n' % (mode, limit * 2))
    r = screen(mode=mode, limit=limit, codes=codes)
    if not r.get('ok'):
        print('  ✗ ' + r.get('msg', ''))
        return 1

    print('=== %s 模式 | 粗筛池 %d 只（扫描 %d 条）| 深挖 %d 只 ==='
          % (mode, r['pool_n'], r['scanned'], r['deep_n']))
    print('  ' + r['note'])
    print()

    rec = [x for x in r['rows'] if x['pool'] == 'recommend']
    obs = [x for x in r['rows'] if x['pool'] == 'observe']

    def _table(items, title):
        if not items:
            return
        print('【%s】%d 只' % (title, len(items)))
        print('  %-8s %-8s %7s %7s %7s %7s %7s %7s %8s'
              % ('代码', '名称', 'PE', 'PE分位', 'PB', 'ROE%', '现金含量', '负债%', '排名分'))
        for x in items[:limit]:
            print('  %-8s %-8s %7s %7s %7s %7s %7s %7s %8s'
                  % (x['code'], (x['name'] or '')[:6],
                     _fmt(x.get('pe_ttm')), _fmt(x.get('pe_pct'), 0),
                     _fmt(x.get('pb')), _fmt(x.get('roe'), 1),
                     _fmt(x.get('cash_quality')), _fmt(x.get('debt_ratio'), 0),
                     _fmt(x.get('composite'), 1)))
        print()
        for x in items[:limit]:
            fl = x.get('flags') or []
            if fl:
                print('  %s %s 红旗：' % (x['code'], (x['name'] or '')[:6]))
                for f in fl[:3]:
                    print('      · ' + f)
            for nt in (x.get('notes') or [])[:1]:
                print('      注：' + nt)
        print()

    _table(rec, '候选池')
    _table(obs, '观察池（红旗 ≥2 条，不参与推荐）')

    print('⚠️ 本模块只做「值不值得进一步研究」的排序，**不预测涨跌**、**不构成投资建议**。')
    print('   低估值可以更低，高 ROE 可以回落；红旗比亮点更值得看。')
    return 0


if __name__ == '__main__':
    sys.exit(_main())
