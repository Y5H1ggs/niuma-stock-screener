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
import os, sys, io, time, math, random
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ds
import factors as FA      # ★ 复用 its `daily_factors` 作为**唯一口径来源**（P85：口径不得多份实现）

FALLBACK_POOL = [
    '000001', '600000', '600030', '000002', '600519', '000858', '600887', '601318',
    '600036', '000333', '600276', '002415', '600585', '601166', '600028', '601857',
    '601988', '601398', '600104', '000651', '002594', '600690', '601088', '600900',
    '601899', '600309', '002304', '600031', '000725', '601668',
]

# ★ v1.2.16b：特征名**直接就是 `factors.daily_factors()` 的键名** ——
#   model.py 不再自己实现任何指标。原先 44 个特征全部本地重写了一遍，
#   与 factors.py 各算一套（P85 明令禁止：口径多份实现 = 同一个坑踩两遍）。
FEATS = [
    # 均线 / 趋势
    'bull', 'bear', 'devma5', 'devma20', 'ma20_slope', 'dist_ma60', 'ma_align',
    'above_ma60_ratio', 'trend_r2', 'trend_slope', 'up_days20',
    # MACD
    'macd_dif', 'macd_dea', 'macd_hist', 'macd_hist_d', 'macd_above0', 'macd_gold',
    # KDJ
    'kdj_k', 'kdj_d', 'kdj_j', 'kdj_kd', 'kdj_gold', 'kdj_dead',
    # 摆动
    'rsi6', 'rsi14', 'cci20', 'wr14', 'pctb', 'mom5', 'mom10', 'mom20',
    # 布林 / 波动
    'boll_bw', 'atr14', 'vol20', 'vol_ratio_sl', 'ret_skew20', 'range_ratio', 'bw_pctile',
    # 风险
    'max_dd20', 'ulcer20', 'atr_ratio', 'down_side_vol',
    # 量能 / 量价
    'vr', 'vr5', 'amt_ratio', 'vol_cv20', 'vol_skew20', 'vol_hi_ratio',
    'vp_corr', 'obv_slope', 'mfi14', 'updown_vol',
    # 位置
    'pos60', 'pos250', 'hh_days', 'll_days', 'new_high20', 'new_high60', 'new_low20',
    'dist_hi20', 'dist_lo20', 'dist_hi250',
    # 资金
    'ff1_ratio', 'ff3_ratio', 'ff5_ratio', 'r0_ratio', 'ff_streak', 'ff_accel',
    'ff_up_ratio', 'ff_cv', 'turn_ratio', 'hs',
    # 涨停 / 形态
    'streak', 'limit_up20', 'limit_down20', 'days_since_lu',
    'upper', 'lower', 'gap', 'yang', 'body', 'close_pos', 'consec_up', 'body_ratio',
    'gap_up20', 'gap_dn20',
    # 相对强度（对大盘）
    'rs5', 'rs20', 'rs60', 'beta20', 'alpha20',
    # 当日涨幅
    'chg',
    # 财务（PIT）
    'roe', 'rev_yoy', 'np_yoy', 'gm', 'ocf_ps', 'pb_proxy', 'net_margin', 'rev_log',
]
HIDX = {1: 3, 3: 4, 5: 5}      # 行内 fwd 字段下标

_FIN_CACHE = {}


def _fin_at(fin, date):
    """PIT 取值：只返回 `date` 当天**已经公告**的最新一期财报（带缓存）。

    ⚠️ 用 `notice`（实际公告日）比较，**不能用 `report`（报告期）**——
       报告期 06-30 的财报若 08-20 才公告，那么 07-01 就使用它属于**前视泄漏**。
    """
    if not fin:
        return None
    key = (id(fin), date)
    if key in _FIN_CACHE:
        return _FIN_CACHE[key]
    cands = [x for x in fin if (x.get('notice') or '9999') <= date]
    r = max(cands, key=lambda x: x['report']) if cands else None
    if len(_FIN_CACHE) > 8000:
        _FIN_CACHE.clear()
    _FIN_CACHE[key] = r
    return r


