# -*- coding: utf-8 -*-
"""model.py — 多指标走势预测模型（**可验证版**）

=== 设计原则（使用者 2026-10-09 要求）===
  1) **没有数据/证据支持的结论一律不给**；
  2) 所有推理必须带概率；
  3) 指标成体系：MACD / KDJ / 资金量 / 均线趋势 / 摆动 / 量能 / 位置 / 形态（共 39 个特征）；
  4) ★ 必须**样本外检验**才允许使用：时间切分（前 70% 交易日训练 / 后 30% 测试），
       报 Brier / AUC / 校准 / Top-K 胜率，并与基线对比。**跑不赢基线就明说不能用。**

=== 两处方法论要点（第一版因此失败，见 CHANGELOG v1.2.14）===
  · **时间切分不随机**：随机切分会在时间序列上造成前视泄漏，把模型"测"得很好看而实盘无效。
  · **必须做每日横截面标准化**：样本池里 600519（1267 元）与 000725（5.4 元）的指标
    绝对水平不可比，直接喂绝对值 → 模型学到的是"这只票是谁"而不是"现在什么形态"。
    修正为：同日所有样本内取百分位排名（0~1）。

=== 口径约定（P84 / P85）===
  · "近 N 日"窗口只用**完整交易日**，不含当日未走完那根；
  · `pos60/pos250` 不足即剔除，绝不缩窗口冒充；
  · 资金用**新浪 netamount**（≠ 东财 f62），全程标 src=sina；
  · 成交额 = 成交量(手) × 100 × 收盘价（腾讯 K 线无成交额字段），已声明。

用法：python model.py [--universe 30] [--horizon 1]
"""
import os, sys, time, math, random
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ds

FALLBACK_POOL = [
    '000001', '600000', '600030', '000002', '600519', '000858', '600887', '601318',
    '600036', '000333', '600276', '002415', '600585', '601166', '600028', '601857',
    '601988', '601398', '600104', '000651', '002594', '600690', '601088', '600900',
    '601899', '600309', '002304', '600031', '000725', '601668',
]

FEATS = [
    'bull', 'bear', 'dev5', 'dev20', 'ma5_slope', 'ma20_slope',
    'macd_dif', 'macd_dea', 'macd_hist', 'macd_hist_d', 'macd_above0', 'macd_gold',
    'kdj_k', 'kdj_d', 'kdj_j', 'kdj_kd', 'kdj_gold', 'kdj_dead',
    'rsi6', 'rsi14', 'pctb', 'boll_bw', 'cci20', 'wr14', 'mom10',
    'vr5', 'vr20', 'vp', 'upper', 'lower', 'gap', 'chg',
    'ff_ratio', 'ff3_ratio', 'ff5_ratio', 'turnover',
    'pos60', 'pos250', 'streak',
]
HIDX = {1: 3, 3: 4, 5: 5}      # 行内 fwd 字段下标


def pool(n=30, verbose=True):
    try:
        rows = ds.clist(fid='f6', fs=ds.MARKET_MAIN, pages=2, fields='f12,f14,f6')
    except Exception:
        rows = []
    out = []
    for x in rows or []:
        c, nm = str(x.get('f12') or ''), str(x.get('f14') or '')
        if not ds.allowed(c) or 'ST' in nm.upper() or '退' in nm:
            continue
        out.append(c)
        if len(out) >= n:
            break
    if len(out) >= 10:
        if verbose:
            print('  样本池：东财成交额榜（全市场主板）%d 只' % len(out))
        return out, 'clist_amt'
    if verbose:
        print('  ⚠️ 全市场榜取数失败 → **退回内置高流动性名单（有偏样本，须声明）**')
    return FALLBACK_POOL[:n], 'fallback_list'


