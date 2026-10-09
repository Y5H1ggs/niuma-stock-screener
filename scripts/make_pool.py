"""生成一个**固定、可复现**的因子研究股票池。

=== 为什么必须固定池子 ===
实测发现同一因子在不同池子上符号可以相反 ——
  `amt_log`（成交额）在超大盘池 IC +0.003、在中小价池 **−0.117**；
  `rev_log`（营收）在混合池 −0.053、在超大盘池 +0.045。
池子的市值结构一变，"结论"就跟着变 → **不定池子的因子研究没有可比性**。

=== ★ 本版本修掉的一个严重缺陷（v1.2.17）===
**旧版按「最新交易日收盘价 ≤ 15」筛池，却把该池回溯 500 天** —— 这是**前视偏差**：
  一只票从 30 元跌到 12 元 → 现在会被选入（现在 ≤15）；
  一只票从 5 元涨到 30 元 → 现在不会被选入。
  → 池子系统性偏向"已经跌下来的票"，把**反转/超跌类因子**的 IC 显著夸大。
消除办法：**池子只按「静态属性」筛**（板块 / 是否 ST / 名称），
  **任何时变条件（价格、市值、流动性）都在回测时逐时点施加**（`factors.py` / `model.py` 的 `--max-px`）。

=== 仍然无法消除的偏差（数据源限制，必须声明）===
**幸存者偏差**：腾讯/新浪只提供"至今仍在交易"的票 → 历史上退市的票不在样本里。
本池**无法修复**这一点，任何基于它的结论都应视为「偏乐观」。

用法：
  python make_pool.py 120 _pool_main.txt            # 只按静态属性筛（推荐）
  python make_pool.py 120 _pool_all.txt --no-st     # 同上（ST 已在代码里排除）
"""
import io, os, random, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ds

N = int(sys.argv[1]) if len(sys.argv) > 1 else 120
OUT = sys.argv[2] if len(sys.argv) > 2 else '_pool_main.txt'
SEED = 20261009

print('时间', time.strftime('%H:%M:%S'))
rows = []
for pg in range(1, 13):
    r = ds.dce_get('RPT_DMSK_TS_STOCKNEW', pz=500, page=pg)
    if not r:
        break
    rows += r
    if len(r) < 500:
        break
    time.sleep(0.4)
print('  报表取到 %d 行（**仅用其代码/名称，不使用价格字段** —— 避免前视）' % len(rows))

seen, cand = set(), []
for x in rows:
    c = str(x.get('SECURITY_CODE') or '')
    nm = str(x.get('SECURITY_NAME_ABBR') or '')
    # ★ 只按**静态属性**筛：板块前缀 + 是否 ST（名称含 ST/退）。
    #   **刻意不看 CLOSE_PRICE / TURNOVERRATE** —— 那两个是时变量，用它筛池就是前视。
    if not ds.allowed(c) or 'ST' in nm.upper() or '退' in nm:
        continue
    if c in seen:
        continue
    seen.add(c)
    cand.append((c, nm))

# 固定种子随机抽样（不按代码排序 —— 会偏向 000xxx/6000xx 段；也不按成交额 —— 会随时间漂移）
random.seed(SEED)
pick = random.sample(cand, min(N, len(cand)))
pick.sort(key=lambda z: z[0])
print('  池：主板 + 非 ST（**无价格条件**）= %d 只 → 固定种子(%d)抽样 %d 只' % (len(cand), SEED, len(pick)))

io.open(OUT, 'w', encoding='utf-8').write('\n'.join(c for c, _ in pick))
print('  已写入 %s' % OUT)
print('  沪深分布：沪 %d / 深 %d' % (sum(1 for c, _ in pick if c[0] == '6'),
                                 sum(1 for c, _ in pick if c[0] == '0')))
print('  样例：', '、'.join('%s %s' % (c, n) for c, n in pick[:8]))
print()
print('  ⚠️ 价格/流动性等**时变条件**请改用逐时点参数：')
print('     python factors.py --pool %s --max-px 15 --days 500' % OUT)
print('     python model.py   --pool %s --max-px 15' % OUT)
print('  ⚠️ 幸存者偏差（退市票不在样本）**数据源层面无法修复**，结论应视为偏乐观。')
