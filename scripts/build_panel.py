"""build_panel.py — 并发构建因子面板（替代 factors.py 的顺序拉取）。

背景：`ds.kline_qq` 单只曾有 5.5 秒网络往返，而 `daily_factors` 只要 1.7ms。
      顺序跑 200 只 = 18 分钟，不可接受。
      → 用线程池并发拉 K 线，把拉取时间压到 1/N。
      ⚠️ 并发数不能太大：免费源会限流，8 是实测较稳的值。

★★ v1.2.37（P0-2）：**补拉资金流与指数基准**，让「资金类因子」不再全为 None
    此前第 5/6 个参数固定传 `{}`，导致 `hs`（换手率）/`turn_ratio`/`r0_ratio`/
    `ff_streak`/`ff_accel`/`ff_up_ratio`/`ff_cv` 与 `rs5/rs20/rs60/beta20/alpha20`
    **在整张面板上全是 None** → 大池只能验证纯技术类因子（v1.2.31 已记录该缺陷）。
    现在：
      · `fl`：逐票拉新浪 `MoneyFlow`（返回 {日期: {'net','r0','turnover'}}），
        字段口径与 `factors.py` 主流程**完全一致**（turnover 必须 /100，见 ds docstring）
      · `ix`：`sh000001` 只拉一次，供 rs/beta/alpha 使用（**必须写显式前缀**，
        裸码 000001 会被当成平安银行）

用法：
    python build_panel.py _pool_200.txt _panel_200.json 300 8
    python build_panel.py _pool.txt _panel.json 300 8 --fin      # 同时拉 PIT 财务

输出末尾会打印**因子覆盖率自证**（资金类/相对强度类各有多少条有值），
用于证伪「补拉了但没生效」。
"""
import io, json, os, sys, time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ds
import factors as F

ARGS = [a for a in sys.argv[1:] if not a.startswith('--')]
FLAGS = {a for a in sys.argv[1:] if a.startswith('--')}

pool_file = ARGS[0] if len(ARGS) > 0 else '_pool_200.txt'
out_file = ARGS[1] if len(ARGS) > 1 else '_panel_200.json'
days = int(ARGS[2]) if len(ARGS) > 2 else 300
workers = int(ARGS[3]) if len(ARGS) > 3 else 8
WITH_FIN = '--fin' in FLAGS
WITH_FLOW = '--no-flow' not in FLAGS          # 默认补拉资金流
FWD = 5

codes = []
for ln in io.open(pool_file, encoding='utf-8'):
    p = ln.split()
    if p:
        codes.append(p[0])
print('池子 %d 只，天数 %d，并发 %d，资金流=%s，财务=%s'
      % (len(codes), days, workers, 'ON' if WITH_FLOW else 'OFF', 'ON' if WITH_FIN else 'OFF'))

# 拉 K 线：需要 days + 250（算 pos250）+ FWD 的余量
need = days + 260

# ---- 指数基准（只拉一次；必须写显式前缀 sh000001）----
t_start = time.time()
ix = {}
try:
    ik = ds.kline_qq('sh000001', need)
    ix = {x['d']: x['c'] for x in ik} if ik else {}
except Exception:
    ix = {}
print('  指数基准 sh000001：%d 根%s' % (len(ix), '' if ix else '  ⚠️ 取数失败 → rs/beta/alpha 将全为 None'))


def one(c):
    """拉单只：K 线 +（可选）新浪资金流 +（可选）财务。异常一律返回 None 不抛。"""
    k = None
    try:
        k = ds.kline_qq(c, need)
    except Exception:
        k = None
    mf = None
    if WITH_FLOW:
        try:
            mf = ds.moneyflow_sina(c, need)
        except Exception:
            mf = None
    fin = None
    if WITH_FIN:
        try:
            fin = ds.financials(c)
        except Exception:
            fin = None
    return c, k, mf, fin


t0 = time.time()
ks, fls, fins = {}, {}, {}
flow_ok = 0
fail_k = 0
with ThreadPoolExecutor(max_workers=workers) as ex:
    for i, (c, k, mf, fin) in enumerate(ex.map(one, codes), 1):
        if k and len(k) > 270:
            ks[c] = k
        else:
            fail_k += 1
        if mf:
            # ⚠️ x[5] 是「换手率×100」（见 ds.moneyflow_sina docstring），必须 /100
            fls[c] = {x[0]: {'net': x[1], 'r0': x[2], 'turnover': x[5] / 100.0}
                      for x in mf if x[0]}
            flow_ok += 1
        if fin:
            fins[c] = fin
        if i % 40 == 0:
            print('  拉取 %d/%d（K线 %d / 资金流 %d）%.0fs'
                  % (i, len(codes), len(ks), flow_ok, time.time() - t0))
print('拉取完成：K线 %d / 资金流 %d / 失败 %d，耗时 %.0fs'
      % (len(ks), flow_ok, fail_k, time.time() - t0))

# ---- 算因子 ----
t1 = time.time()
panel = []
for c, k in ks.items():
    n = len(k)
    hi = n - FWD - 1                      # 最后一个有完整前向收益的索引
    lo = max(260, hi - days + 1)
    fl = fls.get(c) or {}
    fin = fins.get(c)
    for i in range(lo, hi + 1):
        try:
            f, _ = F.daily_factors(k, i, fin, None, fl, ix)
        except Exception:
            continue
        if not f:
            continue
        fwd = (k[i + FWD]['c'] / k[i]['c'] - 1) * 100 if k[i]['c'] else None
        if fwd is None:
            continue
        panel.append([k[i]['d'], c, f, fwd])
calc_sec = time.time() - t1
print('面板 %d 条，算因子耗时 %.0fs' % (len(panel), calc_sec))

# ---- 因子覆盖率自证（★ 证伪「补拉了但没生效」）----
GROUPS = {
    '资金类': ['hs', 'turn_ratio', 'r0_ratio', 'ff_streak', 'ff_accel', 'ff_up_ratio', 'ff_cv'],
    '相对强度': ['rs5', 'rs20', 'rs60', 'beta20', 'alpha20'],
}
print('  ── 因子覆盖率自证（有值条数 / 总条数）──')
if panel:
    tot = len(panel)
    for gname, keys in GROUPS.items():
        parts = []
        for kk in keys:
            if kk not in panel[0][2]:
                parts.append('%s(不存在)' % kk)
                continue
            n_ok = sum(1 for r in panel if isinstance(r[2].get(kk), (int, float)))
            parts.append('%s %d%%' % (kk, round(n_ok / tot * 100)))
        print('    %-6s %s' % (gname, ' | '.join(parts)))

json.dump({'picks': [[c, ''] for c in ks.keys()], 'panel': panel,
           'meta': {'days': days, 'fwd': FWD, 'with_flow': WITH_FLOW,
                    'with_fin': WITH_FIN, 'ix_n': len(ix),
                    'flow_ok': flow_ok, 'kline_ok': len(ks)}},
          io.open(out_file, 'w', encoding='utf-8'), ensure_ascii=False)
print('已写入 %s（%.1f MB），总耗时 %.0fs'
      % (out_file, os.path.getsize(out_file) / 1e6, time.time() - t_start))