# ------------------------------------------------------------------ 特征
def feats(k, i, fl):
    """第 i 根 K 线**收盘时**可得的特征（严格只用 k[0..i]，无前视）。None = 剔除。"""
    if i < 250:
        return None
    c, o, h, l, v = k[i]['c'], k[i]['o'], k[i]['h'], k[i]['l'], k[i]['v']
    prev = k[i - 1]['c']
    if not prev or not c:
        return None
    cl = [k[j]['c'] for j in range(i + 1)]
    hi = [k[j]['h'] for j in range(i + 1)]
    lo = [k[j]['l'] for j in range(i + 1)]
    vols = [k[j]['v'] for j in range(i + 1)]

    ma5, ma10, ma20, ma60 = ds.ma(cl, 5), ds.ma(cl, 10), ds.ma(cl, 20), ds.ma(cl, 60)
    ma5p, ma20p = ds.ma(cl[:-1], 5), ds.ma(cl[:-1], 20)
    b = ds.boll(cl); m = ds.macd(cl); mp = ds.macd(cl[:-1])
    kk, dd, jj = ds.kdj(hi, lo, cl)
    kp, dp, _ = ds.kdj(hi[:-1], lo[:-1], cl[:-1])
    gold = 1.0 if (kp == kp and kk == kk and kp <= dp and kk > dd) else 0.0
    dead = 1.0 if (kp == kp and kk == kk and kp >= dp and kk < dd) else 0.0

    v5 = sum(vols[-5:]) / 5.0; v20 = sum(vols[-20:]) / 20.0
    vr5 = (v / v5) if v5 else 1.0
    vr20 = (v / v20) if v20 else 1.0
    up_ = c > prev
    vp = 1.0 if (up_ and vr20 >= 1.0) else (-1.0 if (up_ and vr20 < 1.0) else (-1.0 if vr20 >= 1.3 else 1.0))

    d = k[i]['d']
    f = fl.get(d)
    if not f:
        return None
    net = f['net']
    amt = v * 100 * c
    n3 = a3 = n5 = a5 = 0.0
    for j in range(max(0, i - 4), i + 1):
        fd = fl.get(k[j]['d'])
        if fd:
            a_ = k[j]['v'] * 100 * k[j]['c']
            if j >= i - 2:
                n3 += fd['net']; a3 += a_
            n5 += fd['net']; a5 += a_
    st = 0
    for j in range(i, 249, -1):
        if k[j - 1]['c'] and k[j]['c'] / k[j - 1]['c'] - 1 >= 0.095:
            st += 1
        else:
            break
    r60 = (max(hi[-60:]) - min(lo[-60:]))
    return [
        1.0 if ma5 > ma10 > ma20 else 0.0,
        1.0 if ma5 < ma10 < ma20 else 0.0,
        (c / ma5 - 1) * 100, (c / ma20 - 1) * 100,
        (ma5 / ma5p - 1) * 100 if ma5p else 0.0,
        (ma20 / ma20p - 1) * 100 if ma20p else 0.0,
        m['dif'], m['dea'], m['macd'],
        (m['macd'] - mp['macd']) if mp['macd'] is not None else 0.0,
        1.0 if m['dif'] > 0 else 0.0,
        1.0 if (mp['macd'] is not None and mp['macd'] <= 0 < m['macd']) else 0.0,
        kk if kk == kk else 50.0, dd if dd == dd else 50.0, jj if jj == jj else 50.0,
        (kk - dd) if (kk == kk and dd == dd) else 0.0, gold, dead,
        ds.rsi(cl, 6), ds.rsi(cl, 14),
        b['pctb'], ((b['up'] - b['low']) / b['mid'] * 100) if b['mid'] else 0.0,
        ds.cci(hi, lo, cl, 20), ds.wr(hi, lo, cl, 14), ds.mom(cl, 10),
        vr5, vr20, vp,
        (h - max(o, c)) / c * 100, (min(o, c) - l) / c * 100,
        (o / prev - 1) * 100, (c / prev - 1) * 100,
        (net / amt) if amt else 0.0, (n3 / a3) if a3 else 0.0, (n5 / a5) if a5 else 0.0,
        f.get('turnover') or 0.0,
        (c - min(lo[-60:])) / r60 * 100 if r60 else 50.0,
        (c - min(lo[-250:])) / (max(hi[-250:]) - min(lo[-250:])) * 100
        if max(hi[-250:]) > min(lo[-250:]) else 50.0,
        float(st),
    ]


