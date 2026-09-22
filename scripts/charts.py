# -*- coding: utf-8 -*-
"""charts.py — 纯 SVG 图表（零依赖 / 无 JS / 离线可用 / 可导出 PDF）

设计定位：**嵌入白卡里的深色仪表盘**。
  报告正文是白底墨蓝+金，图表区用深色渐变面板形成对比 —— 视觉上像把金融终端的一块
  屏幕镶进文档，既有科技感，又不破坏整体版式的克制。

为什么用**内联 SVG**而不是 Chart.js：
  ① 项目铁律"零第三方依赖"，CDN 图表库会引入运行时网络依赖；
  ② SVG 是文档矢量图，打印与 `--pdf` 导出都清晰，Canvas 做不到；
  ③ 无 JS → 报告可安全以文件形式转发/存档，不会因脚本被拦而不显示。

配色遵守 A 股习惯：**涨 = 红 / 跌 = 绿**（与欧美相反），换成高饱和霓虹色系以适配深色底。

━━ 两条可读性硬约束（v1.2.5 实测踩过，勿回退）━━
  ① **viewBox 宽度必须匹配报告容器实际内容宽**。报告 `.wrap` 是 `max-width:960px`，
     减去卡片与子卡的内边距后，图表实际可显示宽度约 **900px**。如果 viewBox 写成 860，
     SVG 会被放大反而糊；写成 1100 则整体缩小、字号跟着缩水到读不清。
     → 统一 `W = 900`。
  ② **字号下限 12px**（在 900 宽度下 1:1 渲染）。曾用 9~10px，缩放到移动端/PDF 后
     完全不可读；且小字号必须配足行距，否则标签互相压叠。

⚠️ 同页多个 SVG 的 `filter`/`gradient` 的 **id 必须唯一** —— 否则后面的图会引用到
   前面图的定义（表现为颜色/发光错乱）。每个函数用自己的 pfx 前缀。
"""

W = 900          # 画布标准宽度（匹配 .wrap 960 − 内边距）
FS = 12          # 最小字号（轴标签）
FS_S = 11.5      # 次小字号（日期等）
FS_T = 15        # 面板标题
FS_V = 13.5      # 数值强调

# ---------------------------------------------------------------- 仪表盘配色
BG1, BG2 = '#0b1a2b', '#17324f'
RAISE = '#ff4d5a'
FALL = '#00e39a'
FLAT = '#8ba4bd'
GRID = '#2b4c6f'
AXIS = '#4a7ba8'
TXT = '#8ba4bd'
TXT_HI = '#e2edf8'
GOLD = '#ffc53d'
CYAN = '#4cc9ff'
PURPLE = '#b388ff'
MA_COLORS = {5: GOLD, 10: CYAN, 20: PURPLE, 60: '#ff8a65'}


def _esc(s):
    return (str(s).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


def _updown(delta):
    return RAISE if delta > 0 else (FALL if delta < 0 else FLAT)


def _defs(pfx):
    return f'''<defs>
<linearGradient id="{pfx}bg" x1="0" y1="0" x2="0" y2="1">
  <stop offset="0" stop-color="{BG1}"/><stop offset="1" stop-color="{BG2}"/>
</linearGradient>
<linearGradient id="{pfx}up" x1="0" y1="0" x2="0" y2="1">
  <stop offset="0" stop-color="#ff5b6b"/><stop offset="1" stop-color="#c8303f"/>
</linearGradient>
<linearGradient id="{pfx}dn" x1="0" y1="0" x2="0" y2="1">
  <stop offset="0" stop-color="#00e39a"/><stop offset="1" stop-color="#00996a"/>
</linearGradient>
<filter id="{pfx}glow" x="-60%" y="-60%" width="220%" height="220%">
  <feGaussianBlur stdDeviation="2.4" result="b"/>
  <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
</filter>
</defs>'''


def _open(w, h, pfx, title='', radius=12):
    """深色圆角面板 + 标题（标题占 44px，下方留白避免与图形重叠）。"""
    t = ''
    if title:
        t = (f'<text x="20" y="27" font-size="{FS_T}" font-weight="600" fill="{TXT_HI}" '
             f'letter-spacing=".2">{_esc(title)}</text>'
             f'<line x1="20" y1="38" x2="{w-36}" y2="38" stroke="{GRID}" '
             f'stroke-width="1" opacity=".6"/>')
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" '
            f'xmlns="http://www.w3.org/2000/svg" '
            f'style="display:block;font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\','
            f'\'PingFang SC\',\'Microsoft YaHei\',sans-serif;'
            f'font-variant-numeric:tabular-nums">'
            f'{_defs(pfx)}'
            f'<rect x="0" y="0" width="{w}" height="{h}" rx="{radius}" fill="url(#{pfx}bg)"/>'
            f'<rect x=".5" y=".5" width="{w-1}" height="{h-1}" rx="{radius}" fill="none" '
            f'stroke="#2f5d8a" stroke-opacity=".5"/>'
            f'{t}')


