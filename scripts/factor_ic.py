"""factor_ic.py — 用「每日横截面 Rank IC」检验因子有效性（★ 标准口径）。

用法：
    python factor_ic.py _panel_big_flow.json          # 面板文件（build_panel.py 产出）


★ 与之前方法的差别（方法论升级，务必看懂）：
  旧法：把所有 (因子值, 未来收益) 池在一起算 Pearson → t = IC×√n
        **严重高估显著性**：同一天里 498 只票的收益高度相关（共同的 beta），
        n 被当成独立样本，t 值虚高。
  新法：**逐日横截面算 Rank IC（Spearman）→ 得到 IC 时间序列 → t = mean/(sd/√天数)**
        这才是因子检验的标准口径（Grinold 的 IC 定义），样本量＝**交易日数**而非行数。

四道闸：
  ① Bonferroni：102 项检验 → 双边 α=0.05 → |t| ≥ 3.40
  ② 时间稳定：按**日期**切两半（不是按行！），两段 IC 同号
  ③ 单调：五分位收益单调
  ④ 强度：|IC| 全样本 ≥ 0.010（低于此＝噪声级）
"""
import io, json, math, statistics as st, sys

PATH = sys.argv[1] if len(sys.argv) > 1 else '_panel_big_flow.json'
D = json.load(io.open(PATH, encoding='utf-8'))
panel = D['panel']
meta = D.get('meta') or {}
print('面板 %s：%d 条，%s' % (PATH, len(panel), meta))

# 按日期分组
byday = {}
for d, c, f, y in panel:
    byday.setdefault(d, []).append((f, y))
dates = sorted(byday)
print('交易日 %d 个（%s ~ %s），日均 %.0f 只'
      % (len(dates), dates[0], dates[-1], len(panel) / len(dates)))

KEYS = sorted(panel[0][2].keys())
print('因子 %d 个\n' % len(KEYS))