def build_panel(codes, verbose=True):
    rows, dropped = [], {'warmup': 0, 'nan': 0, 'fail': 0}
    for n, c in enumerate(codes):
        k = ds.kline_qq(c, 800)
        fl = {}
        for x in ds.moneyflow_sina(c, 600):
            if x[0]:
                fl[x[0]] = {'net': x[1], 'turnover': x[5]}
        if not k or len(k) < 400 or not fl:
            dropped['fail'] += 1
            continue
        today = time.strftime('%Y-%m-%d')
        last = len(k) - 1 - (1 if k[-1]['d'] == today else 0)   # 最后一个【完整】交易日的下标
        # ★ 一直取到 last（含最后几个交易日）：尾部没有"未来收益"，fwd 置 None（评估时过滤掉）。
        #   为什么必须留这条尾巴：单票打分要用「目标票信号日」的**同日横截面**做排名转换，
        #   若样本只到 last-5，最新横截面就停在 5 天前 → 打分时找不到该日截面。
        #   ⚠️ 标签也**不得超过 last**：否则会拿"今日未走完那根"的收盘当未来收益
        #      （同 P84「K 线末根语义」——半日数据既不能进特征，也不能进标签）。
        for i in range(250, last + 1):
            f = feats(k, i, fl)
            if f is None:
                dropped['warmup'] += 1
                continue
            if any(x != x for x in f):
                dropped['nan'] += 1
                continue
            base = k[i]['c']

            def _g(h, i=i, base=base, last=last):
                return (k[i + h]['c'] / base - 1) * 100 if i + h <= last else None
            rows.append((k[i]['d'], c, f, _g(1), _g(3), _g(5)))
        if verbose and (n + 1) % 10 == 0:
            print('    载入 %d/%d（累计 %d 条）' % (n + 1, len(codes), len(rows)), flush=True)
        time.sleep(0.15)
    return rows, dropped


# ------------------------------------------------------------------ 横截面标准化
def xsec(rows):
    """按**日期**对每个特征取百分位排名（0~1）→ 消掉"这只票是谁"的横截面差异。"""
    by = {}
    for r in rows:
        by.setdefault(r[0], []).append(r)
    out = []
    for d, g in by.items():
        n = len(g)
        if n < 8:
            continue
        P = len(g[0][2])
        R = [[0.0] * P for _ in range(n)]
        for j in range(P):
            order = sorted(range(n), key=lambda t: g[t][2][j])
            for pos, t in enumerate(order):
                R[t][j] = (pos + 0.5) / n
        for t in range(n):
            out.append((g[t][0], g[t][1], R[t], g[t][3], g[t][4], g[t][5]))
    return out


# ------------------------------------------------------------------ 模型
def standardize(X, mu=None, sd=None):
    n, p = len(X), len(X[0])
    if mu is None:
        mu = [sum(r[j] for r in X) / n for j in range(p)]
        sd = [(sum((r[j] - mu[j]) ** 2 for r in X) / n) ** 0.5 or 1.0 for j in range(p)]
    return [[(r[j] - mu[j]) / sd[j] for j in range(p)] for r in X], mu, sd


