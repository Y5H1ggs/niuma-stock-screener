# -*- coding: utf-8 -*-
"""
market_review.py — 收盘复盘 + 量化条件基准率 + 明日概率推演（v1.2.45）

为什么要有它：此前复盘全靠 `.workbuddy/tmp/` 里的临时脚本，数据能力与结论
既无法复现、也无法结算。本模块把「拉数 → 分布统计 → 条件基准率 → 概率推演」
固化成**两段式**，避免每次分析都重打全市场：

    python market_review.py --fetch        # 拉数落盘（联网，一次）
    python market_review.py --analyze      # 本地统计（零联网，可反复跑）
    python market_review.py --scenarios    # 明日概率（结合预测台账校准）
    python market_review.py --all          # 依次执行

缓存：`_market_review.json`（`_` 前缀已被 .gitignore 覆盖，不进仓库）。

★ 三条铁律（都是实战踩出来的，别拆）：
  1. **净额 ≠ 广度**：主力净额合计可能被少数大票撑起。2026-11-09 实测
     `+122.3 亿` 但**流出家数 2659 > 流入 2534、主力中位数 −10.9 万**。
     两者必须并列报，只看净额会误判成「资金普遍流入」。（P107）
  2. **条件基准率必须做时间分段稳定性检验**，只报平均值＝用单段极值掩盖风险（P88）。
     本模块对每个条件都给「全样本 / 前半 / 后半」与样本量 N。
  3. **本模块输出的是描述性条件频率，不是预测模型**。`model.py` 样本外
     AUC 0.508~0.528、Brier 全 ≥ 常数基线，**未通过检验，禁止用于定概率**；
     概率推演以历史条件频率为锚 + 台账校准偏差做方向性收缩，不假装是模型输出。
"""
import os
import sys
import io
import json
import time
import math
import statistics

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE)
CACHE = os.path.join(ROOT, '_market_review.json')
FORECASTS = os.path.join(ROOT, '_journal', 'forecasts.json')

IDX = {'sh000001': '上证指数', 'sz399001': '深证成指',
       'sz399006': '创业板指', 'sh000688': '科创50'}


def _num(x, k):
    v = x.get(k)
    return v if isinstance(v, (int, float)) else None


def _ismain(c):
    return c[:3] in ('000', '001', '002', '003') or c[:2] == '60'


def _q(v, p):
    v = sorted(v)
    if not v:
        return None
    return round(v[min(len(v) - 1, int(p * len(v)))], 4)


# ------------------------------------------------------------------ 一、拉数
def fetch(cache=CACHE, verbose=True):
    """一次性拉全市场 / 指数 / 板块级资金并落盘。**只在需要时调用**。"""
    import ds
    pool, pages = {}, []
    for pg in range(1, 13):
        try:
            r = ds.dce_get('RPT_DMSK_TS_STOCKNEW', pz=500, page=pg)
        except Exception as e:
            pages.append((pg, 'EXC %s' % str(e)[:40]))
            break
        if not r:
            pages.append((pg, 'EMPTY'))
            break
        for x in r:
            c = str(x.get('SECURITY_CODE') or '')
            if c:
                pool[c] = x
        pages.append((pg, len(r)))
        if len(r) < 500:
            break
        time.sleep(0.25)                      # 限速，避免限流（P: 操作节制）
    if verbose:
        print('[fetch] 全市场 %d 只，分页 %s' % (len(pool), pages))

    idx = {}
    for c, nm in IDX.items():
        snap = (ds.snapshot([c]) or {}).get(c) or {}
        k = ds.kline(c, 300)
        bars, src = k if isinstance(k, tuple) else (k, '?')
        bars = bars or []
        idx[c] = {'name': nm, 'snap': snap, 'bars': bars, 'src': src,
                  'last_date': bars[-1].get('d') if bars else None}
        if verbose:
            print('[fetch] %-10s %-8s %d 根 末根 %s 源 %s'
                  % (c, nm, len(bars), idx[c]['last_date'], src))
        time.sleep(0.2)

    try:
        b = ds.board_index() or {}
        boards = {'by_name': b.get('by_name', {}), 'meta': b.get('meta', {})}
    except Exception as e:
        boards = {'by_name': {}, 'meta': {'err': str(e)}}
    if verbose:
        print('[fetch] 板块 %d 个' % len(boards['by_name']))

    data = {'at': time.strftime('%Y-%m-%d %H:%M:%S'), 'pages': pages,
            'pool': pool, 'index': idx, 'boards': boards}
    io.open(cache, 'w', encoding='utf-8').write(json.dumps(data, ensure_ascii=False))
    if verbose:
        print('[fetch] 已写入 %s' % cache)
    return data


