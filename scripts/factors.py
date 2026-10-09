# -*- coding: utf-8 -*-
"""factors.py — 量化因子挖掘（因子有效性检验）

不是"再写一批形态"，而是**用横截面统计检验一个因子到底有没有信息量**。

方法（标准因子研究框架）：

  ① 横截面配对  每个交易日，对股票池算出各因子值，与**未来 N 日收益**配对；
  ② IC 序列     逐日算 Spearman 秩相关（因子值 vs 未来收益）→ 得到 IC 时间序列；
  ③ ICIR        = mean(IC) / std(IC)。**这是判断因子是否可用的核心指标**：
                  |IC| 均值 >0.03 且 ICIR >0.3 才算有边际信息；ICIR >0.5 算优秀；
  ④ IC 胜率     IC 与理论方向一致的天数占比（稳定性，比均值更抗极值）；
  ⑤ 分组单调性  按因子分 5 组，看各组平均未来收益是否**单调**。
                  只挑"最好的一组"＝挑樱桃，必须看整条曲线；
  ⑥ 因子相关性  两两相关矩阵，剔除同源重复（如 mom5 与 mom10 常常高度相关）。

三条纪律（写进代码，不靠自觉）：

  · **财务因子必须走 PIT**：用 `ds.fundamentals_at(code, 日期)` 取"那天能看到"的财报，
    否则 IC 是假的——前视偏差会让一个垃圾因子看起来神准；
  · **必须报样本量与显著性**：|IC| 的 t 值 = ICIR × √天数。t < 2 视为噪声，
    哪怕 IC 均值看起来很漂亮；
  · **分组要看单调性**，不只看首尾差。

用法：
  python factors.py --universe 40 --days 150        # 挖掘（默认）
  python factors.py --list                          # 列出因子库
  python factors.py 601169 --single                 # 单票因子时序（不算 IC）
  python factors.py --universe 60 --horizon 5      # 自定义样本池与持有期
"""
import sys
import io
import os
import math
import time
import random
import json

import ds
import cfg

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


# ------------------------------------------------------------------ 因子库
# 每个因子：名称 / 类别 / 取值函数(f=当日特征) / 理论方向(+1 表示值越大越好，-1 反之)
FACTORS = [
    # ---- 价格动量与反转
    ('mom5',      '动量',   lambda f: f['mom5'],      +1),
    ('mom10',     '动量',   lambda f: f['mom10'],     +1),
    ('mom20',     '动量',   lambda f: f['mom20'],     +1),
    ('rev1',      '反转',   lambda f: -f['chg'],      +1),   # 昨日跌得多 → 期望反弹
    ('mom5_rev',  '反转',   lambda f: -f['mom5'],     +1),
    # ---- 均线位置
    ('devma5',    '均线',   lambda f: f['devma5'],    -1),   # 偏离越大越回归
    ('devma20',   '均线',   lambda f: f['devma20'],   -1),
    ('above20',   '均线',   lambda f: 1.0 if f['above20'] else 0.0, +1),
    ('bull',      '均线',   lambda f: 1.0 if f['bull'] else 0.0,    +1),
    # ---- 摆动指标
    ('rsi14',     '摆动',   lambda f: f['rsi14'],     -1),
    ('cci20',     '摆动',   lambda f: f['cci20'],     -1),
    ('wr14',      '摆动',   lambda f: f['wr14'],      +1),
    ('pctb',      '摆动',   lambda f: f['pctb'],      -1),
    # ---- 位置
    ('pos60',     '位置',   lambda f: f['pos60'],     -1),
    ('pos250',    '位置',   lambda f: f['pos250'],    -1),
    ('dist_hi250', '位置',  lambda f: f['dist_hi250'], +1),  # 距 250 日高越远（负得越多）越可能反抽
    # ---- 量能
    ('vr',        '量能',   lambda f: f['vr'],        +1),
    ('vr5',       '量能',   lambda f: f['vr5'],       +1),
    ('amt_log',   '量能',   lambda f: math.log(f['amt']) if f['amt'] > 0 else None, +1),
    ('turnover',  '量能',   lambda f: f['hs'],        -1),
    # ---- K线形态
    ('upper',     '形态',   lambda f: f['upper'],     -1),   # 上影长 → 抛压
    ('lower',     '形态',   lambda f: f['lower'],     +1),
    ('gap',       '形态',   lambda f: f['gap'],       -1),
    ('yang',      '形态',   lambda f: 1.0 if f['yang'] else 0.0, +1),
    ('body',      '形态',   lambda f: f['body'],      +1),
    ('streak',    '涨停',   lambda f: f['streak'],    +1),
    # ---- 波动
    ('atr14',     '波动',   lambda f: f['atr14'],     -1),
    ('vol20',     '波动',   lambda f: f['vol20'],     -1),   # 收益波动率
    # ---- 财务（PIT）
    ('roe',       '财务',   lambda f: f['roe'],       +1),
    ('rev_yoy',   '财务',   lambda f: f['rev_yoy'],   +1),
    ('np_yoy',    '财务',   lambda f: f['np_yoy'],    +1),
    ('gross_margin', '财务', lambda f: f['gm'],       +1),
    ('ocf_ps',    '财务',   lambda f: f['ocf_ps'],    +1),
    # ============ v1.2.16 新增因子（方向按**传统技术分析先验**设定）============
    # ⚠️ 方向只用于"实测符号是否与理论一致"的标记，**不参与 IC 计算**。
    #   刻意**不按"已知反转效应"倒推方向** —— 若预先把方向设成与已知结论一致，
    #   就失去了用实测打脸自己的机会（自证倾向）。让数据说话。
    # ---- 量价关系
    ('vp_corr',   '量价',   lambda f: f['vp_corr'],   +1),   # 量价正相关=配合
    ('obv_slope', '量价',   lambda f: f['obv_slope'], +1),   # 能量潮上行
    ('mfi14',     '量价',   lambda f: f['mfi14'],     -1),   # >80 超买
    ('updown_vol', '量价',  lambda f: f['updown_vol'], +1),  # 放量上攻
    ('amt_ratio', '量价',   lambda f: f['amt_ratio'], +1),   # 近5日放量
    # ---- 波动结构
    ('bw_pctile', '波动',   lambda f: f['bw_pctile'], -1),   # 带宽低分位=挤压
    ('vol_ratio_sl', '波动', lambda f: f['vol_ratio_sl'], -1),  # 短期波动放大
    ('ret_skew20', '波动',  lambda f: f['ret_skew20'], +1),
    ('range_ratio', '波动', lambda f: f['range_ratio'], -1),  # 当日振幅异常
    # ---- 趋势强度
    ('ma20_slope', '趋势',  lambda f: f['ma20_slope'], +1),
    ('up_days20',  '趋势',  lambda f: f['up_days20'],  +1),
    ('dist_ma60',  '趋势',  lambda f: f['dist_ma60'],  -1),  # 偏离 MA60
    # ---- 资金结构
    ('r0_ratio',   '资金',  lambda f: f['r0_ratio'],   +1),   # 特大单净额占比
    ('ff_streak',  '资金',  lambda f: f['ff_streak'],  +1),   # 连续净流入
    ('ff_accel',   '资金',  lambda f: f['ff_accel'],   +1),   # 资金加速
    ('ff_up_ratio', '资金', lambda f: f['ff_up_ratio'], +1),
    ('ff_cv',      '资金',  lambda f: f['ff_cv'],      -1),   # 资金流不稳定
    ('turn_ratio', '资金',  lambda f: f['turn_ratio'], -1),   # 换手放大
    ('ff1_ratio',  '资金',  lambda f: f['ff1_ratio'],  +1),   # 单日主力净额/成交额
    ('ff3_ratio',  '资金',  lambda f: f['ff3_ratio'],  +1),   # 近 3 日
    ('ff5_ratio',  '资金',  lambda f: f['ff5_ratio'],  +1),   # 近 5 日
    # ---- 日内 / 形态
    ('close_pos',  '日内',  lambda f: f['close_pos'],  +1),   # 收在当日高位
    ('consec_up',  '形态',  lambda f: f['consec_up'],  -1),   # 连阳=短期过热
    ('body_ratio', '形态',  lambda f: f['body_ratio'], +1),
    # ---- 财务派生（PIT）
    ('pb_proxy',   '财务',  lambda f: f['pb_proxy'],   -1),   # 低 PB 更好
    ('net_margin', '财务',  lambda f: f['net_margin'], +1),
    ('rev_log',    '财务',  lambda f: f['rev_log'],    +1),
    # ============ v1.2.16b 新增 28 个 ============
    # ---- 相对强度（对大盘）★ 对应「板块相对强度」铁律的指数层量化
    ('rs5',        '相对强度', lambda f: f['rs5'],     +1),
    ('rs20',       '相对强度', lambda f: f['rs20'],    +1),
    ('rs60',       '相对强度', lambda f: f['rs60'],    +1),
    ('beta20',     '相对强度', lambda f: f['beta20'],  +1),
    ('alpha20',    '相对强度', lambda f: f['alpha20'], +1),
    # ---- 序列位置
    ('hh_days',    '位置',  lambda f: f['hh_days'],    -1),   # 越接近新高越强
    ('ll_days',    '位置',  lambda f: f['ll_days'],    +1),   # 离低点越久越好
    ('new_high20', '位置',  lambda f: f['new_high20'], +1),
    ('new_high60', '位置',  lambda f: f['new_high60'], +1),
    ('new_low20',  '位置',  lambda f: f['new_low20'],  -1),
    # ---- 趋势质量
    ('trend_r2',   '趋势',  lambda f: f['trend_r2'],   +1),   # 趋势越"干净"越好
    ('trend_slope', '趋势', lambda f: f['trend_slope'], +1),
    ('above_ma60_ratio', '趋势', lambda f: f['above_ma60_ratio'], +1),
    # ---- 风险 / 回撤
    ('max_dd20',   '风险',  lambda f: f['max_dd20'],   +1),   # 值越小(负)越糟 → 越大越好
    ('ulcer20',    '风险',  lambda f: f['ulcer20'],    -1),
    ('atr_ratio',  '风险',  lambda f: f['atr_ratio'],  -1),   # 波动放大
    ('down_side_vol', '风险', lambda f: f['down_side_vol'], -1),
    # ---- 量能结构
    ('vol_cv20',   '量能',  lambda f: f['vol_cv20'],   -1),
    ('vol_skew20', '量能',  lambda f: f['vol_skew20'], +1),
    ('vol_hi_ratio', '量能', lambda f: f['vol_hi_ratio'], +1),
    # ---- 涨停结构
    ('limit_up20', '涨停',  lambda f: f['limit_up20'], +1),
    ('limit_down20', '涨停', lambda f: f['limit_down20'], -1),
    ('days_since_lu', '涨停', lambda f: f['days_since_lu'], -1),
    # ---- 支撑压力
    ('dist_hi20',  '位置',  lambda f: f['dist_hi20'],  +1),   # 越接近 20 日高越好
    ('dist_lo20',  '位置',  lambda f: f['dist_lo20'],  +1),
    ('ma_align',   '均线',  lambda f: f['ma_align'],   +1),   # 均线多头强度 0~3
    # ---- 缺口
    ('gap_up20',   '形态',  lambda f: f['gap_up20'],   +1),
    ('gap_dn20',   '形态',  lambda f: f['gap_dn20'],   -1),
]