def pool(n=30, verbose=True, pool_codes=None):
    # ★ 显式池：**做对照实验必须固定池子**。实测 clist 时通时断，
    #   两次运行可能落到不同样本（15886 条 vs 12078 条）→ 对比直接失效。
    if pool_codes:
        if verbose:
            print('  样本池：**显式指定** %d 只（固定 → 不同配置间可比）' % len(pool_codes))
        return list(pool_codes), 'explicit'
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
def feats(k, i, fl, fin=None, ix=None):
    """第 i 根 K 线**收盘时**的特征（严格只用 k[0..i]，无前视）。None = 剔除。

    ★ v1.2.16b：改为**薄封装** —— 直接消费 `factors.daily_factors()`。
      原先 model.py 自己实现了 44 个指标，与 factors.py 各算一套；P85 的教训正是
      "任何能被多个脚本各自重写的指标，都是下一个坑"。现在这里只做两件事：
      取键 + 完整性校验，**公式全部由 factors 单点维护**。
    返回 None 的情形：预热不足 / 任一特征缺失或为 nan（**剔除样本，绝不用 0 冒充**）。
    财务为 PIT（按公告日取值，无前视）；`ix` 为指数收盘映射，缺失时相对强度族为空 → 剔除。
    """
    if i < 250:
        return None
    f, _ = FA.daily_factors(k, i, fin, None, fl, ix)
    if not f:
        return None
    out = []
    for nm in FEATS:
        v = f.get(nm)
        if v is None or (isinstance(v, float) and v != v):
            return None
        out.append(float(v))
    return out


_IX = {}


def index_map():
    """上证指数「日期 → 收盘」映射（**带缓存** —— 逐票各取一次指数会白跑几十次请求）。"""
    if not _IX:
        ik = ds.kline_qq('sh000001', 800)
        if ik:
            _IX.update({x['d']: x['c'] for x in ik})
    return _IX


# ★ 逐时点价格上限（由 `--max-px` 设置；None = 不过滤）。
#   用模块级而非逐层传参的原因：build_panel / rank_today / train_final 三处都要用，
#   逐层传参会把签名搞乱，且**漏传一处就会静默不过滤**（比显式全局更危险）。
_G_MAX_PX = None


def build_panel(codes, verbose=True):
    rows, dropped = [], {'warmup': 0, 'nan': 0, 'fail': 0, 'px': 0}
    # ★ 指数基准（相对强度族的前提）：**必须显式写 sh000001**
    #   （裸码 `000001` 会被当成平安银行 —— 本项目老坑）
    ix = index_map()
    if verbose:
        print('    指数基准 sh000001 %d 根%s'
              % (len(ix), '' if ix else '  ⚠️ 取数失败 → 相对强度族将缺失，样本被剔除'))
    for n, c in enumerate(codes):
        k = ds.kline_qq(c, 800)
        fl = {}
        for x in ds.moneyflow_sina(c, 600):
            if x[0]:
                fl[x[0]] = {'net': x[1], 'r0': x[2], 'turnover': x[5] / 100.0}   # x[5] 是换手率×100
        fin = ds.financials(c)          # ★ P0-2：PIT 财务因子（内部按公告日取值，无前视）
        if not k or len(k) < 400 or not fl:
            dropped['fail'] += 1
            continue
        today = time.strftime('%Y-%m-%d')
        last = len(k) - 1 - (1 if k[-1]['d'] == today else 0)   # 最后一个【完整】交易日的下标
        for i in range(250, last + 1):
            # ★ 逐时点价格条件（**当日**收盘价 → 无前视；见 P91）
            if _G_MAX_PX is not None and k[i]['c'] > _G_MAX_PX:
                dropped['px'] += 1
                continue
            f = feats(k, i, fl, fin, ix)
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


def platt(logits, y, iters=400, lr=0.08):
    """Platt scaling：在**独立校准集**上拟合 `p' = σ(a·logit + b)`，修正概率校准。

    ★ 为什么必须做（10-09 实测的诊断）：**Brier 惩罚的是"校准"，AUC 只关心"排序"。**
      出现「AUC 0.514 > 0.5（排序确有微弱信息）却 Brier ≥ 基线（概率还不如一直说常数）」
      —— 这不是"没信号"，而是**概率被系统性高估/低估**。修校准即可把 Brier 拉回基线之下。
      （只改 a、b 两个参数，**不改排序**，所以 AUC 完全不变 —— 这也让它可被证伪。）
    """
    a, b = 1.0, 0.0
    n = max(1, len(y))
    for _ in range(iters):
        ga = gb = 0.0
        for z, yi in zip(logits, y):
            p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, a * z + b))))
            e = p - yi
            ga += e * z
            gb += e
        a -= lr * ga / n
        b -= lr * gb / n
    return a, b