def fit(X, y, epochs=80, bs=256, lr=0.10, l2=3e-3, seed=7):
    rnd = random.Random(seed)
    n, p = len(X), len(X[0])
    w = [0.0] * p; b = 0.0
    idx = list(range(n))
    for _ in range(epochs):
        rnd.shuffle(idx)
        for s in range(0, n, bs):
            ch = idx[s:s + bs]
            gw = [0.0] * p; gb = 0.0
            for i in ch:
                z = b + sum(w[j] * X[i][j] for j in range(p))
                pr = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
                e = pr - y[i]
                gb += e
                for j in range(p):
                    gw[j] += e * X[i][j]
            mm = float(len(ch))
            b -= lr * gb / mm
            for j in range(p):
                w[j] -= lr * (gw[j] / mm + l2 * w[j])
    return w, b


def proba(w, b, r):
    z = b + sum(w[j] * r[j] for j in range(len(r)))
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


def brier(y, p):
    return sum((pi - yi) ** 2 for pi, yi in zip(p, y)) / len(y)


def auc(y, p):
    prs = sorted(zip(p, y))
    n1 = sum(y); n0 = len(y) - n1
    if not n1 or not n0:
        return None
    rk = 0.0; i = 0
    while i < len(prs):
        j = i
        while j + 1 < len(prs) and prs[j + 1][0] == prs[i][0]:
            j += 1
        av = (i + j) / 2.0 + 1
        for t in range(i, j + 1):
            if prs[t][1]:
                rk += av
        i = j + 1
    return (rk - n1 * (n1 + 1) / 2.0) / (n1 * n0)


def topk(y, p, frac):
    k = max(1, int(len(p) * frac))
    o = sorted(range(len(p)), key=lambda i: -p[i])[:k]
    return k, sum(y[i] for i in o) / k, sum(p[i] for i in o) / k


def split(rows, frac=0.70):
    ds_ = sorted(set(r[0] for r in rows))
    cut = ds_[int(len(ds_) * frac)]
    return [r for r in rows if r[0] < cut], [r for r in rows if r[0] >= cut]


def evaluate(TR, TE, h, tag):
    j = HIDX[h]
    # ★ 过滤掉"还没有未来收益"的尾部样本（_g() 返回 None）——
    #   这些样本只用于「当日横截面」，**不能**参与训练/评估。
    TR = [r for r in TR if r[j] is not None]
    TE = [r for r in TE if r[j] is not None]
    Xtr = [list(r[2]) for r in TR]; ytr = [1.0 if r[j] > 0 else 0.0 for r in TR]
    Xte = [list(r[2]) for r in TE]; yte = [1.0 if r[j] > 0 else 0.0 for r in TE]
    if not Xtr or not Xte:
        return dict(tag=tag, h=h, ntr=0, nte=0, base=0.0, act=0.0, br_m=1.0,
                    br_b=1.0, auc=None, top=0.0, top_p=0.0, topn=0, w=[0.0] * len(FEATS))
    Ztr, mu, sd = standardize(Xtr)
    Zte, _, _ = standardize(Xte, mu, sd)
    w, b = fit(Ztr, ytr)
    pte = [proba(w, b, r) for r in Zte]
    base = sum(ytr) / len(ytr)
    k_, wr, mp = topk(yte, pte, 0.20)
    return dict(tag=tag, h=h, ntr=len(TR), nte=len(TE), base=base,
                act=sum(yte) / len(yte), br_m=brier(yte, pte),
                br_b=brier(yte, [base] * len(yte)), auc=auc(yte, pte),
                top=wr, top_p=mp, topn=k_, w=w, mu=mu, sd=sd)