def load(cache=CACHE):
    if not os.path.isfile(cache):
        return None
    return json.load(io.open(cache, encoding='utf-8'))


# ------------------------------------------------------------------ 二、分布统计
def distribution(data):
    P = data['pool']
    V = [x for x in P.values() if _num(x, 'CHANGE_RATE') is not None]
    chgs = [x['CHANGE_RATE'] for x in V]
    bands = [('涨停/准涨停 ≥9.7%', lambda v: v >= 9.7),
             ('大涨 5~9.7%', lambda v: 5 <= v < 9.7),
             ('中涨 2~5%', lambda v: 2 <= v < 5),
             ('微涨 0~2%', lambda v: 0 < v < 2),
             ('平盘', lambda v: v == 0),
             ('微跌 -2~0%', lambda v: -2 < v < 0),
             ('中跌 -5~-2%', lambda v: -5 < v <= -2),
             ('大跌 -9.7~-5%', lambda v: -9.7 < v <= -5),
             ('跌停 ≤-9.7%', lambda v: v <= -9.7)]
    trs = [x['TURNOVERRATE'] for x in V if _num(x, 'TURNOVERRATE') is not None]
    pes = [x['PE_DYNAMIC'] for x in V
           if _num(x, 'PE_DYNAMIC') is not None and 0 < x['PE_DYNAMIC'] < 300]
    pfs = [x['PRIME_INFLOW'] for x in V if _num(x, 'PRIME_INFLOW') is not None]
    main = [x for x in V if _ismain(str(x['SECURITY_CODE']))]
    other = [x for x in V if not _ismain(str(x['SECURITY_CODE']))]
    return {
        'total': len(P), 'valid': len(V),
        'date': next(iter(P.values())).get('TRADE_DATE') if P else None,
        'bands': [(nm, sum(1 for v in chgs if f(v))) for nm, f in bands],
        'up': sum(1 for v in chgs if v > 0),
        'down': sum(1 for v in chgs if v < 0),
        'breadth': round(100.0 * sum(1 for v in chgs if v > 0) / len(V), 2),
        'chg_q': {str(p): _q(chgs, p) for p in (0.1, 0.25, 0.5, 0.75, 0.9)},
        'turnover_q': {str(p): _q(trs, p) for p in (0.1, 0.25, 0.5, 0.75, 0.9)},
        'pe_q': {str(p): _q(pes, p) for p in (0.1, 0.25, 0.5, 0.75, 0.9)},
        # ★ P107：净额与广度并列，不许只看净额
        'prime_sum_yi': round(sum(pfs) / 1e8, 2),
        'prime_in_n': sum(1 for v in pfs if v > 0),
        'prime_out_n': sum(1 for v in pfs if v < 0),
        'prime_median_wan': round(statistics.median(pfs) / 1e4, 2),
        'mainboard': {'n': len(main), 'up': sum(1 for x in main if x['CHANGE_RATE'] > 0)},
        'other': {'n': len(other), 'up': sum(1 for x in other if x['CHANGE_RATE'] > 0)},
    }


# ------------------------------------------------------------------ 三、条件基准率
def _cond_rate(rets, cond, label):
    hit = [i for i in range(len(rets) - 1) if cond(i)]
    if not hit:
        return {'label': label, 'n': 0, 'rate': None}
    wins = sum(1 for i in hit if rets[i + 1] > 0)
    half = len(rets) // 2

    def part(lo, hi):
        h = [i for i in hit if lo <= i < hi]
        return ((round(100.0 * sum(1 for i in h if rets[i + 1] > 0) / len(h), 1), len(h))
                if h else (None, 0))

    a, na = part(0, half)
    b, nb = part(half, len(rets) - 1)
    return {'label': label, 'n': len(hit), 'rate': round(100.0 * wins / len(hit), 1),
            'first_half': a, 'n_first': na, 'second_half': b, 'n_second': nb,
            'stable': bool(a is not None and b is not None and abs(a - b) <= 20)}