# ------------------------------------------------------------------ 统计工具
def _rank(v):
    order = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def spearman(x, y):
    n = len(x)
    if n < 5:
        return None
    rx, ry = _rank(x), _rank(y)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return num / (dx * dy) if dx > 0 and dy > 0 else None


def mean(v):
    return sum(v) / len(v) if v else 0.0


def std(v):
    if len(v) < 2:
        return 0.0
    m = mean(v)
    return math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))


def norm_ppf(p):
    """标准正态分位函数（Acklam 近似，误差 < 1e-9）。
    用途：多重比较校正 —— 同时检验 N 个因子时，Bonferroni 阈值 = Φ⁻¹(1 − 0.05/(2N))。"""
    if p <= 0 or p >= 1:
        return 0.0
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


# ------------------------------------------------------------------ 单日因子快照
def daily_factors(k, i, fin, last_fin=None, fl=None, ix=None):
    """第 i 根 K 线收盘时的因子值。财务因子走 PIT（只看到当时已公告的）。

    ★ v1.2.16：新增 `fl` —— 新浪资金流序列 `{日期: {'net','r0','turnover'}}`。
      **必须显式传入**才计算资金类因子；不传时资金因子为 None（由 IC 跳过），
      **绝不填 0 冒充**（P84/P85 同族：「查不到」与「等于零」长得一样，
      混淆会让模型把"没有数据"读成"资金持平"）。
    """
    if i < 60:
        return None, last_fin
    c = k[i]['c']
    o = k[i]['o']
    h = k[i]['h']
    l = k[i]['l']
    prev = k[i - 1]['c']
    cl = [k[j]['c'] for j in range(i + 1)]
    hi = [k[j]['h'] for j in range(i + 1)]
    lo = [k[j]['l'] for j in range(i + 1)]
    ma5, ma10, ma20, ma60 = ds.ma(cl, 5), ds.ma(cl, 10), ds.ma(cl, 20), ds.ma(cl, 60)
    b = ds.boll(cl)
    # ★ v1.2.16b：MACD / KDJ 也在此处统一计算 —— 让 `daily_factors` 成为
    #   **唯一的口径来源**（P85 教训：任何能被多个脚本各自重写的指标，都是下一个坑）。
    #   model.py 不再自己实现一遍，直接消费这里的结果。
    m_ = ds.macd(cl)
    mp_ = ds.macd(cl[:-1])
    kk_, dd_, jj_ = ds.kdj(hi, lo, cl)
    kp_, dp_, _ = ds.kdj(hi[:-1], lo[:-1], cl[:-1])
    vols = [k[j]['v'] for j in range(max(0, i - 20), i)]
    avgv = mean(vols) if vols else 0

    # 涨停 streak
    st = 0
    for j in range(i, 0, -1):
        if k[j - 1]['c'] and k[j]['c'] / k[j - 1]['c'] - 1 >= 0.095:
            st += 1
        else:
            break
    # ATR / 波动率
    trs = []
    for j in range(max(1, i - 13), i + 1):
        trs.append(max(k[j]['h'] - k[j]['l'],
                       abs(k[j]['h'] - k[j - 1]['c']),
                       abs(k[j]['l'] - k[j - 1]['c'])))
    atr14 = (mean(trs) / c * 100) if c else 0
    rets = [(k[j]['c'] / k[j - 1]['c'] - 1) for j in range(max(1, i - 19), i + 1)]
    vol20 = std(rets) * 100

    # ================= v1.2.16 新增因子：量价 / 波动 / 趋势 / 资金 / 形态 =================
    # 全部只用 k[0..i]（无前视）；窗口不足一律 None（由 compute_ic 跳过）
    v_ = k[i]['v']
    amt_i = v_ * 100 * c

    # --- 量价关系 ---
    _w = min(20, i)
    _chgs = [(k[j]['c'] / k[j - 1]['c'] - 1) * 100 for j in range(i - _w + 1, i + 1)]
    _vols = [k[j]['v'] for j in range(i - _w + 1, i + 1)]
    vp_corr = spearman(_chgs, _vols) if _w >= 10 else None
    # OBV（能量潮）：涨日加量、跌日减量；取 20 日斜率对当日量的归一化
    _obv, _o = [], 0.0
    for j in range(max(1, i - 20), i + 1):
        _o += k[j]['v'] if k[j]['c'] > k[j - 1]['c'] else (-k[j]['v'] if k[j]['c'] < k[j - 1]['c'] else 0.0)
        _obv.append(_o)
    obv_slope = ((_obv[-1] - _obv[0]) / (_w * mean(_vols))) if len(_obv) >= 10 and mean(_vols) > 0 else None
    # MFI(14)：典型价×量的资金流强度（0~100）
    mfi14 = None
    if i >= 15:
        pos_f = neg_f = 0.0
        for j in range(i - 13, i + 1):
            tp = (k[j]['h'] + k[j]['l'] + k[j]['c']) / 3.0
            tp_p = (k[j - 1]['h'] + k[j - 1]['l'] + k[j - 1]['c']) / 3.0
            mf = tp * k[j]['v']
            if tp > tp_p:
                pos_f += mf
            elif tp < tp_p:
                neg_f += mf
        mfi14 = (100.0 * pos_f / (pos_f + neg_f)) if (pos_f + neg_f) > 0 else None
    # 上涨日成交量 / 下跌日成交量（>1 = 放量上攻）
    _uv = sum(k[j]['v'] for j in range(i - 19, i + 1) if k[j]['c'] > k[j - 1]['c'])
    _dv = sum(k[j]['v'] for j in range(i - 19, i + 1) if k[j]['c'] < k[j - 1]['c'])
    updown_vol = (_uv / _dv) if (_dv > 0 and i >= 20) else None
    _v5m, _v20m = mean(vols[-5:]) if len(vols) >= 5 else 0, mean(vols[-20:]) if len(vols) >= 20 else 0
    amt_ratio = (_v5m / _v20m) if _v20m > 0 else None

    # --- 波动结构 ---
    # 布林带宽序列直接算（带宽 = (上轨−下轨)/中轨 = 4σ/mean），比循环调 120 次 ds.boll() 快一个量级
    bw_pctile = None
    if i >= 120:
        _bws = []
        for j in range(i - 119, i + 1):
            _w = cl[j - 19:j + 1]
            _mw = mean(_w)
            if len(_w) == 20 and _mw > 0:
                _bws.append(4 * std(_w) / _mw * 100)
        if len(_bws) >= 60:
            _cur = _bws[-1]
            bw_pctile = sum(1 for x in _bws if x <= _cur) / len(_bws) * 100
    _r5 = [(k[j]['c'] / k[j - 1]['c'] - 1) for j in range(max(1, i - 4), i + 1)]
    _s5, _s20 = std(_r5) * 100, vol20
    vol_ratio_sl = (_s5 / _s20) if _s20 > 0 else None
    ret_skew20 = None
    if len(rets) >= 10:
        _m, _s = mean(rets), std(rets)
        ret_skew20 = (sum(((x - _m) / _s) ** 3 for x in rets) / len(rets)) if _s > 0 else None
    _rngs = [(k[j]['h'] - k[j]['l']) / k[j - 1]['c'] * 100 for j in range(i - 19, i + 1)]
    _cur_rng = (h - l) / prev * 100 if prev else 0
    range_ratio = (_cur_rng / mean(_rngs)) if (i >= 20 and mean(_rngs) > 0) else None

    # --- 趋势强度 ---
    ma20_slope = None
    if i >= 40:
        _m20p = mean(cl[i - 39:i - 19])
        ma20_slope = ((ma20 / _m20p - 1) * 100) if _m20p > 0 else None
    up_days20 = sum(1 for j in range(i - 19, i + 1) if k[j]['c'] > k[j - 1]['c']) if i >= 20 else None
    dist_ma60 = ((c / ma60 - 1) * 100) if ma60 else None

    # --- 资金结构（需 fl；不传则 None，**不填 0**）---
    r0_ratio = ff_streak = ff_accel = ff_up_ratio = ff_cv = turn_ratio = None
    ff1_ratio = ff3_ratio = ff5_ratio = None
    if fl:
        def _mk(j):
            return fl.get(k[j]['d']) or {}
        _nets, _r0s, _turns = [], [], []
        for j in range(max(0, i - 19), i + 1):
            d_ = _mk(j)
            if d_.get('net') is not None:
                _nets.append(d_['net'])
            if d_.get('r0') is not None:
                _r0s.append(d_['r0'])
            if d_.get('turnover') is not None:
                _turns.append(d_['turnover'])
        _cur = _mk(i)
        if _cur.get('r0') is not None and amt_i > 0:
            r0_ratio = _cur['r0'] / amt_i
        # 单日 / 近3日 / 近5日 主力净额占成交额比（模型原先自己实现了一版，现统一到此）
        if _cur.get('net') is not None and amt_i > 0:
            ff1_ratio = _cur['net'] / amt_i
        _s3n = _s3a = _s5n = _s5a = 0.0
        for _j in range(max(0, i - 4), i + 1):
            _dd = _mk(_j)
            _aa = k[_j]['v'] * 100 * k[_j]['c']
            if _dd.get('net') is not None:
                if _j >= i - 2:
                    _s3n += _dd['net']
                    _s3a += _aa
                _s5n += _dd['net']
                _s5a += _aa
        ff3_ratio = (_s3n / _s3a) if _s3a > 0 else None
        ff5_ratio = (_s5n / _s5a) if _s5a > 0 else None
        if len(_nets) >= 20:
            _mn = mean(_nets)
            if _mn != 0:
                ff_cv = std(_nets) / abs(_mn)
            ff_up_ratio = sum(1 for x in _nets if x > 0) / len(_nets)
        # 连续净流入（正）/流出（负）天数
        ff_streak = 0
        for j in range(i, max(0, i - 20), -1):
            n_ = _mk(j).get('net')
            if n_ is None:
                break
            if not ff_streak:
                ff_streak = 1 if n_ > 0 else -1
            elif (n_ > 0) == (ff_streak > 0):
                ff_streak += 1 if ff_streak > 0 else -1
            else:
                break
        if len(_nets) >= 10:
            _m3, _m10 = mean(_nets[-3:]), mean(_nets[-10:])
            ff_accel = (_m3 / _m10 - 1) if _m10 != 0 else None
        if len(_turns) >= 20:
            _t5 = mean(_turns[-5:]) if len(_turns) >= 5 else 0
            _t20 = mean(_turns[-20:])
            turn_ratio = (_t5 / _t20) if _t20 > 0 else None

    # --- 日内结构 / 形态 ---
    close_pos = ((c - l) / (h - l)) if h > l else None
    consec_up = 0
    for j in range(i, max(0, i - 10), -1):
        if k[j]['c'] > k[j]['o']:
            consec_up += 1
        else:
            break
    _bodies = [abs(k[j]['c'] - k[j]['o']) / k[j]['o'] * 100 for j in range(i - 19, i + 1) if k[j]['o']]
    body_ratio = (abs(c - o) / o * 100 / mean(_bodies)) if (i >= 20 and mean(_bodies) > 0) else None

    # ============ v1.2.16b 新增：相对强度 / 位置 / 趋势质量 / 风险 / 量能结构 / 涨停结构 ============
    # --- 相对强度族（需 ix = {日期: 指数收盘}；不传 → 全部 None，**不填 0**）---
    # ★ 这是「板块相对强度」铁律在**指数层面**的量化版本 —— 个股跑赢/跑输大盘多少。
    rs5 = rs20 = rs60 = beta20 = alpha20 = None
    if ix:
        def _ir(n):
            a, b_ = ix.get(k[i - n]['d']), ix.get(k[i]['d'])
            return ((b_ / a - 1) * 100) if (a and b_ and a > 0) else None
        for _n in (5, 20, 60):
            if i >= _n:
                _v = _ir(_n)
                _v = ((c / k[i - _n]['c'] - 1) * 100 - _v) if _v is not None else None
                if _n == 5:
                    rs5 = _v
                elif _n == 20:
                    rs20 = _v
                else:
                    rs60 = _v
        if i >= 21:
            _xs, _ys = [], []
            for _j in range(i - 19, i + 1):
                _a, _b = ix.get(k[_j - 1]['d']), ix.get(k[_j]['d'])
                if _a and _b and _a > 0 and k[_j - 1]['c']:
                    _xs.append(_b / _a - 1)
                    _ys.append(k[_j]['c'] / k[_j - 1]['c'] - 1)
            if len(_xs) >= 15:
                _mx, _my = mean(_xs), mean(_ys)
                _vx = sum((x - _mx) ** 2 for x in _xs)
                if _vx > 0:
                    beta20 = sum((x - _mx) * (y - _my) for x, y in zip(_xs, _ys)) / _vx
                    alpha20 = (_my - beta20 * _mx) * 20 * 100      # 20 日累计超额 %

    # --- 序列位置族（距高/低点的天数：0 = 今天就是那个点）---
    hh_days = ll_days = new_high20 = new_high60 = new_low20 = None
    if i >= 250:
        _w250, _l250 = hi[i - 249:i + 1], lo[i - 249:i + 1]
        hh_days = _w250[::-1].index(max(_w250))
        ll_days = _l250[::-1].index(min(_l250))
        new_high20 = 1.0 if h >= max(_w250[-20:]) else 0.0
        new_high60 = 1.0 if h >= max(_w250[-60:]) else 0.0
        new_low20 = 1.0 if l <= min(_l250[-20:]) else 0.0

    # --- 趋势质量（R² = 趋势有多"干净"；斜率 = 归一化坡度）---
    trend_r2 = trend_slope = above_ma60_ratio = None
    if i >= 20:
        _ys = cl[i - 19:i + 1]
        _xs = list(range(20))
        _mx, _my = mean(_xs), mean(_ys)
        _vx = sum((x - _mx) ** 2 for x in _xs)
        _vy = sum((y - _my) ** 2 for y in _ys)
        _cov = sum((x - _mx) * (y - _my) for x, y in zip(_xs, _ys))
        if _vx > 0 and _vy > 0:
            trend_r2 = _cov ** 2 / (_vx * _vy)
        if _vx > 0 and _my > 0:
            trend_slope = (_cov / _vx) / _my * 100
    if i >= 80:
        _ab = 0
        for _j in range(i - 19, i + 1):
            _w = cl[_j - 59:_j + 1]
            if len(_w) == 60 and cl[_j] > mean(_w):
                _ab += 1
        above_ma60_ratio = _ab / 20.0 * 100

    # --- 风险 / 回撤 ---
    max_dd20 = ulcer20 = atr_ratio = down_side_vol = None
    if i >= 20:
        _pk, _dds = cl[i - 20], []
        for _j in range(i - 19, i + 1):
            _pk = max(_pk, cl[_j])
            _dds.append((cl[_j] / _pk - 1) * 100 if _pk else 0.0)
        max_dd20 = min(_dds)
        ulcer20 = math.sqrt(sum(x * x for x in _dds) / len(_dds))
        _dr = [min(0.0, k[_j]['c'] / k[_j - 1]['c'] - 1) for _j in range(max(1, i - 19), i + 1)]
        down_side_vol = math.sqrt(sum(x * x for x in _dr) / len(_dr)) * 100 if _dr else None
    if i >= 60:
        _atrs = []
        for _j in range(i - 59, i + 1):
            _atrs.append(max(k[_j]['h'] - k[_j]['l'], abs(k[_j]['h'] - k[_j - 1]['c']),
                             abs(k[_j]['l'] - k[_j - 1]['c'])))
        _a60 = mean(_atrs)
        atr_ratio = ((atr14 / 100 * c) / _a60) if _a60 > 0 else None

    # --- 量能结构 ---
    vol_cv20 = vol_skew20 = vol_hi_ratio = None
    if i >= 20:
        _v20s = [k[_j]['v'] for _j in range(i - 19, i + 1)]
        _mv, _sv = mean(_v20s), std(_v20s)
        vol_cv20 = (_sv / _mv) if _mv > 0 else None
        vol_skew20 = (sum(((x - _mv) / _sv) ** 3 for x in _v20s) / len(_v20s)) if _sv > 0 else None
        _vmax = max(k[_j]['v'] for _j in range(max(0, i - 59), i + 1))
        vol_hi_ratio = (v_ / _vmax) if _vmax > 0 else None

    # --- 涨停结构 ---
    limit_up20 = limit_down20 = None
    days_since_lu = None
    if i >= 20:
        limit_up20 = sum(1 for _j in range(i - 19, i + 1)
                         if k[_j - 1]['c'] and k[_j]['c'] / k[_j - 1]['c'] - 1 >= 0.095)
        limit_down20 = sum(1 for _j in range(i - 19, i + 1)
                           if k[_j - 1]['c'] and k[_j]['c'] / k[_j - 1]['c'] - 1 <= -0.095)
        days_since_lu = 99
        for _j in range(i, max(0, i - 60), -1):
            if k[_j - 1]['c'] and k[_j]['c'] / k[_j - 1]['c'] - 1 >= 0.095:
                days_since_lu = i - _j
                break

    # --- 支撑压力 ---
    dist_hi20 = dist_lo20 = ma_align = None
    if i >= 20:
        _h20, _l20 = max(hi[i - 19:i + 1]), min(lo[i - 19:i + 1])
        dist_hi20 = ((c / _h20 - 1) * 100) if _h20 else None
        dist_lo20 = ((c / _l20 - 1) * 100) if _l20 else None
    if ma60:
        ma_align = sum([1.0 if ma5 > ma10 else 0.0, 1.0 if ma10 > ma20 else 0.0,
                        1.0 if ma20 > ma60 else 0.0])

    # --- 缺口 ---
    gap_up20 = gap_dn20 = None
    if i >= 20:
        gap_up20 = sum(1 for _j in range(i - 19, i + 1)
                       if k[_j - 1]['h'] and k[_j]['l'] > k[_j - 1]['h'])
        gap_dn20 = sum(1 for _j in range(i - 19, i + 1)
                       if k[_j - 1]['l'] and k[_j]['h'] < k[_j - 1]['l'])

    f = {
        'chg': (c / prev - 1) * 100 if prev else 0,
        'mom5': (c / cl[-6] - 1) * 100 if len(cl) > 6 else 0,
        'mom10': (c / cl[-11] - 1) * 100 if len(cl) > 11 else 0,
        'mom20': (c / cl[-21] - 1) * 100 if len(cl) > 21 else 0,
        'devma5': (c / ma5 - 1) * 100 if ma5 else 0,
        'devma20': (c / ma20 - 1) * 100 if ma20 else 0,
        'above20': c > ma20, 'bull': ma5 > ma10 > ma20 > ma60,
        'rsi14': ds.rsi(cl, 14), 'cci20': ds.cci(hi, lo, cl, 20),
        'wr14': ds.wr(hi, lo, cl, 14), 'pctb': b['pctb'],
        # ⚠️ P84：pos60 / pos250 在历史不足时返回 None（不再用缩窗口的数字冒充）。
        #   本模块的 IC 计算**已经会跳过 None**（`compute_ic` 里 `if v is None ... continue`），
        #   所以保持 None 是对的 —— 如实剔除样本，好过喂一个假位置进去。
        'pos60': ds.pos(hi, lo, c, 60), 'pos250': ds.pos(hi, lo, c, 250),
        # dist_hi250 同族：`hi[-250:]` 在不足 250 根时会静默退化 → 必须同样置 None
        'dist_hi250': ((c / max(hi[-250:]) - 1) * 100) if len(hi) >= 250 else None,
        'vr': (k[i]['v'] / avgv) if avgv else 1.0,
        'vr5': (k[i]['v'] / mean([k[j]['v'] for j in range(max(0, i - 5), i)])) if i >= 6 else 1.0,
        # ★ 原为 `'amt': 0.0, 'hs': 0.0` —— 于是 `amt_log`（`log(amt) if amt>0 else None`）
        #   与 `turnover` 两个因子**恒定不变**，算 IC 时方差为 0 → 静默返回 None，
        #   **看起来像"这是噪声因子"，实际是"根本没喂数据"**（与 P84/P85 同一族：
        #   "没有"被伪装成"无效"）。修法：amt 用 量×100×收盘 估算（腾讯 K 线无成交额字段，
        #   已在文档声明）；turnover 无来源 → 置 **None**，由 compute_ic 跳过，
        #   宁可显示"缺数据"，也不要一个假 0。
        'amt': k[i]['v'] * 100 * c,
        # ★ v1.2.16b：换手率改从新浪资金流取（它带真实 turnover 字段）。
        #   原先硬写 None → `turnover` 因子恒为噪声（属 P84/P85 一族：
        #   "没喂数据"被显示成"这个因子没用"）。不传 fl 时仍为 None，绝不填 0。
        'hs': ((fl.get(k[i]['d']) or {}).get('turnover') if fl else None),
        'upper': (h - max(o, c)) / c * 100 if c else 0,
        'lower': (min(o, c) - l) / c * 100 if c else 0,
        'gap': (o / prev - 1) * 100 if prev else 0,
        'yang': c > o, 'body': (c - o) / o * 100 if o else 0,
        'streak': st, 'atr14': atr14, 'vol20': vol20,
        # ---- v1.2.16 新增（量价 / 波动 / 趋势 / 资金 / 日内 / 形态）----
        'vp_corr': vp_corr, 'obv_slope': obv_slope, 'mfi14': mfi14,
        'updown_vol': updown_vol, 'amt_ratio': amt_ratio,
        'bw_pctile': bw_pctile, 'vol_ratio_sl': vol_ratio_sl,
        'ret_skew20': ret_skew20, 'range_ratio': range_ratio,
        'ma20_slope': ma20_slope, 'up_days20': up_days20, 'dist_ma60': dist_ma60,
        'r0_ratio': r0_ratio, 'ff_streak': ff_streak, 'ff_accel': ff_accel,
        'ff_up_ratio': ff_up_ratio, 'ff_cv': ff_cv, 'turn_ratio': turn_ratio,
        'close_pos': close_pos, 'consec_up': consec_up, 'body_ratio': body_ratio,
        # ---- v1.2.16b 新增（相对强度 / 位置 / 趋势质量 / 风险 / 量能 / 涨停 / 支撑压力 / 缺口）----
        'rs5': rs5, 'rs20': rs20, 'rs60': rs60, 'beta20': beta20, 'alpha20': alpha20,
        'hh_days': hh_days, 'll_days': ll_days,
        'new_high20': new_high20, 'new_high60': new_high60, 'new_low20': new_low20,
        'trend_r2': trend_r2, 'trend_slope': trend_slope, 'above_ma60_ratio': above_ma60_ratio,
        'max_dd20': max_dd20, 'ulcer20': ulcer20, 'atr_ratio': atr_ratio,
        'down_side_vol': down_side_vol,
        'vol_cv20': vol_cv20, 'vol_skew20': vol_skew20, 'vol_hi_ratio': vol_hi_ratio,
        'limit_up20': limit_up20, 'limit_down20': limit_down20, 'days_since_lu': days_since_lu,
        'dist_hi20': dist_hi20, 'dist_lo20': dist_lo20, 'ma_align': ma_align,
        'gap_up20': gap_up20, 'gap_dn20': gap_dn20,
        # ---- v1.2.16b：模型消费所需（MACD / KDJ / 资金强度 / 其它）—— 统一口径来源 ----
        'macd_dif': m_['dif'], 'macd_dea': m_['dea'], 'macd_hist': m_['macd'],
        'macd_hist_d': ((m_['macd'] - mp_['macd'])
                        if (m_['macd'] is not None and mp_['macd'] is not None) else None),
        'macd_above0': 1.0 if (m_['dif'] is not None and m_['dif'] > 0) else 0.0,
        'macd_gold': 1.0 if (m_['macd'] is not None and mp_['macd'] is not None
                             and mp_['macd'] <= 0 < m_['macd']) else 0.0,
        'kdj_k': kk_ if kk_ == kk_ else None,
        'kdj_d': dd_ if dd_ == dd_ else None,
        'kdj_j': jj_ if jj_ == jj_ else None,
        'kdj_kd': ((kk_ - dd_) if (kk_ == kk_ and dd_ == dd_) else None),
        'kdj_gold': 1.0 if (kp_ == kp_ and kk_ == kk_ and kp_ <= dp_ and kk_ > dd_) else 0.0,
        'kdj_dead': 1.0 if (kp_ == kp_ and kk_ == kk_ and kp_ >= dp_ and kk_ < dd_) else 0.0,
        'rsi6': ds.rsi(cl, 6),
        'boll_bw': ((b['up'] - b['low']) / b['mid'] * 100) if b.get('mid') else None,
        'bear': 1.0 if ma5 < ma10 < ma20 else 0.0,
        'ff1_ratio': ff1_ratio, 'ff3_ratio': ff3_ratio, 'ff5_ratio': ff5_ratio,
    }
    # ---- 财务 PIT（只在"可见财报发生变化"时更新，避免每日重复比较）
    d = k[i]['d']
    nf = _fin_at(fin, d)
    if last_fin is None or last_fin[1] is not nf:
        last_fin = (d, nf)
    cur = nf
    f['roe'] = (cur or {}).get('roe') or None
    f['rev_yoy'] = (cur or {}).get('rev_yoy') or None
    f['np_yoy'] = (cur or {}).get('np_yoy') or None
    f['gm'] = (cur or {}).get('gross_margin') or None
    f['ocf_ps'] = (cur or {}).get('ocf_ps') or None
    # ---- v1.2.16 新增财务派生（全部 PIT：cur 已按公告日取值）----
    # ⚠️ 用**显式 None 判断**而非 `or None` —— `netprofit=0`（真·不赚钱）与"未披露"
    #   是两件事，`or None` 会把 0 吞成缺失（属于 P84/P85 同族的"用缺失冒充零"反向版）。
    _bps = (cur or {}).get('bps')
    _np, _rev = (cur or {}).get('netprofit'), (cur or {}).get('revenue')
    f['pb_proxy'] = (c / _bps) if (_bps is not None and _bps > 0) else None
    f['net_margin'] = (_np / _rev * 100) if (_rev not in (None, 0) and _np is not None) else None
    f['rev_log'] = math.log(_rev) if (_rev is not None and _rev > 0) else None
    return f, last_fin