# ------------------------------------------------------------------ 单票打分 / 今日清单 / 台账闭环
def train_final(pool_codes, h=1, verbose=True):
    """用样本池训练。**先算样本外 AUC（诚实标注），再用全部样本拟合最终权重。**"""
    P, dropped = build_panel(pool_codes, verbose=verbose)
    if len(P) < 1000:
        return None
    X = xsec(P)
    TR, TE = split(X)
    oos = evaluate(TR, TE, h, '训练池')
    j = HIDX[h]
    # ★ 尾部样本（无未来收益，fwd=None）只用于「当日横截面」，**不得进训练** ——
    #   否则 None 参与比较会直接抛 TypeError，或被静默当成某类标签。
    Xa_all = [r for r in X if r[j] is not None]
    Xa = [list(r[2]) for r in Xa_all]
    ya = [1.0 if r[j] > 0 else 0.0 for r in Xa_all]
    Z, mu, sd = standardize(Xa)
    w, b = fit(Z, ya)
    return dict(w=w, b=b, mu=mu, sd=sd, auc=oos['auc'], brier=oos['br_m'],
                base_brier=oos['br_b'], n=len(X), pool=pool_codes, tag=oos['tag'],
                dropped=dropped, raw=P, xs=X)


def _xsec_row(M, day, raw_f):
    """把单票的**原始特征**按「当日横截面」转成与训练一致的排名（0~1）。

    ★ 这一步不能省：训练用的是横截面排名（值域 0~1、均值≈0.5），
      若直接拿原始绝对值去做 `(raw−mu)/sd`，z 会瞬间饱和 → 概率变成 0% 或 100%
      （**实测第一版就是这样输出 100.0% 的**：`turnover=3.5` 经 (3.5−0.5)/0.29 ≈ +10 直接顶到上限）。
      做法：把目标票"插进"训练池**同日**的横截面里取百分位，公式与 `xsec()` 保持一致。
    """
    cs = [r[2] for r in M['raw'] if r[0] == day]
    if len(cs) < 5:
        return None
    n_cs = len(cs)
    out = []
    for j in range(len(raw_f)):
        vals = [c[j] for c in cs]
        less = sum(1 for v in vals if v < raw_f[j])
        out.append((less + 0.5) / (n_cs + 1))
    return out


def score_code(M, code, day=None):
    """对单票的**最后一个完整交易日**打分 → P(次日收涨)。

    ⚠️ 只用**闭合日线**特征；今日盘中信息**刻意不纳入**（训练集全是完整交易日，
       拼半日数据会让特征分布与训练不一致 —— 同 P84「K 线末根语义」纪律）。
    ⚠️ 必须经 `_xsec_row()` 转成横截面排名，**不得直接喂原始值**。
    返回 {'code','prob','date','close','ranked'}；None = 数据不足（不得伪造）。
    """
    k = ds.kline_qq(code, 800)
    fl = {}
    for x in ds.moneyflow_sina(code, 600):
        if x[0]:
            fl[x[0]] = {'net': x[1], 'turnover': x[5]}
    if not k or not fl:
        return None
    i = len(k) - 1 - (1 if k[-1]['d'] == time.strftime('%Y-%m-%d') else 0)
    if day:
        idx = [t for t in range(len(k)) if k[t]['d'] == day]
        if not idx:
            return None
        i = idx[0]
    f = feats(k, i, fl)
    if f is None or any(x != x for x in f):
        return None
    xr = _xsec_row(M, k[i]['d'], f)
    if xr is None:
        return None
    z = [(xr[j] - M['mu'][j]) / M['sd'][j] for j in range(len(xr))]
    return {'code': code, 'prob': proba(M['w'], M['b'], z),
            'date': k[i]['d'], 'close': k[i]['c']}


def rank_today(M, codes, verbose=True):
    """对一组票按模型概率排序（信号日 = 各自最后一个完整交易日）。"""
    out = []
    for n, c in enumerate(codes):
        r = score_code(M, c)
        if r:
            out.append(r)
        if verbose and (n + 1) % 20 == 0:
            print('    打分 %d/%d' % (n + 1, len(codes)), flush=True)
        time.sleep(0.25)
    out.sort(key=lambda z: -z['prob'])
    return out