def _blocks(rows, y, p, k=4):
    """按时间把测试集切 k 段、各段单算 AUC —— **检查"平均 AUC"会不会被某一段撑起来**。
    单一平均数最容易掩盖"某段时间完全无效甚至反向"。"""
    idx = sorted(range(len(rows)), key=lambda i: rows[i][0])
    out, step = [], max(1, len(idx) // k)
    for s in range(0, len(idx), step):
        seg = idx[s:s + step]
        if len(seg) < 50:
            continue
        yy = [y[i] for i in seg]; pp = [p[i] for i in seg]
        out.append((rows[seg[0]][0], rows[seg[-1]][0], len(seg), auc(yy, pp) or 0))
    return out


def evaluate(TR, TE, h, tag):
    j = HIDX[h]
    # ★ 过滤掉"还没有未来收益"的尾部样本（_g() 返回 None）——
    #   这些样本只用于「当日横截面」，**不能**参与训练/评估。
    TR = [r for r in TR if r[j] is not None]
    TE = [r for r in TE if r[j] is not None]
    if not TR or not TE:
        return dict(tag=tag, h=h, ntr=0, nte=0, base=0.0, act=0.0, br_m=1.0, br_cal=1.0,
                    br_b=1.0, auc=None, top=0.0, top_p=0.0, topn=0, w=[0.0] * len(FEATS),
                    platt=(1.0, 0.0), blocks=[], mu=None, sd=None)
    # ★ 三分：拟合权重 / 拟合 Platt 校准 / 评估。
    #   **校准集必须独立于评估集** —— 用测试集去调校准参数，就是把答案抄进答卷
    #   （与"随机切分"同族的泄漏，只是更隐蔽）。
    dts = sorted(set(r[0] for r in TR))
    cut_c = dts[int(len(dts) * 0.85)]
    TFit = [r for r in TR if r[0] < cut_c]
    TCal = [r for r in TR if r[0] >= cut_c]
    if len(TFit) < 300 or len(TCal) < 300:
        TFit, TCal = TR, TR

    def XY(rows):
        return [list(r[2]) for r in rows], [1.0 if r[j] > 0 else 0.0 for r in rows]

    Xf, yf = XY(TFit)
    Zf, mu, sd = standardize(Xf)
    w, b = fit(Zf, yf)

    def logits(rows):
        Z, _, _ = standardize([list(r[2]) for r in rows], mu, sd)
        return [b + sum(w[t] * r[t] for t in range(len(r))) for r in Z]

    yc = [1.0 if r[j] > 0 else 0.0 for r in TCal]
    a, bb = platt(logits(TCal), yc)
    lte = logits(TE)
    yte = [1.0 if r[j] > 0 else 0.0 for r in TE]

    def sig(z):
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))

    p_raw = [sig(z) for z in lte]
    p_cal = [sig(a * z + bb) for z in lte]
    base = sum(yf) / len(yf)
    k_, wr, mp = topk(yte, p_cal, 0.20)
    return dict(tag=tag, h=h, ntr=len(TR), nte=len(TE), base=base,
                act=sum(yte) / len(yte), br_m=brier(yte, p_raw),
                br_cal=brier(yte, p_cal), br_b=brier(yte, [base] * len(yte)),
                auc=auc(yte, p_raw), top=wr, top_p=mp, topn=k_,
                w=w, mu=mu, sd=sd, platt=(a, bb), blocks=_blocks(TE, yte, p_raw))


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
                base=oos['base'], base_brier=oos['br_b'], n=len(X), pool=pool_codes,
                tag=oos['tag'], dropped=dropped, raw=P, xs=X)


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
            fl[x[0]] = {'net': x[1], 'r0': x[2], 'turnover': x[5] / 100.0}   # x[5] 是换手率×100
    if not k or not fl:
        return None
    i = len(k) - 1 - (1 if k[-1]['d'] == time.strftime('%Y-%m-%d') else 0)
    if day:
        idx = [t for t in range(len(k)) if k[t]['d'] == day]
        if not idx:
            return None
        i = idx[0]
    f = feats(k, i, fl, ds.financials(code), index_map())
    if f is None or any(x != x for x in f):
        return None
    xr = _xsec_row(M, k[i]['d'], f)
    if xr is None:
        return None
    z = [(xr[j] - M['mu'][j]) / M['sd'][j] for j in range(len(xr))]
    return {'code': code, 'prob': proba(M['w'], M['b'], z),
            'date': k[i]['d'], 'close': k[i]['c']}