_FIN_CACHE = {}


def _fin_at(fin, date):
    """PIT 取值（带缓存）。返回 date 当天可见的最新一期财报。"""
    if not fin:
        return None
    key = (id(fin), date)
    if key in _FIN_CACHE:
        return _FIN_CACHE[key]
    cands = [x for x in fin if (x['notice'] or '9999') <= date]
    r = max(cands, key=lambda x: x['report']) if cands else None
    if len(_FIN_CACHE) > 5000:
        _FIN_CACHE.clear()
    _FIN_CACHE[key] = r
    return r


# ------------------------------------------------------------------ 主流程
# 全市场榜取数失败时的兜底池（有偏：高流动性主板；**必须在输出里声明**）
_FALLBACK_POOL = [
    '000001', '600000', '600030', '000002', '600519', '000858', '600887', '601318',
    '600036', '000333', '600276', '002415', '600585', '601166', '600028', '601857',
    '601988', '601398', '600104', '000651', '002594', '600690', '601088', '600900',
    '601899', '600309', '002304', '600031', '000725', '601668', '600016', '601288',
    '600048', '000063', '600837', '601601', '600019', '601169', '000538', '002027',
]


def build_panel(universe=40, days=150, horizon=5, verbose=True, pool_codes=None, max_px=None):
    """构建因子面板：[(date, code, {因子名:值}, 未来收益)]

    ★ `pool_codes`：显式指定股票池。**因子结论的可比性完全依赖池子固定** ——
      实测同一因子在不同池子上符号相反（`rev_log` 混合池 −0.053 / 超大盘池 +0.045；
      `amt_log` 混合池 −0.057 / 超大盘池 +0.003）。池子一变，"结论"就变。
      传 None 时才走"全市场榜 + 固定种子抽样"。
    """
    if pool_codes:
        cand = [(str(c), '') for c in pool_codes]
        if verbose:
            print('  使用**显式股票池** %d 只（池子固定 → 不同批次结果可比）' % len(cand))
        return _build_from(cand, days, horizon, verbose, max_px)

    if verbose:
        print('  构建股票池（主板 / 成交额靠前 / 剔除 ST）…')
    rows = ds.clist(fid='f6', fs=ds.MARKET_MAIN, pages=4, fields='f12,f14,f2,f3,f6')
    if not rows:
        # ★ 降级兜底：push2 `clist` 限流时原实现会**静默产出 0 样本**
        #   —— 报告照样打印"因子 IC 排行"表头，只是全空，
        #   读起来像"这些因子都没用"，实际是"一只票都没取到"（P85 同族）。
        cm = ds.clist_meta()
        if verbose:
            print('  ⚠️ 全市场榜取数失败（complete=%s failed=%s）→ 退回内置高流动性名单（**有偏样本，须声明**）'
                  % (cm.get('complete'), cm.get('pages_failed')))
        rows = [{'f12': c, 'f14': '', 'f6': 1} for c in _FALLBACK_POOL]
    cand = []
    for x in rows:
        c, nm = str(x.get('f12', '')), str(x.get('f14', ''))
        if not ds.allowed(c) or 'ST' in nm.upper() or '退' in nm:
            continue
        if not ds._ok(x.get('f6')):
            continue
        cand.append((c, nm))
    random.seed(20260922)          # 固定种子 → 结果可复现（因子研究必须可复现）
    random.shuffle(cand)
    cand = cand[:universe]
    return _build_from(cand, days, horizon, verbose, max_px)


