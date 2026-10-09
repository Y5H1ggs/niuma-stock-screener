"""读 factors.py 落盘的 panel，做深度复查：
  ① 类别层面的信息量分布  ② 高冗余因子对（挂着不同名字的同一个因子）
  ③ Bonferroni 校正后的有效因子清单
用法：python _analyze_panel.py _panel_1009.json
"""
import io, itertools, json, math, os, random, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factors as F


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


path = sys.argv[1] if len(sys.argv) > 1 else '_panel_1009.json'
raw = json.load(io.open(path, encoding='utf-8'))
panel = raw['panel']
picks = raw.get('picks') or []
print('=' * 100)
print('【面板深度复查】%s' % path)
print('=' * 100)
print('  样本 %d 条 | 票 %d 只 | 横截面 %d 天 | 日期 %s ~ %s'
      % (len(panel), len(picks), len(set(p[0] for p in panel)), panel[0][0], panel[-1][0]))
print()

# ---------- 取每个因子的 (行号 -> 值) ----------
random.seed(7)
idxvals, fnmap, drmap, catmap = {}, {}, {}, {}
for nm, cat, fn, dr in F.FACTORS:
    d = {}
    for n, row in enumerate(panel):
        v = fn(row[2])
        if v is None or (isinstance(v, float) and v != v):
            continue
        d[n] = v
    if len(d) < 100:
        continue
    idxvals[nm] = d
    fnmap[nm] = fn
    drmap[nm] = dr
    catmap[nm] = cat
print('  可用于检验的因子 %d 个（值覆盖 ≥100 条）' % len(idxvals))
print()

# ---------- ① 类别层面 ----------
print('=' * 100)
print('【① 类别层面的信息量】按类别统计成员因子的 |IC| 与最大 |t|')
print('=' * 100)
bys = {}
tmp = {}
for d_, row in [(r[0], r) for r in panel]:
    tmp.setdefault(d_, []).append(row)
for d_ in tmp:
    tmp[d_] = [x for x in tmp[d_] if len(tmp[d_]) >= 8]
cat_ic = {}
for nm in idxvals:
    fn = fnmap[nm]
    fwd_by = {}
    ics = []
    for d_, lst in tmp.items():
        vv, rr = [], []
        for row in lst:
            v = fn(row[2])
            if v is None or (isinstance(v, float) and v != v):
                continue
            vv.append(v)
            rr.append(row[3])
        if len(vv) >= 8:
            ic = spearman(vv, rr)
            if ic is not None:
                ics.append(ic)
    if len(ics) >= 10:
        m, s = mean(ics), std(ics)
        t = (m * drmap[nm] / s * math.sqrt(len(ics))) if s else 0.0
        cat_ic[nm] = (m * drmap[nm], t, len(ics))
for cat in sorted(set(catmap.values())):
    mem = [(nm, cat_ic[nm]) for nm in cat_ic if catmap[nm] == cat]
    if not mem:
        continue
    bi = max(mem, key=lambda x: abs(x[1][0]))
    print('  %-8s %2d 个 | 最强 %-14s 方向修正IC %+.4f  t=%+.2f'
          % (cat, len(mem), bi[0], bi[1][0], bi[1][1]))
print()

# ---------- ② 高冗余对 ----------
print('=' * 100)
print('【② 高冗余因子对】|Spearman| ≥ 0.80 —— 这些是「挂着不同名字的同一个因子」')
print('=' * 100)
names = sorted(idxvals)
pairs = []
for a, b in itertools.combinations(names, 2):
    da, db = idxvals[a], idxvals[b]
    common = list(da.keys() & db.keys())
    if len(common) < 300:
        continue
    smp = random.sample(common, min(1200, len(common)))
    c = spearman([da[i] for i in smp], [db[i] for i in smp])
    if c is not None and abs(c) >= 0.80:
        pairs.append((abs(c), c, a, b))
pairs.sort(reverse=True)
if pairs:
    for ac, c, a, b in pairs[:25]:
        print('  %-14s ↔ %-14s  ρ = %+.3f' % (a, b, c))
    print('  共 %d 对高冗余（说明因子库存在重复信息，**不是 85 个独立维度**）' % len(pairs))
else:
    print('  无高冗余对')
print()

# ---------- ③ Bonferroni ----------
print('=' * 100)
print('【③ Bonferroni 校正后的有效因子】')
print('=' * 100)
Nt = len(cat_ic)
z = F.norm_ppf(1 - 0.05 / (2 * Nt))
print('  检验 %d 个因子 → 校正阈值 |t| ≥ %.3f（α=0.05，双侧）' % (Nt, z))
rows = sorted(cat_ic.items(), key=lambda kv: -abs(kv[1][1]))
ok = [(nm, v) for nm, v in rows if abs(v[1]) >= z and abs(v[0]) >= 0.03]
if ok:
    for nm, (ic, t, n) in ok:
        print('  ✅ %-14s %-8s IC %+.4f  t=%+.2f  n=%d' % (nm, catmap[nm], ic, t, n))
else:
    print('  ⚠️ **无因子通过 Bonferroni 校正** —— 即：在 85 个因子里，')
    print('     没有任何一个的信息量大到能排除"多重比较碰巧显著"的可能。')
mid = [(nm, v) for nm, v in rows if abs(v[1]) >= 2 and not (abs(v[1]) >= z and abs(v[0]) >= 0.03)]
print()
print('  未过校正但 |t|≥2（**可能是假阳性，仅作探索**）：')
for nm, (ic, t, n) in mid[:12]:
    print('    ○ %-14s %-8s IC %+.4f  t=%+.2f' % (nm, catmap[nm], ic, t))