def conditional_rates(data):
    out = {}
    for c, v in data['index'].items():
        bars = v['bars']
        if len(bars) < 70:
            continue
        cs = [x['c'] for x in bars]
        rets = [(cs[i] / cs[i - 1] - 1) * 100 for i in range(1, len(cs))]
        ma = {str(k): round(sum(cs[-k:]) / k, 2) for k in (5, 10, 20, 60)}
        mean_r, sd_r = statistics.mean(rets), statistics.pstdev(rets)
        lag1 = (statistics.mean([(rets[i] - mean_r) * (rets[i + 1] - mean_r)
                                 for i in range(len(rets) - 1)]) / sd_r ** 2) if sd_r else 0
        conds = [
            ('无条件基准（次日上涨）', lambda i: True),
            ('今日涨 → 次日涨', lambda i: rets[i] > 0),
            ('今日跌 → 次日涨', lambda i: rets[i] < 0),
            ('今日微涨 0~0.5%', lambda i: 0 < rets[i] <= 0.5),
            ('连涨 2 日 → 次日涨', lambda i: i >= 1 and rets[i] > 0 and rets[i - 1] > 0),
            ('连跌 2 日 → 次日涨', lambda i: i >= 1 and rets[i] < 0 and rets[i - 1] < 0),
            ('今日振幅>1% → 次日涨', lambda i: abs(rets[i]) > 1),
        ]
        out[c] = {'name': v['name'], 'src': v['src'], 'bars': len(cs),
                  'last_date': v['last_date'], 'last_close': cs[-1],
                  'last_chg': round(rets[-1], 3), 'ma': ma,
                  'above_ma20': cs[-1] > ma['20'], 'above_ma60': cs[-1] > ma['60'],
                  'vol20_ann': round(statistics.pstdev(rets[-20:]) * math.sqrt(250), 2),
                  'vol60_ann': round(statistics.pstdev(rets[-60:]) * math.sqrt(250), 2),
                  'lag1': round(lag1, 4),
                  'cond': [_cond_rate(rets, f, nm) for nm, f in conds]}
    return out


# ------------------------------------------------------------------ 四、台账校准
def calibration(path=FORECASTS):
    """从预测台账动态算「平均预测 vs 实际频率」偏差 —— 保证概率推演同源。"""
    if not os.path.isfile(path):
        return {'ok': False, 'why': '无 forecasts.json'}
    F = json.load(io.open(path, encoding='utf-8'))
    events = {'open_up': lambda a: a['open'] > 0, 'close_up': lambda a: a['close'] > 0,
              'high_ge1': lambda a: a['high'] >= 1, 'high_ge2': lambda a: a['high'] >= 2,
              'low_le1': lambda a: a['low'] <= -1, 'break3': lambda a: a['low'] <= -3}
    out = {}
    for k, f in events.items():
        pairs = [(r['probs'][k], 1 if f(r['actual']) else 0)
                 for r in F if r.get('actual') and k in (r.get('probs') or {})]
        if not pairs:
            continue
        pred = statistics.mean(p for p, _ in pairs)
        real = 100.0 * sum(a for _, a in pairs) / len(pairs)
        out[k] = {'n': len(pairs), 'avg_pred': round(pred, 1),
                  'real_freq': round(real, 1), 'bias': round(pred - real, 1)}
    brier = [r['brier'] for r in F if isinstance(r.get('brier'), (int, float))]
    up_bias = [v['bias'] for k, v in out.items() if k in ('open_up', 'close_up', 'high_ge1', 'high_ge2')]
    return {'ok': True, 'n_forecasts': len(F),
            'brier': round(statistics.mean(brier), 4) if brier else None,
            'events': out,
            'up_bias_avg': round(statistics.mean(up_bias), 1) if up_bias else None,
            'note': '样本 <30 笔不许调参；偏差只用于方向性收缩，不拟合新权重'}