cut = dates[len(dates) // 2]
print('时间切分点：%s（前 %d 日 / 后 %d 日）\n' % (cut, dates.index(cut), len(dates) - dates.index(cut)))


def ranks(xs):
    """平均秩（处理并列）。"""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for t in range(i, j + 1):
            r[order[t]] = avg
        i = j + 1
    return r


def pearson(a, b):
    n = len(a)
    if n < 3:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    sa = math.sqrt(sum((v - ma) ** 2 for v in a))
    sb = math.sqrt(sum((v - mb) ** 2 for v in b))
    if sa <= 1e-12 or sb <= 1e-12:
        return None
    return sum((a[i] - ma) * (b[i] - mb) for i in range(n)) / (sa * sb)


# 每日：先算收益秩（共享）
day_y_rank = {}
for d in dates:
    ys = [r[1] for r in byday[d]]
    day_y_rank[d] = ranks(ys)

res = []
for k in KEYS:
    ic_series, ic_in, ic_out = [], [], []
    n_used = 0
    for d in dates:
        rows = byday[d]
        xs, ys = [], []
        for f, y in rows:
            v = f.get(k)
            if isinstance(v, (int, float)) and not (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
                xs.append(v)
                ys.append(y)
        if len(xs) < 30:
            continue
        n_used += len(xs)
        xr = ranks(xs)
        yr = day_y_rank[d][:len(xs)] if len(xs) == len(rows) else ranks(ys)
        ic = pearson(xr, yr)
        if ic is None:
            continue
        ic_series.append(ic)
        (ic_in if d < cut else ic_out).append(ic)
    if len(ic_series) < 30:
        continue
    m = st.mean(ic_series)
    sd = st.pstdev(ic_series) or 1e-9
    t = m / (sd / math.sqrt(len(ic_series)))
    mi = st.mean(ic_in) if ic_in else None
    mo = st.mean(ic_out) if ic_out else None
    # 五分位（全样本池化，按因子值排序）
    pairs = []
    for d in dates:
        for f, y in byday[d]:
            v = f.get(k)
            if isinstance(v, (int, float)):
                pairs.append((v, y))
    pairs.sort(key=lambda p: p[0])
    q = len(pairs) // 5
    qm, qw = [], []
    for i in range(5):
        seg = [p[1] for p in pairs[i * q:(i + 1) * q]]
        qm.append(st.mean(seg))
        qw.append(sum(1 for v in seg if v > 0) / len(seg) * 100)
    mono = all(qm[i] <= qm[i + 1] for i in range(4)) or all(qm[i] >= qm[i + 1] for i in range(4))
    res.append({'k': k, 'ic': m, 't': t, 'sd': sd, 'n_days': len(ic_series),
                'n_obs': n_used, 'ic_in': mi, 'ic_out': mo,
                'wr_q1': qw[0], 'wr_q5': qw[4], 'q5q1': qm[4] - qm[0], 'mono': mono})

res.sort(key=lambda r: -abs(r['t']))
BONF = 3.40
print('=== 按 |t| 排序 Top 22（Bonferroni 门槛 |t| ≥ %.2f，%d 项检验）===' % (BONF, len(KEYS)))
print('  %-14s %8s %7s %8s %8s %7s %7s %6s' %
      ('因子', 'IC均值', 't', 'IC前半', 'IC后半', 'Q1胜率', 'Q5胜率', '单调'))
for r in res[:22]:
    print('  %-14s %+8.4f %+7.2f %+8.4f %+8.4f %6.1f%% %6.1f%% %6s'
          % (r['k'], r['ic'], r['t'], r['ic_in'] or 0, r['ic_out'] or 0,
             r['wr_q1'], r['wr_q5'], '是' if r['mono'] else '否'))

print()
print('=== 四道闸全过 ===')
ok = [r for r in res if abs(r['t']) >= BONF and r['mono']
      and r['ic_in'] is not None and r['ic_out'] is not None
      and r['ic_in'] * r['ic_out'] > 0 and abs(r['ic']) >= 0.010]
if not ok:
    print('  （无）')
else:
    print('  %-14s %8s %7s %8s %8s %7s %7s %9s' %
          ('因子', 'IC均值', 't', 'IC前半', 'IC后半', 'Q1胜率', 'Q5胜率', 'Q5-Q1'))
    for r in ok:
        print('  %-14s %+8.4f %+7.2f %+8.4f %+8.4f %6.1f%% %6.1f%% %+8.2fpp'
              % (r['k'], r['ic'], r['t'], r['ic_in'], r['ic_out'],
                 r['wr_q1'], r['wr_q5'], r['q5q1']))
print('  合计 %d / %d 个' % (len(ok), len(res)))

print()
print('=== 过校正但未过全部（|t|≥%.2f 且两段同号）===' % BONF)
half = [r for r in res if abs(r['t']) >= BONF and r['ic_in'] * r['ic_out'] > 0
        and r not in ok]
for r in half[:12]:
    why = []
    if not r['mono']:
        why.append('非单调')
    if abs(r['ic']) < 0.010:
        why.append('|IC|<0.01')
    print('  %-14s t%+.2f IC%+.4f Q5-Q1 %+.2fpp  卡在：%s'
          % (r['k'], r['t'], r['ic'], r['q5q1'], '、'.join(why) or '—'))

print()
print('=== ★ 本次重点：先前全为 None 的资金类 / 相对强度类 ===')
FOCUS = ['hs', 'turn_ratio', 'r0_ratio', 'ff_streak', 'ff_accel', 'ff_up_ratio', 'ff_cv',
         'rs5', 'rs20', 'rs60', 'beta20', 'alpha20']
d = {r['k']: r for r in res}
print('  %-14s %8s %7s %8s %8s %7s %7s %6s %s' %
      ('因子', 'IC均值', 't', 'IC前半', 'IC后半', 'Q1胜率', 'Q5胜率', '单调', '判定'))
for k in FOCUS:
    r = d.get(k)
    if not r:
        print('  %-14s 样本不足' % k)
        continue
    v = '✅ 过校正' if abs(r['t']) >= BONF else ('△ 边缘(|t|≥2)' if abs(r['t']) >= 2 else '— 不显著')
    if abs(r['t']) >= BONF and not r['mono']:
        v += '但非单调'
    print('  %-14s %+8.4f %+7.2f %+8.4f %+8.4f %6.1f%% %6.1f%% %6s %s'
          % (k, r['ic'], r['t'], r['ic_in'] or 0, r['ic_out'] or 0,
             r['wr_q1'], r['wr_q5'], '是' if r['mono'] else '否', v))

json.dump(res, io.open('_fac_flow_rank.json', 'w', encoding='utf-8'), ensure_ascii=False)
print('\n已存 _fac_flow_rank.json（%d 个因子）' % len(res))