def _build_from(cand, days, horizon, verbose=True, max_px=None):
    """由给定股票池构建面板（池子固定 → 跨批次结果可比）。

    ★ `max_px`：**逐时点**价格上限 —— 用**当日收盘价**判断，因此**不含前视**。
      这才是"低价票"条件的正确施加方式：旧做法用**期末价格**筛池再回溯历史，
      会系统性偏向"已经跌下来的票"，把反转/超跌类因子 IC 显著夸大（见 P91）。
    """

    # ★ 相对强度族的前提：上证指数日线（**必须显式写 sh000001** ——
    #   裸码 `000001` 会被当成平安银行，这是本项目的老坑）。
    ik = ds.kline_qq('sh000001', 800)
    ix = {x['d']: x['c'] for x in ik} if ik else {}
    if verbose:
        print('  指数基准：sh000001 共 %d 根%s' % (len(ik), '' if ix else '  ⚠️ 取数失败 → rs/beta/alpha 将全部为 None'))

    panel = []
    n_px = 0
    for n, (code, nm) in enumerate(cand):
        try:
            k = ds.kline_qq(code, 800)
            fin = ds.financials(code)
            # ★ v1.2.16：新浪资金流序列 —— 新增的「资金结构」类因子（特大单占比 /
            #   连续净流入 / 资金加速度 / 换手率比）全靠它。取不到就传空 dict，
            #   那些因子会保持 None（IC 跳过），**不会退化成 0**。
            fl = {}
            for x in ds.moneyflow_sina(code, 600):
                if x[0]:
                    # ⚠️ x[5] 是「换手率×100」（见 ds.moneyflow_sina docstring），必须 /100
                    fl[x[0]] = {'net': x[1], 'r0': x[2], 'turnover': x[5] / 100.0}
            if not k or len(k) < 120:
                continue
            idx_from = max(60, len(k) - days - horizon - 1)
            last_fin = None
            for i in range(idx_from, len(k) - horizon):
                # ★ 逐时点价格条件（用**当日**收盘价 → 无前视）
                if max_px is not None and k[i]['c'] > max_px:
                    n_px += 1
                    continue
                f, last_fin = daily_factors(k, i, fin, last_fin, fl, ix)
                if not f:
                    continue
                fwd = (k[i + horizon]['c'] / k[i]['c'] - 1) * 100
                panel.append((k[i]['d'], code, f, fwd))
        except Exception:
            pass
        if verbose and (n + 1) % 10 == 0:
            print('    已载入 %d/%d（累计样本 %d）' % (n + 1, len(cand), len(panel)))
        time.sleep(0.2)
    if max_px is not None and verbose:
        print('  ★ 逐时点价格过滤（当日收盘 ≤ %.1f 元）：剔除 %d 个「票-日」样本（**无前视**）'
              % (max_px, n_px))
    return panel, cand