# ------------------------------------------------------------------ K 线 + 成交量
def candlestick_svg(kl, n=55, ma=(5, 10, 20), w=W, h=398, title=''):
    """蜡烛图 + 均线 + 成交量。"""
    if not kl or len(kl) < 5:
        return ''
    pfx = 'ck'
    seg = kl[-n:]
    base = kl[-(n + 60):] if len(kl) > n + 20 else kl
    closes = [r['c'] for r in base]
    off = len(base) - len(seg)

    pad_l, pad_r = 78, 92
    pad_t = 62 if title else 30
    vol_h = 72
    price_h = h - pad_t - vol_h - 40
    plot_w = w - pad_l - pad_r

    hi = max(r['h'] for r in seg)
    lo = min(r['l'] for r in seg)
    for m in ma:
        for i in range(off, len(base)):
            if i >= m - 1:
                v = sum(closes[i - m + 1:i + 1]) / m
                hi, lo = max(hi, v), min(lo, v)
    span = (hi - lo) or 1
    hi += span * 0.07
    lo -= span * 0.07
    span = hi - lo
    vmax = max(r['v'] for r in seg) or 1

    def Y(p):
        return pad_t + (hi - p) / span * price_h

    step = plot_w / len(seg)
    bw = max(2.0, step * 0.6)

    o = [_open(w, h, pfx, title)]

    # 网格 + 价格刻度
    for i in range(5):
        p = lo + span * i / 4
        y = Y(p)
        o.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l+plot_w}" y2="{y:.1f}" '
                 f'stroke="{GRID}" stroke-width="1" stroke-dasharray="2,5" opacity=".65"/>')
        o.append(f'<text x="{pad_l-11}" y="{y+4.2:.1f}" font-size="{FS}" fill="{TXT}" '
                 f'text-anchor="end">{p:.2f}</text>')

    # 蜡烛
    for i, r in enumerate(seg):
        x = pad_l + i * step + step / 2
        up = r['c'] >= r['o']
        col = RAISE if up else FALL
        o.append(f'<line x1="{x:.1f}" y1="{Y(r["h"]):.1f}" x2="{x:.1f}" y2="{Y(r["l"]):.1f}" '
                 f'stroke="{col}" stroke-width="1.1" opacity=".9"/>')
        y1, y2 = Y(max(r['o'], r['c'])), Y(min(r['o'], r['c']))
        hh = max(1.4, y2 - y1)
        o.append(f'<rect x="{x-bw/2:.1f}" y="{y1:.1f}" width="{bw:.1f}" height="{hh:.1f}" '
                 f'fill="url(#{pfx}{"up" if up else "dn"})" rx=".8"/>')

    # 均线（发光）
    for m in ma:
        pts = []
        for i in range(off, len(base)):
            if i < m - 1:
                continue
            v = sum(closes[i - m + 1:i + 1]) / m
            x = pad_l + (i - off) * step + step / 2
            pts.append(f'{x:.1f},{Y(v):.1f}')
        if len(pts) > 1:
            o.append(f'<polyline points="{" ".join(pts)}" fill="none" '
                     f'stroke="{MA_COLORS.get(m, GOLD)}" stroke-width="1.7" '
                     f'stroke-linejoin="round" filter="url(#{pfx}glow)"/>')

    # 图例（放标题行右侧，绝不与图形重叠）
    lx = w - 20 - (len(ma) * 76)
    ly = 27 if title else 16
    for m in ma:
        o.append(f'<rect x="{lx}" y="{ly-6}" width="14" height="3" rx="1.5" '
                 f'fill="{MA_COLORS.get(m, GOLD)}"/>')
        o.append(f'<text x="{lx+20}" y="{ly-2}" font-size="{FS}" fill="{TXT}">MA{m}</text>')
        lx += 76

    # 成交量
    vy0 = pad_t + price_h + 24
    o.append(f'<line x1="{pad_l}" y1="{vy0+vol_h}" x2="{pad_l+plot_w}" y2="{vy0+vol_h}" '
             f'stroke="{GRID}" stroke-width="1" opacity=".6"/>')
    o.append(f'<text x="{pad_l-11}" y="{vy0+14}" font-size="{FS_S}" fill="{TXT}" '
             f'text-anchor="end">VOL</text>')
    for i, r in enumerate(seg):
        x = pad_l + i * step + step / 2
        up = r['c'] >= r['o']
        bh = max(1.2, r['v'] / vmax * vol_h)
        o.append(f'<rect x="{x-bw/2:.1f}" y="{vy0+vol_h-bh:.1f}" width="{bw:.1f}" '
                 f'height="{bh:.1f}" fill="{RAISE if up else FALL}" opacity=".5" rx=".8"/>')

    # 最新价标签
    last = seg[-1]
    ly2 = Y(last['c'])
    # ⚠️ 防顶：现价贴近区间上/下沿时，右侧价格标签会顶到标题行或成交量区 → 夹在安全范围内
    ly2 = min(max(ly2, pad_t + 14), pad_t + price_h - 14)
    col = _updown(last['c'] - last['o'])
    o.append(f'<line x1="{pad_l}" y1="{ly2:.1f}" x2="{pad_l+plot_w}" y2="{ly2:.1f}" '
             f'stroke="{col}" stroke-width="1" stroke-dasharray="5,4" opacity=".78"/>')
    o.append(f'<rect x="{pad_l+plot_w+6}" y="{ly2-11:.1f}" width="62" height="22" rx="4.5" '
             f'fill="{col}"/>')
    o.append(f'<text x="{pad_l+plot_w+37}" y="{ly2+4.4:.1f}" font-size="{FS_V}" fill="#08131f" '
             f'text-anchor="middle" font-weight="700">{last["c"]:.2f}</text>')

    # 日期刻度（3 个，字号够大）
    for kk in (0, len(seg) // 2, len(seg) - 1):
        x = pad_l + kk * step + step / 2
        o.append(f'<text x="{x:.1f}" y="{h-13}" font-size="{FS_S}" fill="{TXT}" '
                 f'text-anchor="middle">{_esc(seg[kk]["d"][5:])}</text>')

    o.append('</svg>')
    return ''.join(o)


# ------------------------------------------------------------------ 资金流柱状
def flow_bars_svg(rows, w=W, h=236, title=''):
    """资金流柱状图。 rows = [(日期, 主力净额万元), ...]（升序）"""
    if not rows:
        return ''
    pfx = 'fb'
    vals = [v for _d, v in rows]
    vmax = max(abs(v) for v in vals) or 1
    pad_l, pad_r = 88, 156      # pad_r 必须容得下右侧"合计 ±N 万元 / 近N日 / 净流出"统计区
    pad_t = 62 if title else 34
    pad_b = 34
    plot_w, plot_h = w - pad_l - pad_r, h - pad_t - pad_b
    zero = pad_t + plot_h / 2
    step = plot_w / len(rows)
    bw = max(3.0, step * 0.55)

    o = [_open(w, h, pfx, title)]
    o.append(f'<line x1="{pad_l}" y1="{zero:.1f}" x2="{pad_l+plot_w}" y2="{zero:.1f}" '
             f'stroke="{AXIS}" stroke-width="1.2" opacity=".9"/>')
    o.append(f'<line x1="{pad_l+plot_w+10}" y1="{pad_t}" x2="{pad_l+plot_w+10}" '
             f'y2="{pad_t+plot_h}" stroke="{GRID}" stroke-width="1" opacity=".5"/>')
    for frac, lab in ((1.0, vmax), (0.0, 0), (-1.0, -vmax)):
        y = zero - frac * (plot_h / 2)
        if frac:
            o.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l+plot_w}" y2="{y:.1f}" '
                     f'stroke="{GRID}" stroke-width="1" stroke-dasharray="2,5" opacity=".55"/>')
        o.append(f'<text x="{pad_l-11}" y="{y+4.2:.1f}" font-size="{FS}" fill="{TXT}" '
                 f'text-anchor="end">{lab:+.0f}</text>')

    for i, (d, v) in enumerate(rows):
        x = pad_l + i * step + step / 2
        bh = max(1.2, abs(v) / vmax * (plot_h / 2))
        y = zero - bh if v > 0 else zero
        o.append(f'<rect x="{x-bw/2:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{bh:.1f}" '
                 f'fill="url(#{pfx}{"up" if v > 0 else "dn"})" rx="1.6"/>')
        if i % max(1, len(rows) // 6) == 0 or i == len(rows) - 1:
            o.append(f'<text x="{x:.1f}" y="{h-13}" font-size="{FS_S}" fill="{TXT}" '
                     f'text-anchor="middle">{_esc(d[5:])}</text>')

    tot = sum(vals)
    col = _updown(tot)
    xr = pad_l + plot_w + 22
    o.append(f'<text x="{xr}" y="{pad_t+16}" font-size="{FS}" fill="{TXT}">合计</text>')
    o.append(f'<text x="{xr}" y="{pad_t+40}" font-size="17" font-weight="700" '
             f'fill="{col}">{tot:+.0f}</text>')
    o.append(f'<text x="{xr}" y="{pad_t+57}" font-size="{FS_S}" fill="{TXT}">万元</text>')
    o.append(f'<text x="{xr}" y="{pad_t+82}" font-size="{FS_S}" fill="{TXT}">近 {len(rows)} 日</text>')
    o.append(f'<text x="{xr}" y="{pad_t+99}" font-size="{FS_S}" fill="{TXT}">'
             f'{"净流出" if tot < 0 else "净流入"}</text>')
    o.append('</svg>')
    return ''.join(o)


# ------------------------------------------------------------------ 横向对比条
def hbar_svg(items, w=W, h=None, title='', suffix='%', signed=True):
    """横向对比条。 items = [(标签, 数值, 副标签), ...]"""
    if not items:
        return ''
    pfx = 'hb'
    row_h = 36
    pad_t = 62 if title else 30
    h = h or (pad_t + row_h * len(items) + 22)
    label_w, num_w, sub_w = 204, 108, 200      # 三列都留够，否则"数值"与"副标签"会挤在一起
    plot_w = w - label_w - num_w - sub_w - 28
    vmax = max(abs(x[1]) for x in items) or 1

    o = [_open(w, h, pfx, title)]
    x0 = label_w + (plot_w / 2 if signed else 0)
    if signed:
        o.append(f'<line x1="{x0:.1f}" y1="{pad_t-10}" x2="{x0:.1f}" y2="{h-16}" '
                 f'stroke="{AXIS}" stroke-width="1.1" opacity=".75"/>')
    for i, it in enumerate(items):
        lab, v, sub = (list(it) + [''])[:3]
        y = pad_t + i * row_h
        col = _updown(v) if signed else CYAN
        bl = max(2.5, abs(v) / vmax * (plot_w / 2 if signed else plot_w))
        bx = (x0 if v >= 0 else x0 - bl) if signed else label_w
        o.append(f'<text x="{label_w-14}" y="{y+16:.1f}" font-size="{FS_V}" fill="{TXT_HI}" '
                 f'text-anchor="end">{_esc(lab)}</text>')
        o.append(f'<rect x="{bx:.1f}" y="{y+3}" width="{bl:.1f}" height="18" rx="4" '
                 f'fill="{col}" opacity=".92"/>')
        o.append(f'<rect x="{bx:.1f}" y="{y+3}" width="{bl:.1f}" height="18" rx="4" '
                 f'fill="none" stroke="#ffffff" stroke-opacity=".16"/>')
        o.append(f'<text x="{label_w+plot_w+14:.1f}" y="{y+16.5:.1f}" font-size="{FS_V}" '
                 f'font-weight="700" fill="{col}">{v:+.2f}{suffix}</text>')
        if sub:
            o.append(f'<text x="{w-36}" y="{y+16:.1f}" font-size="{FS_S}" fill="{TXT}" '
                     f'text-anchor="end">{_esc(sub)}</text>')
    o.append('</svg>')
    return ''.join(o)


# ------------------------------------------------------------------ 回测视觉重点
def backtest_visual_svg(cases, w=W, h=None, title='', **kw):
    """回测"最接近案例"的次日表现 —— 逐笔画出 高低范围 + 开→收。

    这是回测部分最该被看见的东西：**不是均值，而是离散度**。
    均值 +0.5% 可能是"全挤在 0 附近"，也可能是"一半 +5% 一半 −4%" ——
    对 T+1 隔日模式，后者才是真实风险敞口。

    cases: [(标签, 开盘%, 收盘%, 最高%, 最低%), ...]
    """
    if not cases:
        return ''
    pfx = 'bv'
    row_h = 52
    pad_t = 62 if title else 30
    # ⚠️ 底部必须留 **两行**：刻度行 + 图例行。
    # 曾把图例和刻度都放在 h-12，结果图例文字（从 label_w 开始横向铺开）
    # 直接压在 −1%/+1% 刻度上（实测发现）。
    h = h or (pad_t + row_h * len(cases) + 64)
    label_w, num_w = 196, 108
    plot_w = w - label_w - num_w - 32
    lim = max(3.0, max(max(abs(c[2]), abs(c[4]), abs(c[1]), abs(c[3])) for c in cases) * 1.12)
    mid = label_w + plot_w / 2

    def X(v):
        return mid + v / lim * (plot_w / 2)

    o = [_open(w, h, pfx, title)]
    for g in (-2, -1, 1, 2):
        if abs(g) <= lim:
            o.append(f'<line x1="{X(g):.1f}" y1="{pad_t-12}" x2="{X(g):.1f}" y2="{h-58}" '
                     f'stroke="{GRID}" stroke-width="1" stroke-dasharray="2,5" opacity=".6"/>')
            o.append(f'<text x="{X(g):.1f}" y="{h-44}" font-size="{FS_S}" fill="{TXT}" '
                     f'text-anchor="middle">{g:+d}%</text>')
    o.append(f'<line x1="{mid:.1f}" y1="{pad_t-12}" x2="{mid:.1f}" y2="{h-58}" '
             f'stroke="{AXIS}" stroke-width="1.2" opacity=".85"/>')

    for i, (lab, oo, cc, hh, ll) in enumerate(cases):
        y = pad_t + i * row_h + row_h / 2
        o.append(f'<text x="{label_w-14}" y="{y+4.5:.1f}" font-size="{FS}" fill="{TXT_HI}" '
                 f'text-anchor="end">{_esc(lab)}</text>')
        x1, x2 = X(ll), X(hh)
        o.append(f'<rect x="{min(x1,x2):.1f}" y="{y-8:.1f}" width="{max(2.5,abs(x2-x1)):.1f}" '
                 f'height="16" rx="3" fill="#2b4c6f" opacity=".75"/>')
        col = _updown(cc)
        xa, xb = X(oo), X(cc)
        o.append(f'<rect x="{min(xa,xb):.1f}" y="{y-5:.1f}" width="{max(3.0,abs(xb-xa)):.1f}" '
                 f'height="10" rx="2.5" fill="{col}" filter="url(#{pfx}glow)"/>')
        o.append(f'<circle cx="{xa:.1f}" cy="{y:.1f}" r="3.6" fill="#0b1a2b" '
                 f'stroke="{TXT_HI}" stroke-width="1.5"/>')
        o.append(f'<text x="{w-36}" y="{y+4.5:.1f}" font-size="{FS_V}" font-weight="700" '
                 f'fill="{col}" text-anchor="end">{cc:+.2f}%</text>')

    o.append(f'<line x1="24" y1="{h-30}" x2="{w-36}" y2="{h-30}" stroke="{GRID}" '
             f'stroke-width="1" opacity=".45"/>')
    o.append(f'<text x="24" y="{h-11}" font-size="{FS_S}" fill="{TXT}">'
             f'○ 开盘　▬ 开→收（红涨绿跌）　灰条 = 当日高低范围</text>')
    o.append('</svg>')
    return ''.join(o)


# ------------------------------------------------------------------ 分布直方
def hist_svg(values, w=W, h=224, title='', buckets=8):
    """收益分布直方图（回测离散度）。"""
    if not values:
        return ''
    pfx = 'hs'
    lo, hi = min(values), max(values)
    if hi == lo:
        hi = lo + 1
    bw = (hi - lo) / buckets
    cnt = [0] * buckets
    for v in values:
        cnt[min(buckets - 1, int((v - lo) / bw))] += 1
    cmax = max(cnt) or 1
    pad_l = 64
    pad_t = 62 if title else 30
    pad_b = 38
    plot_w, plot_h = w - pad_l - 28, h - pad_t - pad_b
    step = plot_w / buckets

    o = [_open(w, h, pfx, title)]
    for i, c in enumerate(cnt):
        x = pad_l + i * step
        bh = max(1.2, c / cmax * plot_h)
        mid_v = lo + bw * (i + 0.5)
        col = RAISE if mid_v > 0 else FALL
        o.append(f'<rect x="{x+4:.1f}" y="{pad_t+plot_h-bh:.1f}" width="{step-8:.1f}" '
                 f'height="{bh:.1f}" fill="{col}" opacity=".85" rx="3"/>')
        if c:
            o.append(f'<text x="{x+step/2:.1f}" y="{pad_t+plot_h-bh-7:.1f}" font-size="{FS}" '
                     f'fill="{TXT_HI}" text-anchor="middle">{c}</text>')
        o.append(f'<text x="{x+step/2:.1f}" y="{h-13}" font-size="{FS_S}" fill="{TXT}" '
                 f'text-anchor="middle">{lo+bw*i:+.1f}</text>')
    o.append(f'<line x1="{pad_l}" y1="{pad_t+plot_h}" x2="{pad_l+plot_w}" '
             f'y2="{pad_t+plot_h}" stroke="{AXIS}" stroke-width="1.1" opacity=".75"/>')
    o.append(f'<text x="{pad_l-12}" y="{pad_t+13}" font-size="{FS}" fill="{TXT}" '
             f'text-anchor="end">{cmax}</text>')
    o.append(f'<text x="{pad_l-12}" y="{pad_t+plot_h}" font-size="{FS}" fill="{TXT}" '
             f'text-anchor="end">0</text>')
    o.append('</svg>')
    return ''.join(o)


# ------------------------------------------------------------------ 迷你走势
def sparkline_svg(values, w=210, h=40, col=None):
    """迷你走势线（卡片角落用）。"""
    if not values or len(values) < 2:
        return ''
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    step = w / (len(values) - 1)
    pts = ' '.join(f'{i*step:.1f},{h-4-(v-lo)/span*(h-10):.1f}' for i, v in enumerate(values))
    c = col or _updown(values[-1] - values[0])
    return (f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
            f'xmlns="http://www.w3.org/2000/svg">'
            f'<polyline points="{pts}" fill="none" stroke="{c}" stroke-width="1.8" '
            f'stroke-linejoin="round" stroke-linecap="round"/></svg>')