def log_forecast(r, M, note=''):
    """把一次模型预测写入预测台账 → **闭环**：到期由 `calibrate.py settle` 自动结算。

    ⚠️ `made_on` 必须填**信号日**（r['date']），不是今天 ——
       `settle_all()` 取的是"made_on 之后的第一个交易日"，填今天会把结算日推后一天。
    ⚠️ 只记录模型**真正预测过**的事件（`close_up`）。
       不把没算过的 open_up/high_ge1 填进去冒充实测 —— 那是伪造。
    """
    import journal
    probs = {'close_up': round(r['prob'] * 100, 1)}
    meta = {'method': 'model.py', 'auc_oos': round(M['auc'] or 0, 4),
            'brier_oos': round(M['brier'], 4), 'baseline_brier': round(M['base_brier'], 4),
            'n_pool': M['n'], 'config': M['tag'], 'note': note,
            'caveat': '样本外 AUC 仅 %.3f（近随机），概率不得当可靠信号使用' % (M['auc'] or 0)}
    return journal.record_forecast(r['code'], r['date'], probs, r['close'], None, meta)


def main():
    uni, target, do_log = 30, None, ('--log' in sys.argv)
    for i, a in enumerate(sys.argv):
        if a == '--universe':
            uni = int(sys.argv[i + 1])
        if a == '--score':
            target = sys.argv[i + 1]
    print('=' * 104)
    print('【模型单票判断】%s' % target if target else '【多指标走势预测模型 · 样本外检验】')
    print('=' * 104)
    print('时间', time.strftime('%Y-%m-%d %H:%M:%S'),
          '| 特征 %d 个（MACD / KDJ / 资金 / 均线 / 摆动 / 量能 / 位置 / 形态）' % len(FEATS))
    codes, src = pool(uni)

    if target:
        M = train_final(codes)
        if not M:
            print('⚠️ 训练样本不足，**不予打分**。')
            return
        print('  训练池 %d 只 → %d 条样本 | 样本外 AUC **%.4f** | Brier %.4f（基线 %.4f）'
              % (len(codes), M['n'], M['auc'] or 0, M['brier'], M['base_brier']))
        r = score_code(M, target)
        if not r:
            print('⚠️ 该票 K 线或资金流缺失 → **不给结论**（不猜）')
            return
        print()
        print('-' * 104)
        print('  信号日 %s（最后一个**完整**交易日）  收盘 %.2f' % (r['date'], r['close']))
        print('  ★ 模型 P(次一交易日收涨) = **%.1f%%**' % (r['prob'] * 100))
        if r['prob'] <= 0.001 or r['prob'] >= 0.999:
            print('  🔴 **概率饱和（0% / 100%）→ 判定为口径异常，本次结果作废。**')
            print('     任何"恰好 0 或 100%"的关键指标，先怀疑口径/取数失败，而不是当成极强信号。')
            print('     （实测第一版就是因为漏了横截面排名转换，稳定输出 100.0%）')
            return
        print('  ⚠️ 验证状态：该配置样本外 AUC **%.4f**（0.5 = 无区分度；本轮实测未通过）'
              % (M['auc'] or 0))
        if (M['auc'] or 0) < 0.53:
            print('  → **未通过有效性检验：此概率不得作为决策依据**，仅作记录与追踪之用。')
        print('  ⚠️ 本打分只用**闭合日线**特征，今日盘中信息**未纳入**。')
        print('-' * 104)
        if do_log:
            rec = log_forecast(r, M, note='model.py --score')
            print('  已写入预测台账 id=%s → 到期 `calibrate.py settle` 自动结算' % rec['id'])
        return

    print('载入数据（腾讯 qfq 800 根 + 新浪资金 600 日）…')
    rows, dropped = build_panel(codes)
    print('样本 %d 条 | 剔除：预热 %d、nan %d、取数失败 %d'
          % (len(rows), dropped['warmup'], dropped['nan'], dropped['fail']))
    if len(rows) < 1000:
        print('⚠️ 样本 < 1000，**不予建模**（样本不足的结果不可外推）。')
        return
    xs = xsec(rows)
    print('横截面标准化后可用样本 %d 条（同日样本 <8 的日期已剔除）' % len(xs))
    print()

    trA, teA = split(rows)
    trX, teX = split(xs)
    print('切分：训练 %s ~ %s（%d 条）｜测试 %s ~ %s（%d 条）'
          % (trA[0][0], trA[-1][0], len(trA), teA[0][0], teA[-1][0], len(teA)))
    print('（**按时间切分，不随机** —— 随机切分会造成前视泄漏，把模型"测"得很好看而实盘无效）')
    print()
    print('-' * 104)
    print('%-22s %4s %9s %11s %8s %12s %12s' % ('配置', 'h', 'Brier', '基线Brier', 'AUC', 'Top20%实际', 'Top20%预测'))
    print('-' * 104)
    res = []
    for tag, TR, TE in (('① 绝对值特征', trA, teA), ('② 横截面标准化', trX, teX)):
        for h in (1, 3, 5):
            r = evaluate(TR, TE, h, tag)
            res.append(r)
            print('%-22s %4d %9.4f %11.4f %8.4f %11.2f%% %11.2f%%'
                  % (tag, h, r['br_m'], r['br_b'], r['auc'] or 0, r['top'] * 100, r['top_p'] * 100))
    print('-' * 104)
    for h in (1, 3, 5):
        rr = [r for r in teX if r[HIDX[h]] is not None]
        print('  h=%d 测试集实际上涨率 %.2f%%（样本 %d 条；已剔除"无未来收益"的尾部）'
              % (h, sum(1 for r in rr if r[HIDX[h]] > 0) / len(rr) * 100, len(rr)))
    print('判读：Brier **越小越好**，「基线 Brier」= 永远说基准概率；AUC 0.5 = 无区分度。')
    print()

    best = max(res, key=lambda r: (r['auc'] or 0))
    print('-' * 104)
    print('【最佳配置（按 AUC）：%s / h=%d】分桶校准 —— 校准好坏=概率能不能信' % (best['tag'], best['h']))
    print('-' * 104)
    j = HIDX[best['h']]
    TR, TE = (trX, teX) if '横截面' in best['tag'] else (trA, teA)
    TR = [r for r in TR if r[j] is not None]
    TE = [r for r in TE if r[j] is not None]
    Xtr = [list(r[2]) for r in TR]; ytr = [1.0 if r[j] > 0 else 0.0 for r in TR]
    Xte = [list(r[2]) for r in TE]; yte = [1.0 if r[j] > 0 else 0.0 for r in TE]
    Ztr, mu, sd = standardize(Xtr); Zte, _, _ = standardize(Xte, mu, sd)
    w, b = fit(Ztr, ytr)
    pte = [proba(w, b, r) for r in Zte]
    print('  %-13s %6s %11s %11s %10s' % ('预测区间', '样本', '平均预测', '实际频率', '偏差'))
    for a, b_ in ((0, .40), (.40, .45), (.45, .48), (.48, .50), (.50, .52), (.52, .55), (.55, .60), (.60, 1.0)):
        sel = [(pi, yi) for pi, yi in zip(pte, yte) if a <= pi < b_]
        if not sel:
            continue
        mp = sum(x[0] for x in sel) / len(sel); af = sum(x[1] for x in sel) / len(sel)
        print('  %-13s %6d %10.2f%% %10.2f%% %+9.2fpp'
              % ('%.2f ~ %.2f' % (a, b_), len(sel), mp * 100, af * 100, (af - mp) * 100))
    print()
    print('  权重前 14（标准化后；正 = 看涨）：')
    for nm, wv in sorted(zip(FEATS, w), key=lambda z: -abs(z[1]))[:14]:
        print('    %-14s %+8.4f  %s' % (nm, wv, '█' * min(22, int(abs(wv) * 60))))


if __name__ == '__main__':
    main()