def compute_ic(panel, direction):
    """逐日横截面 IC。返回 ic 序列（升序）。"""
    by_day = {}
    for d, code, f, fwd in panel:
        by_day.setdefault(d, []).append((f, fwd))
    ics = []
    for d, lst in sorted(by_day.items()):
        if len(lst) < 8:
            continue
        vals, rets = [], []
        for f, fwd in lst:
            v = direction['fn'](f)
            if v is None or (isinstance(v, float) and (v != v)):
                continue
            vals.append(v)
            rets.append(fwd)
        if len(vals) < 8:
            continue
        ic = spearman(vals, rets)
        if ic is not None:
            ics.append(ic)
    return ics


def compute_ic_dated(panel, fn):
    """逐日横截面 IC → [(日期, ic), …]（升序）。

    ★ 为什么需要它：**平均 IC 可以被单一时段完全污染**。
      实测教训（v1.2.16）：`streak`（连板）在两个不同样本上符号相反
      （一轮 t=+3.04、另一轮 t=−0.17）—— 只看均值根本发现不了，
      只有**分时段**才能看出"这个因子在某段时间有效、换一段就反向"。
      同理 `model.py` 也是靠分时段才查出"AUC 0.53 全靠某一季度撑起来"。
    """
    by_day = {}
    for d, code, f, fwd in panel:
        by_day.setdefault(d, []).append((f, fwd))
    out = []
    for d, lst in sorted(by_day.items()):
        if len(lst) < 8:
            continue
        vals, rets = [], []
        for f, fwd in lst:
            v = fn(f)
            if v is None or (isinstance(v, float) and (v != v)):
                continue
            vals.append(v)
            rets.append(fwd)
        if len(vals) < 8:
            continue
        ic = spearman(vals, rets)
        if ic is not None:
            out.append((d, ic))
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]

    if '--list' in sys.argv:
        print('=' * 92)
        print('【因子库】共 %d 个' % len(FACTORS))
        print('=' * 92)
        print('  %-14s %-6s %s' % ('因子', '类别', '理论方向'))
        for nm, cat, fn, dr in FACTORS:
            print('  %-14s %-6s %s' % (nm, cat, '值越大越好' if dr > 0 else '值越小越好'))
        return

    universe, days, horizon = 40, 150, 5
    dump_path = None
    pool_path = None
    max_px = None
    for i, a in enumerate(sys.argv):
        if a == '--universe':
            universe = int(sys.argv[i + 1])
        if a == '--days':
            days = int(sys.argv[i + 1])
        if a == '--horizon':
            horizon = int(sys.argv[i + 1])
        if a == '--dump':
            dump_path = sys.argv[i + 1]
        if a == '--pool':
            pool_path = sys.argv[i + 1]
        if a == '--max-px':
            max_px = float(sys.argv[i + 1])

    if args and '--single' in sys.argv:
        code = args[0]
        k = ds.kline_qq(code, 400)
        fin = ds.financials(code)
        last_fin = None
        rows = []
        for i in range(60, len(k) - horizon):
            f, last_fin = daily_factors(k, i, fin, last_fin)
            if f:
                rows.append((k[i]['d'], f, (k[i + horizon]['c'] / k[i]['c'] - 1) * 100))
        print('【%s 单票因子时序】%d 个交易日，未来 %d 日收益' % (code, len(rows), horizon))
        print('  %-12s %10s %10s %10s %10s %10s %10s' % (
            '日期', 'RSI14', '%B', 'CCI20', '量比', 'ROE', '未来收益%'))
        for d, f, r in rows[-25:]:
            print('  %-12s %10.1f %10.1f %10.1f %10.2f %10s %+10.2f' % (
                d, f['rsi14'], f['pctb'], f['cci20'], f['vr'],
                ('%.2f' % f['roe']) if f['roe'] is not None else '—', r))
        return

    print('=' * 104)
    print('【量化因子挖掘】股票池 %d 只 × 最近 %d 个交易日，持有期 %d 日' % (universe, days, horizon))
    print('=' * 104)
    print('  财务因子走 PIT（只用当时已公告的财报）；种子固定为 20260922 以保证可复现')
    pool_codes = None
    if pool_path:
        pool_codes = [l.strip() for l in io.open(pool_path, encoding='utf-8') if l.strip()]
    panel, picks = build_panel(universe, days, horizon, pool_codes=pool_codes, max_px=max_px)
    print('  样本总数 %d（%d 只票）' % (len(panel), len(picks)))
    n_days = len(set(p[0] for p in panel))
    print('  横截面天数 %d' % n_days)
    if dump_path:
        # ★ 面板落盘：因子研究要反复试不同检验口径，每次都重跑 10 分钟不可接受。
        #   落盘后任何分析都能秒级复现（种子固定，结果确定）。
        json.dump({'picks': picks, 'panel': [[d, c, f, r] for d, c, f, r in panel]},
                  io.open(dump_path, 'w', encoding='utf-8'), ensure_ascii=False)
        print('  ★ 面板已落盘 → %s（可反复分析，无需重跑）' % dump_path)

    print()
    print('=' * 104)
    print('【因子 IC 排行】IC = 因子值与未来收益的逐日 Spearman 秩相关')
    print('=' * 104)
    print('  %-14s %-5s %8s %8s %7s %7s %6s  %s' % (
        '因子', '类别', 'IC均值', 'IC标准差', 'ICIR', 't值', '胜率', '判定'))

    results = []
    for nm, cat, fn, dr in FACTORS:
        ics = compute_ic(panel, {'fn': fn, 'dir': dr})
        if len(ics) < 10:
            continue
        m, s = mean(ics), std(ics)
        icir = m / s if s else 0.0
        t = icir * math.sqrt(len(ics))
        win = sum(1 for x in ics if x * dr > 0) / len(ics) * 100
        # 方向修正：把 IC 乘理论方向，便于横向比较（正 = 与理论一致）
        signed = m * dr
        results.append({'nm': nm, 'cat': cat, 'm': m, 's': s, 'icir': icir * dr,
                        't': t * dr, 'win': win, 'signed': signed, 'n': len(ics)})

    results.sort(key=lambda r: -abs(r['icir']))
    # ★ 多重比较校正（v1.2.16）：同时检验 N 个因子时，即使全部是纯噪声，
    #   按 |t|>2 判定也会有约 N×5% 的"假阳性"。因子库从 33 增到 57 后，
    #   这个偏差不可忽略 → 用 Bonferroni 阈值 Φ⁻¹(1−0.05/(2N)) 做硬性校正。
    Nt = max(1, len(results))
    z_bonf = norm_ppf(1 - 0.05 / (2 * Nt))
    n_raw = n_bonf = 0
    for r in results:
        ta, sg = abs(r['t']), abs(r['signed'])
        if ta >= z_bonf and sg >= 0.03:
            verdict = '✅ 有效'
            n_bonf += 1
            n_raw += 1
        elif ta >= 3 and sg >= 0.03:
            verdict = '△ 未过校正'
            n_raw += 1
        elif ta >= 2:
            verdict = '○ 边际'
        else:
            verdict = '× 噪声'
        dirn = '同向' if r['signed'] > 0 else '反向'
        print('  %-14s %-5s %+8.4f %8.4f %+7.3f %+7.2f %5.0f%%  %-4s %s' % (
            r['nm'], r['cat'], r['signed'], r['s'], r['icir'], r['t'], r['win'], dirn, verdict))

    print()
    print('  判定（v1.2.16 起加入多重比较校正）：')
    print('    ✅ 有效      = |t| ≥ %.2f（Bonferroni，α=0.05/%d 项检验）且 |方向修正 IC| ≥ 0.03'
          % (z_bonf, Nt))
    print('    △ 未过校正  = |t| ≥ 3 但未达校正阈值 —— **可能是多重比较造成的假阳性**，仅供探索')
    print('    ○ 边际      = |t| ≥ 2        × 噪声 = 其余')
    print('    同向/反向   = 实测符号是否与「理论方向」一致（反向 = 传统技术分析在该样本上被推翻）')
    print('  ⚠️ 本轮共检验 %d 个因子 → 即使**全是噪声**，期望假阳性也有 %.1f 个（|t|>2 口径）；'
          % (Nt, Nt * 0.05))
    print('     预期"未过校正但 |t|≥3"的假阳性约 %.1f 个。**故只有 ✅ 一档可作为结论。**' % (Nt * 0.0027))
    print('  ⚠️ IC 是**横截面**相关性，衡量"同一天里谁比谁强"，不是"能不能赚钱"；')
    print('     且未含交易成本、涨跌停无法成交、滑点——这些都要在策略层单独扣。')

    # 分组单调性（对 ICIR 最高的 5 个因子）
    print()
    print('=' * 104)
    print('【分组单调性检验】按因子分 5 组，看各组未来收益是否单调（只挑最好组 = 挑樱桃）')
    print('=' * 104)
    for r in results[:5]:
        fn = next(x[2] for x in FACTORS if x[0] == r['nm'])
        dr = next(x[3] for x in FACTORS if x[0] == r['nm'])
        by_day = {}
        for d, code, f, fwd in panel:
            by_day.setdefault(d, []).append((f, fwd))
        buckets = [[] for _ in range(5)]
        for d, lst in by_day.items():
            vv = []
            for f, fwd in lst:
                v = fn(f)
                if v is None or (isinstance(v, float) and v != v):
                    continue
                vv.append((v * dr, fwd))
            if len(vv) < 10:
                continue
            vv.sort(key=lambda t: t[0])
            step = len(vv) / 5.0
            for bi in range(5):
                seg = vv[int(bi * step):int((bi + 1) * step)]
                if seg:
                    buckets[bi].append(mean([x[1] for x in seg]))
        line = ' '.join('G%d:%+.2f%%' % (i + 1, mean(b)) for i, b in enumerate(buckets))
        mono = all(mean(buckets[i]) <= mean(buckets[i + 1]) for i in range(4)) or \
               all(mean(buckets[i]) >= mean(buckets[i + 1]) for i in range(4))
        print('  %-14s %s   %s' % (r['nm'], line, '✅单调' if mono else '⚠️非单调'))

    # ============ 分时段稳定性（v1.2.16 新增）============
    # 动机：`streak` 在两个样本上符号相反（+3.04 vs −0.17），只看均值发现不了。
    print()
    print('=' * 104)
    print('【分时段稳定性检验】按时间切 4 段，各段独立算 IC 均值（方向修正后）')
    print('=' * 104)
    print('  %-14s %9s %9s %9s %9s   %s' % ('因子', '第1段', '第2段', '第3段', '第4段', '判定'))
    n_drift = 0
    for r in results[:10]:
        _fn = next(x[2] for x in FACTORS if x[0] == r['nm'])
        _dr = next(x[3] for x in FACTORS if x[0] == r['nm'])
        dated = compute_ic_dated(panel, _fn)
        if len(dated) < 40:
            continue
        segs = [[] for _ in range(4)]
        per = len(dated) / 4.0
        for kx, (d, ic) in enumerate(dated):
            segs[min(3, int(kx / per))].append(ic * _dr)
        ms = [mean(s) for s in segs]
        npos = sum(1 for x in ms if x > 0)
        if npos in (4, 0):
            tag = '✅四段同号'
        elif npos in (3, 1):
            tag = '△三段同号'
        else:
            tag = '🔴符号漂移'
            n_drift += 1
        print('  %-14s %+9.4f %+9.4f %+9.4f %+9.4f   %s' % (r['nm'], ms[0], ms[1], ms[2], ms[3], tag))
    print()
    print('  ⚠️ 判读：**平均 IC 为正 ≠ 因子有效** —— 四段符号漂移说明它只在特定市场环境下成立，')
    print('     不可当稳定 alpha 用。本轮 |ICIR| 前 10 里有 **%d 个**出现符号漂移。' % n_drift)
    print('\n  ⚠️ 样本天数 %d，ICIR 的置信度随天数上升；本结论仅供筛选候选因子，' % n_days)
    print('     要用于实盘前需扩大股票池与时间跨度，并做样本外验证（见 references/calibration.md）。')


if __name__ == '__main__':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    main()