# ------------------------------------------------------------------ 五、明日概率
def scenarios(stats, calib, data):
    """以条件基准率为锚，输出可复核的区间概率。**不是模型输出**。"""
    idx = stats['cond'].get('sh000001', {})
    conds = {c['label']: c for c in idx.get('cond', [])}
    last = idx.get('last_chg', 0)
    if 0 < last <= 0.5:
        key = '今日微涨 0~0.5%'
    elif last > 0:
        key = '今日涨 → 次日涨'
    else:
        key = '今日跌 → 次日涨'
    anchor = conds.get(key, {}).get('rate')
    unconditional = conds.get('无条件基准（次日上涨）', {}).get('rate')
    m = stats['market']
    in_ratio = 100.0 * m['prime_in_n'] / (m['prime_in_n'] + m['prime_out_n'])

    # 偏下修 / 偏上修因素（透明列出，不做数值拟合 —— 样本 <30 笔不许调参）
    down, up = [], []
    if not idx.get('above_ma20'):
        down.append('指数在 MA20 之下')
    if not idx.get('above_ma60'):
        down.append('指数在 MA60 之下（中期趋势偏弱）')
    if m['breadth'] < 45:
        down.append('红盘率 %.1f%% < 45%%（广度差）' % m['breadth'])
    if in_ratio < 50:
        down.append('主力流入家数占比 %.1f%% < 50%%（净额由少数大票撑起）' % in_ratio)
    if idx.get('lag1', 0) < 0:
        down.append('lag1 自相关 %.3f < 0（短期反转效应）' % idx.get('lag1'))
    if idx.get('vol20_ann', 0) > idx.get('vol60_ann', 0):
        down.append('近 20 日波动率高于 60 日（波动放大）')
    up.append('主力净额合计 %+.1f 亿为正' % m['prime_sum_yi'])
    if unconditional and unconditional > 50:
        up.append('无条件次日上涨基准 %.1f%%' % unconditional)

    spread = 6.0     # 区间半宽：反映样本量与条件筛选的不确定性，非拟合值
    def band(center):
        return [round(max(1.0, center - spread), 1), round(min(99.0, center + spread), 1)]

    out = {
        'anchor': {'label': key, 'rate': anchor,
                   'n': conds.get(key, {}).get('n'),
                   'first_half': conds.get(key, {}).get('first_half'),
                   'second_half': conds.get(key, {}).get('second_half'),
                   'unconditional': unconditional},
        'calibration': calib,
        'down_factors': down, 'up_factors': up,
        'scenarios': [
            {'name': '上证指数次日收红', 'band': band(anchor) if anchor else None,
             'basis': '条件基准率 %s%%（N=%s）+ 下修因素 %d 项' % (anchor, conds.get(key, {}).get('n'), len(down))},
            {'name': '个股红盘率 >50%（广度修复）', 'band': [25, 40],
             'basis': '今日红盘率 %.1f%%；无历史广度序列，主观区间' % m['breadth']},
            {'name': '主线（资金净流入 TOP 板块）延续', 'band': [50, 65],
             'basis': '板块级资金强弱差，但受短期反转效应制约'},
            {'name': '高位连板继续退潮', 'band': [55, 70],
             'basis': '连板板指走弱 + 涨停系主力净流出（由 mood.py 单独提供）'},
            {'name': '全面普涨（红盘率>60%）', 'band': [8, 20],
             'basis': '需增量资金扩散，今日净额集中而非普涨'},
        ],
        'model_note': ('model.py 样本外 AUC 0.508~0.528、Brier 全 ≥ 常数基线 → 未通过检验，'
                       '禁止用于选股/定概率；本推演为描述性条件频率 + 台账方向性收缩，'
                       '非模型输出，故不报 AUC（不适用）'),
    }
    return out


def _print(stats, sc):
    m = stats['market']
    print('\n=== 全市场 ===')
    print('  %d 只 / 有效 %d | 涨 %d 跌 %d | 红盘率 %.1f%%'
          % (m['total'], m['valid'], m['up'], m['down'], m['breadth']))
    print('  ★ 主力净额 %+.1f 亿 BUT 流入 %d 家 / 流出 %d 家、中位数 %.1f 万 → 净额≠广度'
          % (m['prime_sum_yi'], m['prime_in_n'], m['prime_out_n'], m['prime_median_wan']))
    print('=== 条件基准率（上证） ===')
    for c in stats['cond']['sh000001']['cond']:
        print('  %-26s N=%3d 全 %5s%% 前半 %5s%% 后半 %5s%% 稳定=%s'
              % (c['label'], c['n'], c['rate'], c['first_half'], c['second_half'], c['stable']))
    print('=== 明日情景概率（区间，已列收缩因素） ===')
    for s in sc['scenarios']:
        print('  %-26s %s%%  ← %s' % (s['name'], s['band'], s['basis']))
    print('  校准: Brier %s | 上涨类平均偏差 %s pp | %s'
          % (sc['calibration'].get('brier'), sc['calibration'].get('up_bias_avg'),
             sc['calibration'].get('note')))
    print('  %s' % sc['model_note'])


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or '--help' in argv or '-h' in argv:
        print(__doc__)
        return 0
    cache = CACHE
    if '--cache' in argv:
        cache = argv[argv.index('--cache') + 1]
    do_fetch = '--fetch' in argv or '--all' in argv
    do_stats = '--analyze' in argv or '--scenarios' in argv or '--all' in argv
    if do_fetch:
        fetch(cache)
    data = load(cache)
    if not data:
        print('❌ 无缓存，请先 --fetch')
        return 1
    stats = {'market': distribution(data), 'cond': conditional_rates(data)}
    sc = scenarios(stats, calibration(), data)
    io.open(os.path.splitext(cache)[0] + '_stats.json', 'w', encoding='utf-8').write(
        json.dumps({'stats': stats, 'scenarios': sc}, ensure_ascii=False, indent=1))
    if do_stats:
        _print(stats, sc)
    return 0


if __name__ == '__main__':
    sys.exit(main())
