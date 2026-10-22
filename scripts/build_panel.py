"""_build_panel.py — 并发构建因子面板（替代 factors.py 的顺序拉取）。

背景：`ds.kline_qq` 单只约 5.5 秒（含网络往返），而 `daily_factors` 只要 1.7ms。
      顺序跑 200 只 = 18 分钟，不可接受。
      → 用 8 线程并发拉 K 线，把拉取时间压到 1/8。
      ⚠️ 并发数不能太大：免费源会限流，8 是实测较稳的值。

用法：
    python _build_panel.py _pool_200.txt _panel_200.json 300 8
    （池子文件 / 输出 / 天数 / 并发数）
"""
import io, json, os, sys, time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ds
import factors as F

pool_file = sys.argv[1] if len(sys.argv) > 1 else '_pool_200.txt'
out_file = sys.argv[2] if len(sys.argv) > 2 else '_panel_200.json'
days = int(sys.argv[3]) if len(sys.argv) > 3 else 300
workers = int(sys.argv[4]) if len(sys.argv) > 4 else 8
FWD = 5

codes = []
for ln in io.open(pool_file, encoding='utf-8'):
    p = ln.split()
    if p:
        codes.append(p[0])
print('池子 %d 只，天数 %d，并发 %d' % (len(codes), days, workers))

# 拉 K 线：需要 days + 250（算 pos250）+ FWD 的余量
need = days + 260


def one(c):
    try:
        k = ds.kline_qq(c, need)
        return c, (k if k and len(k) > 270 else None)
    except Exception:
        return c, None


t0 = time.time()
ks = {}
with ThreadPoolExecutor(max_workers=workers) as ex:
    for i, (c, k) in enumerate(ex.map(one, codes), 1):
        if k:
            ks[c] = k
        if i % 40 == 0:
            print('  拉取 %d/%d（成功 %d）%.0fs' % (i, len(codes), len(ks), time.time() - t0))
print('拉取完成：成功 %d / %d，耗时 %.0fs' % (len(ks), len(codes), time.time() - t0))

# 算因子
t1 = time.time()
panel = []
for c, k in ks.items():
    n = len(k)
    hi = n - FWD - 1                      # 最后一个有完整前向收益的索引
    lo = max(260, hi - days + 1)
    for i in range(lo, hi + 1):
        try:
            f, _ = F.daily_factors(k, i, None, None, {}, {})
        except Exception:
            continue
        if not f:
            continue
        fwd = (k[i + FWD]['c'] / k[i]['c'] - 1) * 100 if k[i]['c'] else None
        if fwd is None:
            continue
        panel.append([k[i]['d'], c, f, fwd])
print('面板 %d 条，算因子耗时 %.0fs' % (len(panel), time.time() - t1))

json.dump({'picks': [[c, ''] for c in ks.keys()], 'panel': panel},
          io.open(out_file, 'w', encoding='utf-8'), ensure_ascii=False)
print('已写入 %s（%.1f MB）' % (out_file, os.path.getsize(out_file) / 1e6))