def explain(M, code):
    """单票**可复核归因**：模型是线性的 → `截距 + Σ(权重 × 标准化排名) = logit` → 概率。

    返回 `(rows, bias, logit, date, close)`，rows = [(特征, 横截面排名, 权重, 贡献)]（按贡献升序）。
    ★ 为什么必须能归因：一个给出"41.6%"却说不清**为什么**的模型，
      和掷骰子没有区别 —— 概率必须能被拆开、被逐项核对，才谈得上被信任。
    """
    k = ds.kline_qq(code, 800)
    fl = {}
    for x in ds.moneyflow_sina(code, 600):
        if x[0]:
            fl[x[0]] = {'net': x[1], 'r0': x[2], 'turnover': x[5] / 100.0}   # x[5] 是换手率×100
    if not k or not fl:
        return None
    i = len(k) - 1 - (1 if k[-1]['d'] == time.strftime('%Y-%m-%d') else 0)
    f = feats(k, i, fl, ds.financials(code), index_map())
    if f is None or any(x != x for x in f):
        return None
    xr = _xsec_row(M, k[i]['d'], f)
    if xr is None:
        return None
    rows, logit = [], M['b']
    for j, nm in enumerate(FEATS):
        z = (xr[j] - M['mu'][j]) / M['sd'][j]
        c = M['w'][j] * z
        logit += c
        rows.append((nm, xr[j], M['w'][j], c))
    rows.sort(key=lambda z: z[3])
    return rows, M['b'], logit, k[i]['d'], k[i]['c']


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
    global _G_MAX_PX
    uni, target, do_log = 30, None, ('--log' in sys.argv)
    feats_file = None
    pool_file = None
    for i, a in enumerate(sys.argv):
        if a == '--universe':
            uni = int(sys.argv[i + 1])
        if a == '--score':
            target = sys.argv[i + 1]
        if a == '--feats':
            feats_file = sys.argv[i + 1]
        if a == '--pool':
            pool_file = sys.argv[i + 1]
        if a == '--max-px':
            _G_MAX_PX = float(sys.argv[i + 1])
    # ★ `--feats FILE`：只保留文件里列出的特征 → **做受控对照实验**用。
    #   动机：特征从 44 扩到 100 后样本外指标全面退化，必须能回答
    #   "到底是加多了，还是新特征本身没用" —— 只有同一池子、同一时点的对照才算数。
    if feats_file:
        want = [l.strip() for l in io.open(feats_file, encoding='utf-8') if l.strip()]
        wset = set(want)
        keep = [x for x in FEATS if x in wset]
        miss = [x for x in want if x not in set(FEATS)]
        if keep:
            print('  ★ 特征子集生效：%d 个（原 %d 个）%s'
                  % (len(keep), len(FEATS), ('；忽略未知名 %d 个：%s' % (len(miss), miss[:6])) if miss else ''))
            FEATS[:] = keep
    print('=' * 104)
    print('【模型单票判断】%s' % target if target else '【多指标走势预测模型 · 样本外检验】')
    print('=' * 104)
    print('时间', time.strftime('%Y-%m-%d %H:%M:%S'),
          '| 特征 %d 个（MACD / KDJ / 资金 / 均线 / 摆动 / 量能 / 位置 / 形态）' % len(FEATS))
    pcs = None
    if pool_file:
        pcs = [l.strip() for l in io.open(pool_file, encoding='utf-8') if l.strip()]
    codes, src = pool(uni, pool_codes=pcs)

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
        _b = (M.get('base') or 0) * 100
        _dev = (r['prob'] * 100) - _b
        print('  ★ 模型 P(次一交易日收涨) = **%.1f%%**   ｜ 训练池基准率 %.1f%%   ｜ 偏离 **%+.1fpp**'
              % (r['prob'] * 100, _b, _dev))
        print('  ⚠️ **概率口径**：这是"相对基准率的偏离"，**不是方向判断**。'
              '偏离在 ±5pp 内 ≈ 模型"没有观点"。')
        if r['prob'] <= 0.001 or r['prob'] >= 0.999:
            print('  🔴 **概率饱和（0% / 100%）→ 判定为口径异常，本次结果作废。**')
            print('     任何"恰好 0 或 100%"的关键指标，先怀疑口径/取数失败，而不是当成极强信号。')
            return
        print('  ⚠️ 验证状态：该配置样本外 AUC **%.4f**（0.5 = 无区分度；本轮实测未通过）'
              % (M['auc'] or 0))
        if (M['auc'] or 0) < 0.53:
            print('  → **未通过有效性检验：此概率不得作为决策依据**，仅作记录与追踪之用。')
        print('  ⚠️ **信息截止**：只用 %s 的闭合日线特征 —— 今日盘中量价、盘中资金、'
              '盘后消息**均未纳入**。' % r['date'])
        print('-' * 104)

        if '--explain' in sys.argv:
            ex = explain(M, target)
            if ex:
                rows, bias, logit, d_, c_ = ex
                print('【归因 · 可逐项复核】模型是线性的：`截距 + Σ(权重 × 标准化排名) = logit`')
                print('  %-14s %11s %10s %11s' % ('特征', '横截面排名', '权重', '对 logit 贡献'))
                for nm, rk, w_, c in rows[:8]:
                    print('  %-14s %10.2f%% %+10.4f %+11.4f  ← 压低' % (nm, rk * 100, w_, c))
                print('  %-14s %11s %10s %11s' % ('…（中间 %d 项略）' % max(0, len(rows) - 12), '', '', ''))
                for nm, rk, w_, c in rows[-4:]:
                    print('  %-14s %10.2f%% %+10.4f %+11.4f  ← 抬高' % (nm, rk * 100, w_, c))
                ssum = sum(x[3] for x in rows)
                print('  截距 %+.4f ｜ Σ贡献 %+.4f ｜ logit %+.4f ｜ 概率 %.1f%%'
                      % (bias, ssum, bias + ssum, r['prob'] * 100))
                print('  校验：截距 + Σ贡献 == logit（可自行相加核对；对不上说明归因有漏项）')
                print()
        if do_log:
            rec = log_forecast(r, M, note='model.py --score')
            print('  已写入预测台账 id=%s → 到期 `calibrate.py settle` 自动结算' % rec['id'])
        return

    print('载入数据（腾讯 qfq 800 根 + 新浪资金 600 日）…')
    rows, dropped = build_panel(codes)
    _extra = ('、价格>%.2f %d' % (_G_MAX_PX, dropped['px'])) if _G_MAX_PX is not None else ''
    print('样本 %d 条 | 剔除：预热 %d、nan %d、取数失败 %d%s'
          % (len(rows), dropped['warmup'], dropped['nan'], dropped['fail'], _extra))
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
    print('%-20s %4s %9s %9s %11s %8s %11s %11s' % (
        '配置', 'h', 'Brier原', 'Brier校准', '基线Brier', 'AUC', 'Top20%实际', 'Top20%预测'))
    print('-' * 112)
    res = []
    for tag, TR, TE in (('① 绝对值特征', trA, teA), ('② 横截面标准化', trX, teX)):
        for h in (1, 3, 5):
            r = evaluate(TR, TE, h, tag)
            res.append(r)
            flag = '✅' if r['br_cal'] < r['br_b'] else '  '
            print('%-20s %4d %9.4f %9.4f %11.4f %8.4f %10.2f%% %10.2f%% %s'
                  % (tag, h, r['br_m'], r['br_cal'], r['br_b'], r['auc'] or 0,
                     r['top'] * 100, r['top_p'] * 100, flag))
    print('-' * 112)
    print('  「Brier校准」= 用**独立校准集**拟合的 Platt scaling（只改 a/b，不改排序 → AUC 不变）')
    print('  ✅ = Brier 已低于基线（接入财务因子后原先 6 个配置**全部 ≥ 基线**）')
    print()
    print('【分时段稳定性】—— 平均 AUC 会不会被某一段撑起来？')
    bconf = max(res, key=lambda r: (r['auc'] or 0))
    print('  （取 AUC 最高的配置：%s / h=%d）' % (bconf['tag'], bconf['h']))
    for b_ in bconf['blocks']:
        print('    %s ~ %s   n=%5d   AUC %.4f%s' % (b_[0], b_[1], b_[2], b_[3],
              '   ← 反向' if b_[3] < 0.5 else ''))
    print()
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
