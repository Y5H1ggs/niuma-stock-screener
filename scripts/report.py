# -*- coding: utf-8 -*-
"""
report.py — 一键生成 HTML 深度分析报告（卡片式版式 · 顶部含券商式评级）

用法:
    python report.py 600000
    python report.py 600000 --title 午盘深度分析 --sector-top 4
    python report.py 600000 --notes notes.json      # 注入人工研判段落
    python report.py 600000 --no-bt --no-ladder     # 跳过回测/连板梯队（快）

⚠️ 首次使用必须先完成【基础数据录入】：把 config.example.json 复制为 config.json，
   填上你自己的 cash（资金）、allowed_prefixes（交易权限）、fee_rate_roundtrip（费用率）等。
   脚本里没有任何预设的个人参数；--cash 省略时取 config.json 的值。

notes.json（可选，全部字段都可缺省；缺省时用规则自动生成）:
{
  "title":    "午盘深度分析",
  "subtitle": "本次为验证级分析：……",
  "rating_note": "评级理由的补充说明（会拼在模型理由之后）",
  "verdict":  "结论段……",
  "bull":     ["支持点1", "支持点2"],
  "bear":     ["反对点1", "反对点2"],
  "scenarios":[["次日低开","约 70~80% 🔴 高概率","开盘卖胜率仅 20~30%"]],
  "watch":    ["收盘价站不站得住 7.70", "……"],
  "oneline":  "一句话结论"
}

评级口径：100 分制机械打分 → 券商投资评级五档 买入/增持/中性/减持/卖出。
评级为模型输出，不构成任何买卖指令。
"""
import sys
import os
import json
import argparse
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cfg
import ds
import charts
from ds import (_fl, _ok, snapshot, ulist, fflow, slist, sector_stats, kline_qq,
                indicators, kdj, mood, n100, allowed, tsym, get, MARKET_MAIN)

# 板块属性桶（不是题材，参与"相对强度"会失真）
BUCKET = ('机构重仓', '融资融券', '沪股通', '深股通', '标准普尔', 'MSCI中国', '富时罗素',
          '中证500', '上证180', '沪深300', '创业板综', '科创板', '专精特新', '央企改革',
          '信托重仓', 'AB股', 'AH股', '破净股', 'GDR', '小盘股', '大盘股', '中盘股',
          '微盘股', '转债标的', '预盈预增', '预亏预减', '养老金', '社保重仓', 'QFII重仓',
          'MSCI中国A股', '举牌', '含B股', '含H股')

# 券商投资评级五档（100 分制映射）
RATING_TIERS = [
    (80, '买入',   'r', '多维共振，风险报酬比占优'),
    (65, '增持',   'ro', '多数维度偏正面，个别项需观察'),
    (45, '中性',   'y', '多空互见，缺乏一致性方向'),
    (30, '减持',   'g', '多项维度转负，风险报酬比不利'),
    (0,  '卖出',   'g', '多维度全面转弱'),
]


def pick_sectors(d):
    """板块基准选取（P34 → P35 修订，2026-09-21）。

    返回 (best, peer)：
      best —— 相关板块里**板块级主力净额最大**者，用于展示"今天这个方向的钱有多少"；
      peer —— **相对强度主判据**：取「行业基准」与「题材基准」中相对强度**更低（更不利）**的一个。

    为什么要"取更不利者"（P35）：
      这两个基准都**不可挑选** —— 行业＝东财口径、每票恰一个；题材＝所属概念里资金最大者、
      规则固定为"取最大"。但它们仍可能给出相反结论，此时**绝不允许挑有利的那个**：
      取更不利的一侧，正是对"用算法给自己挑最有利解释"这个错误的直接修复。
      实证：600519 标的乙，行业基准（电力 112 只）+0.23pp vs 题材基准 −0.49pp；
            旧口径挑中"光伏发电 13 只"得 +0.53pp 排 3/13，真实同业"碳化硅 47 只"为 −1.18pp 排 35/47。

    回退链：两个基准都不可用 → 退回"成分数最少的板块"（旧 P34 逻辑）→ 再退回第一个板块。
    ⚠️ 副作用：会向 d 写入 basis_divergence（两个基准方向矛盾时为 True），供评级与判读标注。
    """
    secs = [x for x in (d.get('sectors') or []) if x.get('rel') is not None]
    if not secs:
        return None, None
    rich = [x for x in secs if x.get('board_zl') is not None]
    best = max(rich, key=lambda x: x['board_zl']) if rich else secs[0]
    based = [x for x in secs if x.get('basis')]
    if based:
        peer = min(based, key=lambda x: x['rel'])
        if len(based) >= 2:
            rels = [x['rel'] for x in based]
            d['basis_divergence'] = (min(rels) < 0 < max(rels))
        return best, peer
    pool = [x for x in secs if x.get('n')]
    return best, (min(pool, key=lambda x: x['n']) if pool else best)


def score(d, notes):
    """100 分制机械打分 → 券商式评级。四维：技术30 / 资金30 / 板块25 / 情绪15。

    校准原则（v2）：
      ① **基准分 = 该维度满分的 50%**（技术15 / 资金15 / 板块12 / 情绪8），
         即"无任何特征的普通票"= 50 分，正好落在「中性」档；
      ② 每维**加分项合计 = 基准分**（使理论极值恰好 = 满分，不靠截断凑分）；
      ③ 减分项与加分项基本对称；任一维度触底按 0 计、触顶按满分计。
      这样分数才真正区分质量，而不是"人人 29/30"。
    """
    s = d['snap']
    ind = d.get('ind') or {}
    flow = d['flow']
    dims = []

    # ---------------- 技术面 30（基准 15，加分项合计 15）
    items = []
    if ind:
        sc = 15.0
        b = ind['boll']
        if ind.get('bull'):
            sc += 4; items.append(('均线呈完整多头排列', +4))
        else:
            sc -= 4; items.append(('均线未成多头排列', -4))
        dev5 = (s['price'] / ind['ma5'] - 1) * 100 if ind.get('ma5') else 0
        if dev5 >= 0:
            sc += 2; items.append((f'现价站上 MA5（{dev5:+.2f}%）', +2))
        else:
            sc -= 3; items.append((f'现价跌破 MA5（{dev5:+.2f}%）', -3))
        if ind['macd']['dif'] > ind['macd']['dea']:
            sc += 2; items.append(('MACD 金叉（DIF &gt; DEA）', +2))
        else:
            sc -= 2; items.append(('MACD 死叉（DIF &lt; DEA）', -2))
        pb = b['pctb']
        if pb >= 95:
            sc -= 5; items.append((f'BOLL 位于通道 {pb:.0f}% 分位，贴/破上轨（位置风险）', -5))
        elif pb >= 80:
            sc -= 2; items.append((f'BOLL 位于通道 {pb:.0f}% 分位，偏上', -2))
        elif pb <= 40:
            sc += 1; items.append((f'BOLL 位于通道 {pb:.0f}% 分位，偏下', +1))
        else:
            sc += 2; items.append((f'BOLL 位于通道 {pb:.0f}% 分位，居中（最佳介入带）', +2))
        r = ind.get('rsi14', 50)
        if r > 75:
            sc -= 5; items.append((f'RSI14 {r:.1f} 深度超买', -5))
        elif r > 70:
            sc -= 3; items.append((f'RSI14 {r:.1f} 超买', -3))
        elif r >= 45:
            sc += 2; items.append((f'RSI14 {r:.1f} 健康区间', +2))
        elif r < 30:
            sc -= 1; items.append((f'RSI14 {r:.1f} 超卖（趋势偏弱）', -1))
        else:
            items.append((f'RSI14 {r:.1f} 偏弱但不极端', 0))
        c = ind.get('cci20', 0)
        if c > 200:
            sc -= 4; items.append((f'CCI20 {c:.0f} 极度超买', -4))
        elif c > 100:
            sc -= 2; items.append((f'CCI20 {c:.0f} 超买', -2))
        elif c >= 0:
            sc += 1; items.append((f'CCI20 {c:.0f} 偏强未过热', +1))
        else:
            sc -= 1; items.append((f'CCI20 {c:.0f} 为负，动能转弱', -1))
    else:
        sc = 15.0
        items.append(('技术指标未取到（K 线源不可用），本维度按中性 15/30 计', 0))
    v = d.get('vr20', 0)
    if d.get('intraday'):
        items.append(('盘中量为<b>当日累计</b>，与 20 日<b>全天</b>均量口径不可比 '
                      f'（现 {v:.2f} 倍），本项<b>不计分</b>；量能请以「量比」与'
                      '「占昨日全日量比例」为准', 0))
    elif v >= 2:
        sc += 2; items.append((f'成交量为 20 日均量 {v:.2f} 倍，显著放量', +2))
    elif v >= 1.5:
        sc += 1; items.append((f'成交量为 20 日均量 {v:.2f} 倍，温和放量', +1))
    elif v < 1:
        sc -= 2; items.append((f'成交量为 20 日均量 {v:.2f} 倍，缩量', -2))
    else:
        items.append((f'成交量为 20 日均量 {v:.2f} 倍，平量（无信息量）', 0))
    dims.append(('技术面', max(0, min(30, sc)), 30, items))

    # ---------------- 资金面 30（基准 15，加分项合计 15）
    items = []
    if not d.get('flow_ok', True):
        # 取数失败绝不参与加减分，按中性计，否则会给出与事实相反的极低分
        dims.append(('资金面', 15, 30,
                     [('资金流数据获取失败（ulist 限流），本维度<b>按中性 15/30 计</b>，不参与加减分', 0)]))
    else:
        sc = 15.0
        zl_ = flow.get('zl')
        if zl_ is None:
            items.append(('主力净额未取到，不参与计分', 0))
        elif zl_ > 0:
            sc += 4; items.append((f"主力净流入 {money(zl_)}", +4))
        elif zl_ < 0:
            sc -= 5; items.append((f"主力净流出 {money(abs(zl_))}", -5))
        else:
            items.append(('主力净额为 0，无方向信息、不参与计分', 0))
        np_ = flow.get('net_pct')
        if np_ is None:
            items.append(('主力净占比未取到，不参与计分', 0))
        elif np_ >= 30:
            sc += 3; items.append((f'主力净占比 {np_:.2f}%，强势介入', +3))
        elif np_ >= 10:
            sc += 2; items.append((f'主力净占比 {np_:.2f}%，较强', +2))
        elif np_ > 0:
            sc += 1; items.append((f'主力净占比 {np_:.2f}%，仅属温和', +1))
        elif np_ < 0:
            sc -= 3; items.append((f'主力净占比 {np_:.2f}%，为负', -3))
        else:
            items.append(('主力净占比 0%，无方向信息、不参与计分', 0))
        fd = d.get('flow_days') or []
        if len(fd) >= 3:
            neg = sum(1 for x in fd if _fl(x[1]) < 0)
            if neg <= 1:
                sc += 3; items.append((f'近 {len(fd)} 日主力负值仅 {neg} 天，连续性良好', +3))
            elif neg == 2:
                sc += 2; items.append((f'近 {len(fd)} 日主力负值 {neg} 天，基本连续', +2))
            elif neg >= len(fd) * 0.5:
                sc -= 3; items.append((f'近 {len(fd)} 日主力负值 {neg} 天，持续失血', -3))
            else:
                sc -= 1; items.append((f'近 {len(fd)} 日主力负值 {neg} 天，不够稳定', -1))
        elif fd:
            # 样本不足 3 天时，"0 天负值"只是碰巧，不构成"连续性良好"，禁止据此加分
            items.append((f'日线级资金流仅取到 {len(fd)} 天（样本不足 3 天），不判连续性、不予加分', 0))
        else:
            items.append(('日线级资金流未取到，历史连续性未参与计分', 0))
        xl_ = flow.get('xl')
        if xl_ is None:
            items.append(('超大单数据未取到，不参与计分', 0))
        elif xl_ > 0:
            sc += 3; items.append((f"超大单净流入 {money(xl_)}（机构级）", +3))
        elif xl_ < 0:
            sc -= 3; items.append((f"超大单净流出 {money(abs(xl_))}", -3))
        else:
            items.append(('超大单净额为 0，无方向信息、不参与计分', 0))
        ot, it = s['outer'], s['inner']
        if s.get('pre_open') or (ot == 0 and it == 0):
            items.append(('内外盘数据不可用（盘前快照），不参与计分', 0))
        elif ot + it == 0:
            items.append(('内外盘数据不可用，不参与计分', 0))
        else:
            gap = (ot - it) / (ot + it) * 100
            if gap >= 10:
                sc += 2; items.append((f'外盘显著大于内盘（主动买占优 {gap:+.1f}%）', +2))
            elif gap <= -10:
                sc -= 2; items.append((f'内盘显著大于外盘（主动卖占优 {gap:.1f}%）', -2))
            else:
                items.append((f'内外盘基本均衡（差 {gap:+.1f}%），无方向信息、不参与计分', 0))
        dims.append(('资金面', max(0, min(30, sc)), 30, items))

    # ---------------- 板块面 25（基准 12 + 个股级 13；板块级加分受相对强度闸门约束）
    items = []
    sc = 12.0
    best, peer = pick_sectors(d)
    if best:
        # ★ 相对强度主判据（P35，2026-09-21）=「行业基准」与「题材基准」中**更不利**的那个
        #   （见 pick_sectors）。不再"挑最热"或"挑最细" —— 挑的动作本身就能挑出最有利的解释：
        #   同一只票挑中"光伏发电 13 只"得 +0.53pp、挑中"碳化硅 47 只"得 −1.18pp。
        ref = peer or best
        rel = ref['rel']
        rb = ref.get('basis')
        ptxt = (f"{rb}基准 {ref['name']}（{ref['n']} 只）" if rb
                else f"基准板块 {ref['name']}（{ref['n']} 只）")

        # ★ 板块级加分闸门（2026-09-18 新增，P33）：
        #   「板块主力净额」「板块红盘率」是**板块级**指标，与该股无关。板块一强，
        #   连一只明显被落下的票也能白拿这 3~7 分 —— 与铁律 1/3 直接矛盾：
        #   资金没流到它身上时，板块走强对它反而是负面信号（说明钱选了别的票）。
        #   → 个股跑输同业中位数时，板块级**正分归零**（负分照扣）。
        peel = rel < 0

        def _blk(txt, v, peel=peel):
            """板块级加减分：个股跑输基准时，正分归零（负分保留）。"""
            if peel and v > 0:
                return (txt + '（个股跑输基准中位数 → 该板块级加分归零）', 0)
            return (txt, v)

        if rel >= 1:
            sc += 6; items.append((f"领先{ptxt}中位数 {rel:.2f}pp", +6))
        elif rel >= 0:
            sc += 4; items.append((f"略领先{ptxt}中位数 {rel:.2f}pp", +4))
        elif rel >= -1:
            sc -= 3; items.append((f"跑输{ptxt}中位数 {abs(rel):.2f}pp", -3))
        elif rel >= -2:
            sc -= 6; items.append((f"显著跑输{ptxt}中位数 {abs(rel):.2f}pp", -6))
        else:
            # ★ 铁律 1/3 的量化落点：板块整体走强（或至少不弱）而个股大幅落后，
            #   说明资金在同一个方向里选了别的票 —— 这是 T+1 模式最该躲开的形态，
            #   不论均线多漂亮。故给最重的一档扣分。
            sc -= 10; items.append((f"严重跑输{ptxt}中位数 {abs(rel):.2f}pp"
                                    f"（资金在同方向内选择了其他标的，脱离板块式走弱）", -10))
        # ★ 基准分歧（P35）：两个**不可挑选**的基准给出相反结论时，必须显式标注，
        #   并说明本次按更不利者计分 —— 否则读者会以为"它领先"，实际只是选了有利的基准。
        if d.get('basis_divergence'):
            other = next((x for x in d['sectors']
                          if x.get('basis') and x['bk'] != ref['bk']
                          and x.get('rel') is not None), None)
            if other:
                items.append((f"基准分歧：{other['basis']}基准 {other['name']} 为 "
                              f"{other['rel']:+.2f}pp，本次按更不利者（{rb or '基准'}）计分，"
                              f"不作有利解释", 0))
        elif (peer is not None and best is not peer and best.get('rel') is not None
                and best['rel'] > 0 >= rel and (best['rel'] - rel) >= 1.0):
            # ★ 跟风位：只在资金最热的那个板块里"领先"、基准里跑输，
            #   说明它被大板块的中位数平均效应救了，实际没拿到同方向的资金。
            sc -= 3; items.append(
                (f"最热板块 {best['name']}（{best['n']} 只）领先 {best['rel']:+.2f}pp，"
                 f"但{ptxt}跑输 {abs(rel):.2f}pp —— 仅靠宽口径中位数「沾光」，属跟风位", -3))
        zl = best.get('board_zl')
        if zl is None:
            zl = best.get('zl')
        if zl is None:
            items.append(('板块主力净额未取到（盘前/限流），不参与计分', 0))
        elif zl >= 50:
            t, v = _blk(f'主线方向 {best["name"]} 主力净流入 {zl:.1f} 亿，资金主战场', 4)
            sc += v; items.append((t, v))
        elif zl > 0:
            t, v = _blk(f'主线方向 {best["name"]} 主力净流入 {zl:.1f} 亿', 2)
            sc += v; items.append((t, v))
        elif zl < 0:
            sc -= 3; items.append((f'主线方向 {best["name"]} 主力净流出 {abs(zl):.1f} 亿', -3))
        else:
            items.append(('板块主力净额为 0，无方向信息、不参与计分', 0))
        ur = ref['up'] / max(1, ref['n']) * 100
        if ur >= 80:
            t, v = _blk(f'{ptxt}红盘率 {ur:.0f}%，普涨', 2); sc += v; items.append((t, v))
        elif ur >= 50:
            t, v = _blk(f'{ptxt}红盘率 {ur:.0f}%', 1); sc += v; items.append((t, v))
        else:
            sc -= 3; items.append((f'{ptxt}红盘率仅 {ur:.0f}%，普跌', -3))
        inlim = any(str(c) == str(d['code']) for x in d['sectors'] for c, _, _ in x['limit_up'])
        if inlim:
            sc += 1; items.append(('在板块涨停／准涨停名单内（资金接力类型包含它）', +1))
        else:
            items.append(('不在板块涨停名单内（属跟风位）', 0))
    else:
        items.append(('板块数据缺失，本维度按中性 12/25 计', 0))
    dims.append(('板块面', max(0, min(25, sc)), 25, items))

    # ---------------- 情绪面 15（基准 8，加分项合计 7）
    # 注：这一维是"市场级"指标，同日对所有个股几乎同分，只承担环境闸门作用。
    items = []
    sc = 8.0
    m = d.get('mood') or {}
    valid_m = [v for v in m.values() if v.get('zl_yi') is not None]
    negs = [v for v in valid_m if v['zl_yi'] < 0]
    if not valid_m:
        items.append(('板块级资金数据未取到（盘前/限流），该项按中性计、不加减分', 0))
    elif negs:
        sc -= 4; items.append((f'昨日涨停系"价强钱撤"（{len(negs)}/{len(valid_m)} 个板指主力净流出）', -4))
    else:
        sc += 4; items.append(('昨日涨停系资金净流入，接力环境健康', +4))
    lad = d.get('ladder') or []
    n2 = sum(1 for x in lad if x[2] >= 2)
    if n2 >= 10:
        sc += 3; items.append((f'连板梯队 {n2} 只，情绪活跃', +3))
    elif n2 >= 3:
        sc += 1; items.append((f'连板梯队 {n2} 只，情绪中性', +1))
    elif lad:
        sc -= 3; items.append((f'连板梯队仅 {n2} 只，情绪偏冷', -3))
    dims.append(('情绪面', max(0, min(15, sc)), 15, items))

    total = sum(x[1] for x in dims)
    for lo, lab, tone, desc in RATING_TIERS:
        if total >= lo:
            rating, tone_, rdesc = lab, tone, desc
            break
    missing = []
    if not d.get('flow_ok', True):
        missing.append('资金面')
    if not d.get('sectors'):
        missing.append('板块面')
    if not ind:
        missing.append('技术面')
    if not d.get('mood') and not d.get('ladder'):
        missing.append('情绪面')

    # ★ 数据完整度闸门（P30，2026-09-17 新增）
    # 资金面(30) + 板块面(25) 合计 55 分，是判断"今日能不能做"的两个主维度。
    # 两者同时取数失败时，它们各自的"基准分"会把任何票都往 50 分（中性档）拉：
    # 数字看上去像结论，实际信息量为零 —— 比直说"不予评级"危险得多。
    # 实测案例：标的甲 09-17 盘中逆板块跌 2.96%、板块内排名 345/356，
    # 打出"中性 52/100"，而当日 −4% 的真实答案被这个数字掩盖。
    no_rating = all(x in missing for x in ('资金面', '板块面'))
    if no_rating:
        rating, tone_, rdesc = '数据不足', 'y', '资金面与板块面均取数失败，本次不提供评级'
    return {'total': round(total), 'rating': rating, 'tone': tone_, 'desc': rdesc,
            'dims': dims, 'missing': missing, 'no_rating': no_rating,
            'selfcheck': d.get('selfcheck')}


# ------------------------------------------------------------------ 工具
def esc(s):
    return (str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


def cls(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return ''
    return 'up' if v > 0 else ('dn' if v < 0 else '')


def pct(v, sign=True):
    try:
        return f'{float(v):+.2f}%' if sign else f'{float(v):.2f}%'
    except (TypeError, ValueError):
        return '—'


def money(v, unit='万'):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return '—'
    return f'{v / 1e4:+.0f}{unit}' if unit == '万' else f'{v / 1e8:+.2f}亿'


def _pct(v):
    """东财百分比字段（f184 主力净占比等）以 ×100 存储：真实 4.11% 返回 411。
    ⚠️ 不做 /100 会让报告直接印出「主力净占比 411.00%，强势介入」这种荒谬数字，
       并把「温和流入」误判为「强力介入」而多加分。
    合理性保护：真正的净占比是「主力净额 / 当日成交额」，不可能 |x| > 100，
       超过即判定为量纲未归一，自动 /100。返回 None 表示取数失败（不参与计分）。"""
    if not _ok(v):
        return None
    x = _fl(v)
    if abs(x) > 100:
        x = x / 100.0
    return x


def minute(code):
    """腾讯分时：[('0930', price, vol_hand), ...]"""
    url = f'https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={tsym(code)}'
    r = get(url, ref='https://gu.qq.com/')
    if not r:
        return []
    try:
        arr = json.loads(r)['data'][tsym(code)]['data']['data']
        out = []
        for x in arr:
            p = x.split()
            out.append((p[0], _fl(p[1]), _fl(p[2]) if len(p) > 2 else 0.0))
        return out
    except Exception:
        return []


def vwap(pts):
    tv = sum(p[2] for p in pts)
    return (sum(p[1] * p[2] for p in pts) / tv) if tv else float('nan')


def board_flows():
    """全市场概念+行业板块的主力净额 {bk: (主力亿, 板指涨跌%)}。数据取自 ds.board_index()（进程内缓存）。
    ⚠️ 东财 clist 盘前/限流时 f62 返回 "-"，此时**不入表**（而不是记 0）——
       否则"板块主力 0 亿"会被当成"资金持平"参与打分，掩盖取数失败。"""
    out = {}
    for bk, (nm, kind, zl, chg) in (ds.board_index().get('by_bk') or {}).items():
        if zl is None:
            continue
        out[bk] = (zl, chg)
    return out


def ladder_scan(topn=80, pre=False):
    """真实扫描今日涨停股的连板 streak（不用 BK1638，那会高估数倍）。
    返回 (梯队列表, 取数失败数, 是否可用)。
    ⚠️ 盘前/限流/口径不符（pre=True）时东财 f3 不可依赖，此时必须显式返回「不可用」——
       否则会伪造成"2 板及以上 0 只 → 情绪偏冷"，把取数失败当利空。"""
    today = datetime.date.today().strftime('%Y-%m-%d')
    rows = ds.clist(fid='f3', fs=MARKET_MAIN, pages=6, fields='f12,f14,f2,f3')
    valid = [] if pre else [x for x in rows if _ok(x.get('f3'))]
    if not valid:
        return [], 0, False
    ups = [x for x in valid if _fl(x.get('f3')) >= 9.7][:topn]
    out, failed = [], 0
    for x in ups:
        code = str(x.get('f12'))
        k = kline_qq(code, 30)
        if not k:
            failed += 1
            continue
        streak = 0
        for i in range(len(k) - 1, 0, -1):
            if k[i]['c'] >= round(k[i - 1]['c'] * 1.1, 2) - 0.001:
                streak += 1
            else:
                break
        if k[-1]['d'] != today:      # 今日 K 未入库，补上今天这根
            streak += 1
        out.append((code, str(x.get('f14')), streak))
    out.sort(key=lambda t: -t[2])
    return out, failed, True


# ------------------------------------------------------------------ 数据汇总
def gather(code, sector_top=4, cash=None, do_bt=True, do_ladder=True, anon=False):
    if cash is None:
        cash = cfg.get('cash') or 0
    if anon:
        # 脱敏模式（--anon，v1.2.7）：本报告的读者不是账户持有人时使用。
        # 归零后下游自动变成中性：资金占用行改印"—"、自证校验按 100 股（每边最低佣金
        # 的摊薄口径与持仓规模无关，仅用于费用模型自洽断言），不泄露任何本金/持仓。
        cash = 0
    d = {'code': code, 'cash': cash, 'anon': anon,
         'fee': float(cfg.get('fee_rate_roundtrip', 0.0018)),
         'stop': float(cfg.get('stop_loss_pct', -4.0)),
         'target': float(cfg.get('take_profit_pct', 2.0))}
    if (not anon) and (not allowed(code)):
        pre = '、'.join(str(x) for x in (cfg.get('allowed_prefixes') or []))
        d['warn_allowed'] = (f'⚠️ 该标的超出你在 config.json 里声明的可交易板块'
                             f'（allowed_prefixes = {pre}），仅作分析展示。')

    snap = snapshot([code])
    s = snap.get(str(code))
    if not s:
        raise SystemExit(f'{code} 快照获取失败')
    d['snap'] = s

    # ---- 盘前修正（09:15 前 / 当日未开盘）----
    # 腾讯在开盘前返回的是"今日未开盘"空壳：open=0、vol=0、chg=0.00、lb=0.00。
    # 直接用会把涨跌幅/量比/成交额/技术指标全部算成 0（曾产出"+0.00% 量比0.00"的废报告）。
    # 此时回退到【上一交易日收盘】作为分析基准，并在报告中显式声明。
    d['pre_open'] = False
    if _fl(s.get('vol')) <= 0 or _fl(s.get('open')) <= 0:
        kp, _src0 = ds.kline(code, 6)
        _today = datetime.date.today().strftime('%Y-%m-%d')
        if kp and kp[-1]['d'] == _today and len(kp) >= 3:
            kp = kp[:-1]
        if len(kp) >= 2:
            last, prev = kp[-1], kp[-2]
            s['price'], s['prev'] = last['c'], prev['c']
            s['open'], s['high'], s['low'] = last['o'], last['h'], last['l']
            s['vol'] = last['v']
            s['chg'] = (last['c'] / prev['c'] - 1) * 100 if prev['c'] else 0.0
            s['limit_up'] = round(prev['c'] * 1.1, 2)
            s['sealed'] = last['c'] >= s['limit_up'] - 1e-6
            s['time'] = last['d'].replace('-', '') + '150000'
            s['hs'] = float('nan')
            s['lb'] = float('nan')
            s['amount'] = float('nan')
            s['outer'] = s['inner'] = 0
            s['bids'] = s['asks'] = []
            s['pre_open'] = True
            d['pre_open'] = True
            d['prev_date'] = last['d']

    # 指数必须带显式前缀：裸 000001 会被判成深市 = 平安银行（曾把上证指数显示成 11.70）
    idx = snapshot(['sh000001', 'sz399001', 'sz399006', 'sh000688'])
    d['index'] = idx

    # ---- 板块基准（P35，2026-09-21 重做）----
    # 旧做法：拉 slist 前 8 个板块 → 按资金挑"最热"当方向、按成分数挑"最少"当同业。
    # 问题：**任何"挑"的动作都可能挑出对自己最有利的解释**。同一只票的板块相对强度
    #   因挑中"光伏发电 13 只"得 +0.53pp（排 3/13），因挑中"碳化硅 47 只"得 −1.18pp（排 35/47）
    #   —— 结论完全相反（600519 标的乙实测）。
    # 新做法：只用两个**不可挑选**的基准：
    #   ① 行业基准 = 东财 f127 所属行业（交易所口径，每票恰一个，不是我们选的）；
    #   ② 题材基准 = 该股所属概念里**今日主力净额最大**者（规则固定为"取最大"，
    #      而不是"取对个股最有利的"）。
    #   两者矛盾时不得只报有利的那个，见 pick_sectors() 的"取更不利者"。
    prof = ds.stock_profile(code)
    d['profile'] = prof
    bidx = ds.board_index().get('by_name') or {}
    ibk = (bidx.get(prof['industry'])[0] if prof.get('industry') in bidx else None)
    tbk, tzl = None, None
    for cn in (prof.get('concepts') or []):
        rec = bidx.get(cn)
        if not rec or rec[1] != '概念' or rec[2] is None:
            continue
        if tzl is None or rec[2] > tzl:
            tbk, tzl = rec[0], rec[2]
    d['ind_bk'], d['theme_bk'], d['theme_zl'] = ibk, tbk, tzl
    # G9：板块对比前先核对主业 —— 主营构成（按产品）决定"这是业绩票还是题材票"
    d['mainbiz'] = ds.main_business(code)

    # 其余板块：slist 相关度前若干，按"板块主力净额 / 涨幅中位数"排序，补充方向视野
    mine = [x for x in slist(code) if x['name'] not in BUCKET]
    bf = {} if d['pre_open'] else board_flows()
    cand, seen_bk = [], set()
    for x in mine:
        if x['bk'] in seen_bk:
            continue
        st = sector_stats(x['bk'], pre=d['pre_open'])
        if not st.get('ok') or st['n'] < 5:
            continue
        seen_bk.add(x['bk'])
        cand.append(({'bk': x['bk'], 'name': x['name']}, st))
    # ★ 两个基准板块**必须**进样本：即使 slist 没返回它、或 sector_top 会截掉它
    for bk, basis in ((ibk, '行业'), (tbk, '题材')):
        if not bk or bk in seen_bk:
            continue
        nm = (ds.board_index()['by_bk'].get(bk) or ('?',))[0]
        st = sector_stats(bk, pre=d['pre_open'])
        if not st.get('ok') or st['n'] < 5:
            continue
        seen_bk.add(bk)
        cand.append(({'bk': bk, 'name': nm}, st))
    for x_, _st in cand:                    # 打基准标记
        x_['basis'] = '行业' if x_['bk'] == ibk else ('题材' if x_['bk'] == tbk else None)

    def _skey(t):
        x_, st_ = t
        z = bf.get(x_['bk'], (None, None))[0]
        return -(z if z is not None else (st_['median_chg'] if st_['median_chg'] is not None else 0))

    cand.sort(key=_skey)
    # 基准优先占位（表格里一眼可见），其余按热度补足到 sector_top
    basis_logs = [t for t in cand if t[0]['basis']]
    others = [t for t in cand if not t[0]['basis']][:max(0, sector_top - len(basis_logs))]
    pool = basis_logs + others
    n_pool = [t for t in cand if t[1].get('n')]
    d['peer_bk'] = (min(n_pool, key=lambda t: t[1]['n'])[0]['bk'] if n_pool else None)
    d['sectors'] = []
    d['sector_ok'] = bool(cand)
    for x, st in pool:
        cb = st.get('chg_by') or {}
        rk = sorted(cb.items(), key=lambda z: -z[1])
        rank = next((i + 1 for i, z in enumerate(rk) if str(z[0]) == str(code)), None)
        med = st['median_chg']
        d['sectors'].append({
            'bk': x['bk'], 'name': x['name'], 'n': st['n'], 'up': st['up'],
            'zl': st['zl_yi'], 'med': med, 'limit_up': st['limit_up'],
            'board_zl': bf.get(x['bk'], (None, None))[0],
            'rank': rank, 'rank_n': len(cb) or None,
            'rel': (round(s['chg'] - med, 2) if med is not None else None),
            'src': st.get('src'),
            'peer': (x['bk'] == d['peer_bk']),
            'basis': x.get('basis'),
        })

    ul = ulist([code]).get(str(code), {})
    d['flow_ok'] = bool(ul) and _ok(ul.get('f62'))
    d['flow'] = {
        'zl': _fl(ul.get('f62')) if _ok(ul.get('f62')) else None,
        'net_pct': _pct(ul.get('f184')),
        'xl': _fl(ul.get('f66')) if _ok(ul.get('f66')) else None,
        'dl': _fl(ul.get('f72')) if _ok(ul.get('f72')) else None,
        'zl2': _fl(ul.get('f78')) if _ok(ul.get('f78')) else None,
        'xl2': _fl(ul.get('f84')) if _ok(ul.get('f84')) else None,
    }
    d['flow_days'] = fflow(code, klt=101, lmt=12)

    pts = minute(code)
    d['minutes'] = pts
    d['vwap'] = vwap(pts) if pts else float('nan')
    if pts:
        d['touch_at'] = next((p[0] for p in pts if p[1] >= s['limit_up'] - 0.011), None)
        d['m_high'] = max(pts, key=lambda p: p[1])
        d['m_low'] = min(pts, key=lambda p: p[1])
        d['upper_shadow'] = (s['high'] - max(s['open'], s['price'])) / s['prev'] * 100 if s['prev'] else 0
        d['from_high'] = (s['price'] / s['high'] - 1) * 100 if s['high'] else 0

    k, src = ds.kline(code, 300)
    d['k_src'] = src
    if k and k[-1]['d'] == datetime.date.today().strftime('%Y-%m-%d'):
        k = k[:-1]           # 去掉今日未完成根（避免与 live 重复）
    d['k'] = k
    if k:
        d['ind'] = indicators([x['c'] for x in k], [x['h'] for x in k],
                              [x['l'] for x in k], live=s['price'])
        d['ind']['kdj'] = kdj([x['h'] for x in k] + [s['price']],
                              [x['l'] for x in k] + [s['price']],
                              [x['c'] for x in k] + [s['price']])
        v20 = [x['v'] for x in k[-20:]]
        d['vr20'] = (s['vol'] / (sum(v20) / len(v20))) if v20 and sum(v20) else 0
    # ⚠️ 盘中（09:30~14:57）的 s['vol'] 是「当日累计」，而 vr20 的分母是 20 日「全天」均量
    #    → 两个口径不可比，必然把放量算成缩量。实证 2026-09-21 09:55 标的乙：
    #      开盘 25 分钟已成交昨日全天的 20.4%、量比 2.25，却被算成「0.40 倍 → 缩量 −2 分」。
    #    故盘中一律不对量能做加减分，只保留数值供展示并强制标注口径（见 render 侧）。
    d['intraday'] = bool(s.get('time')) and 930 <= int(str(s['time'])[8:12]) < 1457

    if do_bt:
        try:
            import backtest as bt
            kk = kline_qq(code, 800)
            if len(kk) < 120:
                d['bt_err'] = f'K线仅 {len(kk)} 根，不足 120 根，回测跳过（三源均不可用或新股）'
            else:
                d['bt_k'] = kk
                d['bt'] = []
                for nm, rule in bt.RULES:
                    r = bt.backtest(kk, rule, nm)
                    if r:
                        d['bt'].append(r)
                d['bt_base'] = bt.backtest(kk, None, '基准(任意日)', base=True)
                d['bt_mod'] = bt
        except Exception as e:
            d['bt_err'] = f'{type(e).__name__}: {str(e)[:80]}'

    try:
        d['mood'] = mood(pre=d['pre_open'])
    except Exception:
        d['mood'] = {}

    if do_ladder:
        try:
            d['ladder'], d['ladder_failed'], d['ladder_ok'] = ladder_scan(pre=d['pre_open'])
        except Exception:
            d['ladder'], d['ladder_failed'], d['ladder_ok'] = [], 0, False

    # ---- 日线资金流（近 14 日，供报告的资金流向图使用）----
    # 用东财 push2his daykline：注意 ds.fflow(klt=101) 只回当日一条，拿不到历史。
    try:
        _fu = ('https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?secid=%s'
               '&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56&klt=101&lmt=0&ut=%s'
               % (ds.secid(code), ds.UT))
        _fj = json.loads(ds.get(_fu) or '{}')
        _fk = (_fj.get('data') or {}).get('klines') or []
        d['flow_daily'] = [(x.split(',')[0], ds._fl(x.split(',')[1]) / 1e4)
                           for x in _fk[-14:]]
    except Exception:
        d['flow_daily'] = []

    # ---- 自证校验（常驻，v1.3.0）----
    # 设计意图：本项目最危险的不是崩溃，而是"**看起来完全正常的错误**"——
    # 占位符被读成 0、单位差 100 倍、字段顺序读错。它们不抛异常、数字自洽，
    # 只会让结论悄悄失真。所以**每次采集后自动跑一遍**，而不是等人工偶然发现。
    # 失败项会写进报告的「数据完整度」卡；校验器自身也会输出断言条数，证明它跑过了。
    try:
        import selfcheck
        _sh = int(cash / s['price'] / 100) * 100 if s.get('price') else 0
        d['selfcheck'] = selfcheck.check_all(code, shares=_sh or 100,
                                             snap=s, kline=d.get('kl'))
    except Exception as _e:
        d['selfcheck'] = {'code': code, 'fail': [], 'warn': [], 'total': 0, 'pass': 0,
                          'grade': '?', 'error': repr(_e)}

    return d


# ------------------------------------------------------------------ 规则判读
def auto_judge(d):
    s, ind, flow = d['snap'], d.get('ind', {}), d['flow']
    pb, nb = [], []
    best, peer = pick_sectors(d)
    if best:
        # 相对强度以「行业基准 / 题材基准」中**更不利**者为判据（P35），并印出两者
        ref = peer or best
        rb = ref.get('basis')
        rt = (f"{rb}基准 {esc(ref['name'])}（{ref['n']} 只）" if rb
              else f"基准板块 {esc(ref['name'])}（{ref['n']} 只）")
        if ref['rel'] < 0:
            nb.append(f"<b>跑输{rt}中位数 {abs(ref['rel'])} 个百分点</b>"
                      f"（{s['chg']:+.2f}% vs 中位 {ref['med']:+.2f}%，排名 "
                      f"{ref.get('rank')}/{ref['n']}）——铁律第一条警示")
        else:
            pb.append(f"在{rt}内领先中位数 <b>{ref['rel']} 个百分点</b>"
                      f"（{s['chg']:+.2f}% vs 中位 {ref['med']:+.2f}%），排名 "
                      f"{ref.get('rank')}/{ref['n']}")
        other = next((x for x in d['sectors']
                      if x.get('basis') and x['bk'] != ref['bk']
                      and x.get('rel') is not None), None)
        if other:
            nb.append(f"<b>基准分歧</b>：{other['basis']}基准 <b>{esc(other['name'])}</b>"
                      f"（{other['n']} 只）为 {other['rel']:+.2f}pp，与上述结论方向相反；"
                      f"本次一律按<b>更不利者</b>计分，不挑有利解释")
        elif (best is not peer and best.get('rel') is not None and best['rel'] > 0 >= ref['rel']):
            nb.append(f"资金最热的 <b>{esc(best['name'])}</b>（{best['n']} 只）看似领先 "
                      f"{best['rel']:+.2f}pp，但基准板块跑输 —— 属<b>跟风位</b>"
                      f"（被大板块的中位数平均效应掩盖）")
    if s['limit_up'] and abs(s['high'] - s['limit_up']) < 0.011:
        pb.append('今日<b>触及涨停</b>——这是有资金真实进攻过的硬证据')
    if ind.get('bull'):
        pb.append(f"完整多头排列：MA5 {ind['ma5']:.2f} &gt; MA10 {ind['ma10']:.2f} &gt; "
                  f"MA20 {ind['ma20']:.2f} &gt; MA60 {ind['ma60']:.2f}")
    if ind.get('macd') and ind['macd']['dif'] > ind['macd']['dea']:
        pb.append(f"MACD 零轴{'上方' if ind['macd']['dif'] > 0 else '下方'}金叉"
                  f"（DIF {ind['macd']['dif']:.3f} &gt; DEA {ind['macd']['dea']:.3f}）")
    if flow.get('zl') is not None and flow['zl'] > 0:
        signs = [('+' if _fl(x[1]) > 0 else '−') for x in d.get('flow_days', [])]
        pb.append(f"主力今日净流入 <b>{money(flow['zl'])}</b>（超大单 {money(flow.get('xl') or 0)} / "
                  f"大单 {money(flow.get('dl') or 0)}），近 {len(signs)} 日符号 {' '.join(signs)}")
    if not s.get('pre_open') and s['outer'] > s['inner']:
        tot = s['outer'] + s['inner']
        pb.append(f"外盘 {s['outer'] / 1e4:.1f} 万 &gt; 内盘 {s['inner'] / 1e4:.1f} 万，"
                  f"主动买占 {s['outer'] / tot * 100:.1f}%")

    if ind.get('boll') and ind['boll']['pctb'] >= 95:
        nb.append(f"<b>BOLL(20,2) 位于通道 {ind['boll']['pctb']:.0f}% 分位</b>"
                  f"（上轨 {ind['boll']['up']:.3f}）——贴近/站上上轨")
    if ind.get('rsi14') and ind['rsi14'] > 70:
        nb.append(f"<b>RSI14 = {ind['rsi14']:.1f} 超买</b>")
    if ind.get('cci20') and ind['cci20'] > 100:
        nb.append(f"<b>CCI(20) = {ind['cci20']:.1f} 超买</b>")
    if s.get('lb') and s['lb'] < 2:
        nb.append(f"量比仅 <b>{s['lb']:.2f}</b>——量能未有效放大")
    if d.get('vwap') and s['price'] < d['vwap']:
        nb.append(f"现价 {s['price']} <b>低于日内均价 {d['vwap']:.3f}</b>，日内偏弱")
    if flow.get('net_pct') is not None:
        tag = '温和' if flow['net_pct'] < 10 else ('较强' if flow['net_pct'] < 30 else '强势')
        nb.append(f"主力净占比 <b>{flow['net_pct']:.2f}%</b>（{tag}）"
                  + ('' if flow['net_pct'] >= 10 else '，够不上"大资金强力介入"'))
    if flow.get('zl') is not None and flow['zl'] < 0:
        nb.append(f"主力<b>净流出 {money(abs(flow['zl']))}</b>，资金在离场")
    elif flow.get('zl') is None:
        nb.append('主力资金数据<b>未取到</b>（盘前/限流）——该项<b>不作为利空</b>，需盘中复核')
    return pb, nb


def auto_scenarios(d):
    """由回测主形态的统计推导概率（规则生成，人工可覆盖）。"""
    bt = d.get('bt') or []
    if not bt:
        return [], None
    s = d['snap']
    hit = None
    for r in bt:
        nm = r['name']
        if s['high'] >= s['limit_up'] - 0.011 and not s.get('sealed'):
            if nm.startswith('触板未封'):
                hit = r
                break
        if s.get('sealed') and nm == '涨停封板':
            hit = r
            break
    if hit is None:
        hit = max(bt, key=lambda r: (r['n'] >= 10, r['win_close']))
    rows = [
        ('次日低开', f"约 {100 - hit['win_open']:.0f}%",
         f"该形态开盘卖胜率仅 {hit['win_open']:.0f}%，次日开盘均值 {hit['avg_open']:+.2f}%"),
        ('次日盘中冲到 ≥+2%，给出兑现窗口', f"约 {hit['p_2']:.0f}%",
         f"次日最高均值 {hit['avg_high']:+.2f}%"),
        ('次日收盘为正', f"约 {hit['win_close']:.0f}%",
         f"样本 N={hit['n']}，收盘卖均值 {hit['avg_close']:+.2f}%"),
        ('次日跌破 −3%（进入纪律区）', f"约 {hit['p_break']:.0f}%",
         f"次日最低均值 {hit['avg_low']:+.2f}%"),
    ]
    return rows, hit


# ------------------------------------------------------------------ 版式
CSS = """
  :root{--up:#C0392B;--dn:#1E8E5A;--red:#C0392B;--green:#1E8E5A;
        --ink:#141C28;--ink2:#3B4757;--muted:#7C8798;--faint:#A9B2BE;
        --line:#E7E5DF;--hair:#F1EFE9;--bg:#F3F2EE;--card:#fff;--head:#FBFAF7;
        --navy:#12304F;--navy2:#1B4A73;--gold:#A9843F;--goldsoft:#F7F1E4;--goldline:#EADFC6;}
  *{box-sizing:border-box;}
  body{margin:0;padding:34px 18px 70px;background:var(--bg);color:var(--ink);
       font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
       font-size:14px;line-height:1.8;-webkit-font-smoothing:antialiased;}
  .wrap{max-width:960px;margin:0 auto;}
  h1{font-size:23px;font-weight:600;margin:0 0 6px;}

  /* ---------- 报头 ---------- */
  .mast{display:flex;align-items:center;justify-content:space-between;gap:20px 26px;flex-wrap:wrap;
        padding:0 2px 18px;border-bottom:1px solid var(--line);}
  .mast h1{font-size:27px;font-weight:700;letter-spacing:.5px;margin:0;color:#101825;
        display:flex;align-items:baseline;flex-wrap:wrap;}
  .mast h1 .code{font-size:18px;font-weight:500;color:var(--faint);margin-left:8px;letter-spacing:1.5px;}
  .mast h1 .kind{font-size:12px;font-weight:500;color:var(--gold);background:var(--goldsoft);
        border:1px solid var(--goldline);border-radius:999px;padding:3px 12px;margin-left:12px;
        vertical-align:middle;letter-spacing:.5px;position:relative;top:-3px;
        /* 徽章必须整体不折行：曾因外层限宽而被拆成「…深度分 / 析」，后半段还跑出了圆角底 */
        flex:0 1 auto;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .mast .tagline{font-size:12.5px;color:var(--muted);margin-top:9px;line-height:1.95;}
  .mast .tagline b{color:var(--ink2);font-weight:600;}
  .px{text-align:right;padding-bottom:2px;flex:none;}
  .px .p{font-size:30px;font-weight:700;line-height:1.05;font-variant-numeric:tabular-nums;letter-spacing:-.5px;}
  .px .c{font-size:14px;font-weight:600;margin-top:5px;font-variant-numeric:tabular-nums;}
  /* ---------- 抬头卡右栏（v1.2.8）----------
     旧版右侧只有一个价格块，而左侧 tagline 很长会撑满整行 → 价格块被 flex-wrap 挤到
     第二行左端，抬头卡**右上角整片空白**。现改为「价格 + 4 项关键指标」两栏结构：
     左栏限定 flex 基准并允许收缩（min-width:0），右栏固定不换行。 */
  .mast .mastl{flex:1 1 46%;min-width:0;}
  .mast .mastr{display:flex;align-items:center;gap:22px;flex:0 1 auto;}
  .mast .mkgrid{display:grid;grid-template-columns:repeat(2,minmax(86px,auto));
        gap:10px 20px;border-left:1px solid var(--line);padding-left:20px;}
  .mast .mk{display:flex;flex-direction:column;gap:3px;}
  .mast .mk .k{font-size:10.5px;color:var(--muted);letter-spacing:.5px;white-space:nowrap;}
  .mast .mk .v{font-size:14px;font-weight:700;color:var(--ink);
        font-variant-numeric:tabular-nums;white-space:nowrap;}
  .mast .mk .v.up{color:var(--up);}
  .mast .mk .v.dn{color:var(--dn);}
  .mast .mk .v.gold{color:#A9843F;}

  /* ---------- 卡片 ---------- */
  .card{background:var(--card);border:1px solid var(--line);border-radius:15px;
        margin:16px 0;overflow:hidden;
        box-shadow:0 1px 1px rgba(18,36,58,.04),0 10px 24px -16px rgba(18,36,58,.18);}
  .cardhead{display:flex;align-items:center;gap:11px;padding:15px 22px;
            border-bottom:1px solid var(--hair);background:var(--head);}
  .cardhead::before{content:"";width:4px;height:17px;border-radius:2px;flex:none;
            background:linear-gradient(180deg,var(--navy2),var(--gold));}
  .card.warn .cardhead::before{background:linear-gradient(180deg,#BF4030,#E5A091);}
  .card.ok .cardhead::before{background:linear-gradient(180deg,#1B7F52,#84C6A6);}
  .cardhead .tt{font-size:15px;font-weight:600;color:#16202F;letter-spacing:.3px;}
  .cardhead .badge{margin-left:auto;flex:none;font-size:11px;font-weight:500;color:var(--gold);
            background:var(--goldsoft);border:1px solid var(--goldline);
            border-radius:999px;padding:2px 11px;letter-spacing:.4px;white-space:nowrap;}
  .cardbody{padding:18px 22px 20px;}
  .cardbody > *:first-child{margin-top:0;}
  .cardbody > *:last-child{margin-bottom:0;}

  /* ---------- 卡内子卡：每个小板块一张 ---------- */
  .sub{background:#FAF9F6;border:1px solid var(--hair);border-radius:11px;
       padding:14px 16px;margin:0 0 14px;}
  .sub:last-child{margin-bottom:0;}
  .sub > .st{display:flex;align-items:center;gap:9px;font-size:12.5px;font-weight:600;
       color:#2A3644;margin:0 0 9px;letter-spacing:.3px;}
  .sub > .st::before{content:"";width:3px;height:12px;border-radius:2px;flex:none;
       background:linear-gradient(180deg,var(--navy2),var(--gold));}
  .sub.warn{background:#FDF7F5;border-color:#F3E2DD;}
  .sub.warn > .st::before{background:linear-gradient(180deg,#BF4030,#E5A091);}
  .sub.ok{background:#F6FBF8;border-color:#DFF0E7;}
  .sub.ok > .st::before{background:linear-gradient(180deg,#1B7F52,#84C6A6);}
  .sub > .st .cnt{margin-left:auto;font-weight:600;font-size:12px;color:var(--muted);
       font-variant-numeric:tabular-nums;letter-spacing:0;}
  .sub p{margin:6px 0;}
  table.kv td:first-child,table.kv th:first-child{white-space:nowrap;width:150px;}

  /* ---------- 顶部评级（hero） ---------- */
  .hero{margin:22px 0 16px;border-radius:17px;overflow:hidden;color:#E9EFF6;
        background:radial-gradient(125% 150% at 100% 0%,#1F527E 0%,#12304F 48%,#0B1F34 100%);
        box-shadow:0 20px 44px -24px rgba(11,31,52,.72);}
  .hero .htop{display:flex;align-items:center;justify-content:space-between;gap:14px;
        padding:13px 24px;border-bottom:1px solid rgba(255,255,255,.10);
        font-size:11.5px;letter-spacing:1.6px;color:#9DB5CC;}
  .hero .htop .rt{color:#E4C77E;letter-spacing:1.2px;}
  .hero .hmain{display:flex;align-items:center;gap:30px;padding:24px;flex-wrap:wrap;}
  .grade{font-size:54px;font-weight:800;line-height:.98;letter-spacing:5px;color:#E4C77E;
        text-shadow:0 3px 20px rgba(228,199,126,.28);}
  .grade.r,.grade.ro{color:#E4C77E;}
  .grade.y{color:#D9DEE6;text-shadow:none;}
  .grade.g{color:#7FC7A2;text-shadow:none;}
  .gsub{font-size:11.5px;color:#8FA8C0;margin-top:9px;letter-spacing:.8px;}
  .score{font-size:32px;font-weight:700;font-variant-numeric:tabular-nums;color:#fff;}
  .score small{font-size:12.5px;font-weight:400;color:#8FA8C0;letter-spacing:.5px;}
  .bar{height:7px;border-radius:99px;overflow:hidden;min-width:158px;margin-top:10px;
        background:rgba(255,255,255,.14);}
  .bar > i{display:block;height:100%;border-radius:99px;
        background:linear-gradient(90deg,#B98F45,#EBD9A8);}
  .bar.y > i{background:linear-gradient(90deg,#8E97A6,#C9D1DB);}
  .bar.g > i{background:linear-gradient(90deg,#2E8F63,#8FCBAC);}
  .hdesc{flex:1;min-width:230px;font-size:13px;color:#D3DEEA;line-height:1.9;}
  .hdesc .hnote{font-size:11.5px;color:#8FA8C0;margin-top:7px;line-height:1.8;}
  .hdims{padding:2px 24px 22px;}
  .hdt{font-size:11.5px;letter-spacing:1.5px;color:#8FA8C0;margin:4px 0 10px;}
  .dimrow{display:grid;grid-template-columns:78px 1fr 66px;gap:14px;align-items:center;
        padding:8px 0;font-size:12.5px;color:#C8D6E5;
        border-bottom:1px solid rgba(255,255,255,.07);}
  .dimrow:last-child{border-bottom:none;}
  .dimrow .bar{margin:0;}
  .dimrow .num{color:#fff;font-variant-numeric:tabular-nums;text-align:right;font-weight:600;}
  .hero .warnbox{margin:14px 24px 22px;padding:12px 16px;border-radius:10px;
        background:rgba(217,166,74,.13);border:1px solid rgba(217,166,74,.32);
        font-size:12.5px;color:#EEDDB8;line-height:1.85;}
  /* ---------- 本期点评（hero 内，v1.2.6）---------- */
  .hero .hvbox{margin:0 24px;padding:14px 17px 15px;border-radius:11px;
        background:rgba(76,201,255,.065);border:1px solid rgba(76,201,255,.22);}
  .hero .hvt{font-size:11.5px;letter-spacing:1.7px;color:#8FA8C0;margin-bottom:10px;}
  .hero .hvrate{display:flex;flex-wrap:wrap;gap:6px 18px;font-size:12.5px;
        color:#C8D6E5;line-height:1.7;}
  .hero .hvrate b{color:#fff;font-variant-numeric:tabular-nums;font-weight:700;}
  .hero .hvrate .hvup b{color:#FFC77E;}
  .hero .hvrate .hvlo b{color:#FF8A93;}
  .hero .hvbase{margin-left:auto;color:#8FA8C0;font-size:11.5px;}
  .hero .hvb{font-size:12.5px;color:#B9CBDC;line-height:1.9;margin-top:9px;}
  .hero .hvb b{color:#E4C77E;font-weight:700;}
  /* 自证校验页脚：右对齐 + 深底专用浅色（原先误用浅底 --muted，深蓝上几乎看不见） */
  .hero .hchk{margin:15px 24px 22px;text-align:right;font-size:12px;
        color:#8FA8C0;line-height:1.8;letter-spacing:.2px;}
  .hero .hchk b{color:#7FD9A8;font-variant-numeric:tabular-nums;font-weight:700;}

  /* ---------- 评级理由条目 ---------- */
  .ri{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;
      padding:7px 0;font-size:13px;border-bottom:1px dashed #EDEAE3;color:var(--ink2);}
  .ri:last-child{border-bottom:none;}
  .tag{flex:none;font-weight:600;font-size:12px;border-radius:7px;padding:1px 9px;
      font-variant-numeric:tabular-nums;letter-spacing:.2px;}
  .tag.p{color:#B3351F;background:#FBECE7;}
  .tag.n{color:#17754A;background:#E7F4ED;}
  .tag.z{color:#877F6E;background:#F2F0EA;}

  /* ---------- 表格 ---------- */
  table{width:100%;border-collapse:separate;border-spacing:0;font-size:13px;margin:0;}
  th{background:#F4F2ED;color:#4E5A69;font-weight:600;text-align:left;padding:9px 12px;
     border-bottom:1px solid var(--line);white-space:nowrap;font-size:12px;letter-spacing:.4px;}
  td{padding:9px 12px;border-bottom:1px solid var(--hair);vertical-align:top;}
  tr:last-child td{border-bottom:none;}
  tr.mark td{background:#FFF8E4;}
  table tr:first-child th:first-child{border-top-left-radius:9px;}
  table tr:first-child th:last-child{border-top-right-radius:9px;}
  table tr:last-child td:first-child{border-bottom-left-radius:9px;}
  table tr:last-child td:last-child{border-bottom-right-radius:9px;}
  .up{color:var(--up);font-weight:600;}
  .dn{color:var(--dn);font-weight:600;}
  .num{font-variant-numeric:tabular-nums;font-weight:600;}
  ul{padding-left:19px;margin:6px 0;} li{margin:5px 0;} ol{padding-left:19px;margin:6px 0;}
  li::marker{color:var(--gold);}
  .kpi{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;}
  .kpi > div{background:linear-gradient(160deg,#FFFFFF,#F5F4EF);border:1px solid var(--hair);
       border-radius:10px;padding:12px 14px;}
  .kpi .l{font-size:11.5px;color:var(--muted);letter-spacing:.4px;margin-bottom:5px;}
  .kpi .v{font-size:20px;font-weight:700;letter-spacing:-.3px;font-variant-numeric:tabular-nums;}
  .foot{margin-top:38px;padding:18px 20px;border:1px solid var(--line);border-radius:13px;
        background:#FBFAF7;font-size:11.5px;color:var(--muted);line-height:1.95;}
  .hi{background:#FBF3DE;padding:1px 5px;border-radius:4px;}
  .mark{background:#FFF8E4;}
  tr.peertr td{background:#FBF7EC;}
  .peertag{display:inline-block;margin-left:6px;padding:0 6px;border-radius:20px;
           background:#F0E4C8;color:#7A5A1B;font-size:10.5px;font-weight:600;
           vertical-align:1px;}
  .tiny{font-size:12px;color:var(--muted);line-height:1.85;}
  code{background:#F2F0EA;border-radius:4px;padding:1px 5px;font-size:12px;
       font-family:ui-monospace,SFMono-Regular,Menlo,monospace;}

  /* ---------- 打印 / 导出 PDF（v1.2.8）----------
     ⚠️ 必须显式声明 print-color-adjust:exact —— 浏览器打印**默认不输出背景色**，
        而本报告的 hero 评级卡与全部图表都是「深底浅字」。一旦背景被丢弃，
        浅色文字会落在白纸上 = 白底白字，整页不可读（且文件大小正常，看不出问题）。
     另：卡片不许跨页断开，标题行不许与正文分离。 */
  *{-webkit-print-color-adjust:exact !important;print-color-adjust:exact !important;}
  @page{size:A4;margin:11mm 9mm 13mm;}
  @media print{
    body{padding:0;background:#fff;}
    .wrap{max-width:none;}
    .card,.sub,.hero,.kpi,.ri,.warnbox,svg{break-inside:avoid;page-break-inside:avoid;}
    .cardhead,.st,.hdt{break-after:avoid;page-break-after:avoid;}
    a{color:var(--ink2);text-decoration:none;}
  }
"""


def C(title, body, tone='', badge=''):
    """章节卡：标题栏 + 内容体"""
    bd = f'<span class="badge">{badge}</span>' if badge else ''
    return (f'<div class="card {tone}"><div class="cardhead">'
            f'<span class="tt">{title}</span>{bd}</div>'
            f'<div class="cardbody">{body}</div></div>')


def SC(t, body, tone='', cnt=''):
    """卡内子卡：每个小板块一张（tone: '' / ok / warn；cnt: 右上角标注）"""
    c = f'<span class="cnt">{cnt}</span>' if cnt else ''
    return f'<div class="sub {tone}"><div class="st">{t}{c}</div>{body}</div>'


def rating_hero(sc, notes):
    """顶部评级：深色 hero 卡（大字评级 + 分数条 + 四维得分 + 数据完整度）"""
    tone = sc['tone']
    tot = sc['total']
    nr = sc.get('no_rating')
    if nr:
        tbar = ''
        scoreblock = ('<div class="score" style="font-size:30px">—</div>'
                      '<div class="hnote" style="margin-top:6px">主维度数据缺失，'
                      '打分无信息量，故不给出分数</div>')
        gsub = '不予评级'
    else:
        tbar = '' if tot >= 65 else ('y' if tot >= 45 else 'g')
        scoreblock = (f'<div class="score">{tot}<small> / 100</small></div>'
                      f'<div class="bar {tbar}"><i style="width:{min(100, tot)}%"></i></div>')
        gsub = f'{tot} / 100 分'
    dimrows = ''
    for name, got, mx, items in sc['dims']:
        w = got / mx * 100
        barcls = '' if w >= 65 else ('y' if w >= 40 else 'g')
        dimrows += (f'<div class="dimrow"><span>{name}</span>'
                    f'<span class="bar {barcls}"><i style="width:{w:.0f}%"></i></span>'
                    f'<span class="num">{got:.0f} / {mx}</span></div>')
    miss = ''
    if nr:
        miss = ('<div class="warnbox"><b>⚠️ 本次不提供评级</b>　'
                '资金面（主力资金流）与板块面（同板块横向对比）<b>均取数失败</b>，'
                '这两个维度合计 55 分无法评估。若强行把它们按"中性"计分再输出一个总分，'
                '任何一只票都会落在 50 分附近 —— <b>看似结论，实则零信息量</b>。'
                '请以本报告下方的技术面、盘口与情景分析为准。</div>')
    elif sc.get('missing'):
        miss = (f'<div class="warnbox"><b>⚠️ 数据完整度</b>　以下维度取数失败，'
                f'<b>已按中性计、未参与加减分</b>：{ "、".join(sc["missing"]) }。'
                f'该维度不提供信息量，本次评级的置信度相应下降。</div>')

    # ---- 自证校验结果（常驻，v1.2.5）----
    # 不依赖第二数据源的**内部恒等关系**检查：主力=大单+超大单、量额单位、涨停价、费用方向……
    # 它能在每次取数时都跑（交叉校验做不到——那需要两个源都可用）。
    _chk = sc.get('selfcheck') or {}
    chk = ''
    if _chk.get('fail'):
        _li = ''.join(f'<li>[{i}] {esc(m)}</li>' for _l, i, m in _chk['fail'])
        chk = (f'<div class="warnbox"><b>⛔ 自证校验未通过（{len(_chk["fail"])} 项）</b>　'
               f'以下数据存在 <b>内部矛盾</b>，相关结论不可用，请勿据此决策：'
               f'<ul style="margin:4px 0 0 16px">{_li}</ul>'
               f'<span class="tiny">自证校验只检查数据内部是否自洽、不依赖第二数据源，'
               f'本次共执行 {_chk.get("total", 0)} 项断言。</span></div>')
    elif _chk.get('warn'):
        _li = ''.join(f'<li>[{i}] {esc(m)}</li>' for _l, i, m in _chk['warn'])
        chk = (f'<div class="warnbox"><b>⚠️ 自证校验提示（{len(_chk["warn"])} 项）</b>　'
               f'可能是正常市况（如竞价期内外盘不等），也可能确有问题，需人工确认：'
               f'<ul style="margin:4px 0 0 16px">{_li}</ul></div>')
    elif _chk.get('total'):
        chk = (f'<div class="hchk">'
               f'✅ 自证校验通过 <b>{_chk["pass"]}/{_chk["total"]}</b> 项断言'
               f'（内部恒等关系自洽；不依赖第二数据源）</div>')

    # ---- 本期点评（v1.2.6）----
    # 用**四维达成率**（得分 ÷ 满分）机械生成，只做一件事：把"最低的那一维"指出来。
    # 刻意不写成主观研判长句 —— 那会和「评级理由卡」重复，也容易滑向抒情。
    verdict = ''
    if nr:
        verdict = ('<div class="hvbox"><div class="hvt">本 期 点 评</div>'
                   '<div class="hvb">资金面与板块面均取数失败，本报告<b>不提供评级</b>。'
                   '对缺失维度按基准分计入会把总分拉向"中性档"——那是默认值、不是判断，'
                   '因此本次只用下方的技术面、盘口与情景分析作答。</div></div>')
    else:
        _p = [(nm, (got / mx * 100 if mx else 0.0)) for nm, got, mx, _ in sc['dims']]
        _lo = min(_p, key=lambda x: x[1])
        _hi = max(_p, key=lambda x: x[1])
        # ⚠️ 类名必须与报告已有的工具类**不重名**：曾用 .hi/.lo，而报告里已有
        #    全局 `.hi{background:#FBF3DE}`（浅金标签），导致点评里的最高项
        #    白捡了一块浅金底色。改用 hvup/hvlo 命名空间。
        _cells = ''.join(
            '<span class="%s">%s <b>%.0f%%</b></span>'
            % ('hvup' if nm == _hi[0] else ('hvlo' if nm == _lo[0] else ''), nm, v)
            for nm, v in _p)
        verdict = (f'<div class="hvbox"><div class="hvt">本 期 点 评</div>'
                   f'<div class="hvrate">{_cells}'
                   f'<span class="hvbase">达成率 = 得分 ÷ 满分　基准线 50%</span></div>'
                   f'<div class="hvb">四维达成率最高为「{_hi[0]}」{_hi[1]:.0f}%、'
                   f'最低为「{_lo[0]}」{_lo[1]:.0f}%　→　'
                   f'<b>{_lo[0]}是本次评级的主要拖累项</b>。'
                   f'点评由四维分数机械生成，用于快速定位短板，不含人工调整，'
                   f'也不构成任何买卖指令。</div></div>')
    body = f'''<div class="htop"><span>模型投资评级 · 100 分制机械打分</span><span class="rt">券商五档口径</span></div>
<div class="hmain">
  <div>
    <div class="grade {tone}">{sc['rating']}</div>
    <div class="gsub">{gsub}</div>
  </div>
  <div style="min-width:170px">
    {scoreblock}
  </div>
  <div class="hdesc">{sc['desc']}
    <div class="hnote">口径：技术面 30 + 资金面 30 + 板块面 25 + 情绪面 15＝100。
    <b>各维度基准分＝满分×50%</b>（即无特征的普通票 = 50 分，正落在"中性"档），
    加减分对称、加分项合计恰好等于基准分，理论极值 = 100。
    映射券商投资评级五档（买入／增持／中性／减持／卖出）。<b>评级为模型输出，不构成任何买卖指令。</b></div>
  </div>
</div>
<div class="hdims"><div class="hdt">四 维 得 分</div>{dimrows}</div>
{verdict}{miss}{chk}'''
    return f'<div class="hero">{body}</div>'


def rating_reasons(sc, notes):
    """评级理由卡：每个维度一张子卡，逐条 ± 加减分"""
    subs = ''
    for name, got, mx, items in sc['dims']:
        lis = ''
        for t, dv in items:
            if dv > 0:
                tg = f'<span class="tag p">+{dv}</span>'
            elif dv < 0:
                tg = f'<span class="tag n">{dv}</span>'
            else:
                tg = '<span class="tag z">0</span>'
            lis += f'<div class="ri"><span>{t}</span>{tg}</div>'
        subs += SC(name, lis, cnt=f'{got:.0f} / {mx}')
    note = notes.get('rating_note') or ''
    if note:
        subs += SC('评级理由补充', note, cnt='人工研判')
    return C('评级理由 · 逐条加减分', subs,
             badge=f'{len(sc["dims"])} 个维度 · 含基础分与加减分')


# ------------------------------------------------------------------ HTML
def build(d, notes):
    s = d['snap']
    code, name = d['code'], s['name']
    title = notes.get('title') or '深度分析'
    date_s = datetime.date.today().strftime('%Y-%m-%d')
    # 数据时点必须来自快照自身的时间戳（14 位 YYYYMMDDHHMMSS），
    # ⚠️ 用 today() 会把"昨日收盘快照"标成今天的盘中，这是曾出现过的严重误导。
    _raw = str(s.get('time') or '')
    if len(_raw) >= 12:
        snap_date = f'{_raw[0:4]}-{_raw[4:6]}-{_raw[6:8]}'
        hm = f'{_raw[8:10]}:{_raw[10:12]}'
    else:
        snap_date, hm = date_s, _raw
    # 交易时段描述：盘前取到的快照其实是【上一交易日收盘】，不能标成"盘中"
    if snap_date != date_s:
        sess = '上一交易日收盘'
    elif hm < '09:15':
        sess = '盘前'
    elif hm < '09:30':
        sess = '集合竞价'
    elif hm <= '15:00':
        sess = '盘中'
    else:
        sess = '收盘'
    ind = d.get('ind', {})
    pb, nb = auto_judge(d)
    for x in (notes.get('bull') or []):
        pb.insert(0, x)
    for x in (notes.get('bear') or []):
        nb.insert(0, x)
    sc_rows, hit = auto_scenarios(d)
    if notes.get('scenarios'):
        sc_rows = [tuple(x) for x in notes['scenarios']]
    sc = score(d, notes)

    # ---- 抬头卡右栏的 4 项关键指标（v1.2.8）----
    # 只放「一眼定调」的四个数：评级 / 当日主力 / 相对强度（更不利者）/ 250 日位置。
    # 取不到就印「不可用」，绝不留空、也绝不用 0 冒充（见 P58）。
    _pk = pick_sectors(d)[1]
    _zl = (d.get('flow') or {}).get('zl')
    _r2c = {'买入': 'up', '增持': 'up', '中性': 'gold', '减持': 'dn', '卖出': 'dn'}
    _mk = []
    _mk.append(('模型评级',
                f'{sc["rating"]} {sc["total"]}' if sc.get('total') else sc['rating'],
                _r2c.get(sc['rating'], '')))
    if _zl is None:
        _mk.append(('当日主力', '不可用', ''))
    else:
        _mk.append(('当日主力', money(_zl), 'up' if _zl > 0 else ('dn' if _zl < 0 else '')))
    if _pk and _pk.get('rel') is not None:
        _rr = _pk['rel']
        _mk.append(('相对强度', f'{_rr:+.2f}pp', 'up' if _rr > 0 else ('dn' if _rr < 0 else '')))
    else:
        _mk.append(('相对强度', '不可用', ''))
    _p250 = ind.get('pos250')
    _mk.append(('250日位置', f'{_p250:.1f}%' if isinstance(_p250, (int, float)) else '不可用', ''))
    _mkhtml = ''.join(f'<div class="mk"><span class="k">{esc(k)}</span>'
                      f'<span class="v {c}">{esc(v)}</span></div>' for k, v, c in _mk)

    H = []
    A = H.append
    A(f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(name)} {code} {esc(title)} · {date_s}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">

<div class="mast">
  <div class="mastl">
    <h1>{esc(name)}<span class="code">{code}</span><span class="kind">{esc(title)}</span></h1>
    <div class="tagline">数据时点 <b>{snap_date} {hm}</b>（{sess}）　·　
{'沪市' if str(code).startswith('6') else '深市'}　·　
{esc(notes.get('subtitle') or '实时数据 + 板块横向对比 + 历史形态回测')}</div>
  </div>
  <div class="mastr">
    <div class="px">
      <div class="p {cls(s['chg'])}">{s['price']}</div>
      <div class="c {cls(s['chg'])}">{pct(s['chg'])}</div>
    </div>
    <div class="mkgrid">{_mkhtml}</div>
  </div>
</div>
''')

    # ===== 顶部：评级 hero + 评级理由卡 =====
    A(rating_hero(sc, notes))
    A(rating_reasons(sc, notes))

    if d.get('warn_allowed'):
        A(C('合规提示', esc(d['warn_allowed']), 'warn'))

    # ===== 一、数据速览 =====
    _pre = bool(s.get('pre_open'))
    _day = '上一交易日' if _pre else '今日'

    def _n(v, unit='', dec=2, sign=False):
        """安全数值格式化：None / nan / 非数值 → —（盘前快照的换手/量比/成交额无值）"""
        try:
            f = float(v)
        except (TypeError, ValueError):
            return '—'
        if f != f:            # nan
            return '—'
        return (f'{f:+.{dec}f}' if sign else f'{f:.{dec}f}') + unit

    ibits = []
    for k_, lbl in (('sh000001', '上证'), ('sz399001', '深成'), ('sz399006', '创业板'), ('sh000688', '科创50')):
        x = d['index'].get(k_)
        if x:
            ibits.append(f"{lbl} {x['price']:.2f} <b class=\"{cls(x['chg'])}\">{pct(x['chg'])}</b>")
    kp = f'''<div class="kpi">
  <div><div class="l">现价 / 涨幅</div><div class="v {cls(s['chg'])}">{s['price']} {pct(s['chg'])}</div></div>
  <div><div class="l">{_day}最高</div><div class="v {cls(s['high'] - s['prev'])}">{s['high']}{'（涨停）' if abs(s['high'] - s['limit_up']) < 0.011 else ''}</div></div>
  <div><div class="l">自最高回落</div><div class="v dn">{d.get('from_high', 0):.1f}%</div></div>
  <div><div class="l">量比</div><div class="v">{_n(s.get('lb'))}</div></div>
</div>'''
    if d.get('flow_ok', True):
        flowrow = (f'<tr><td>主力净额 / 净占比</td>'
                   f'<td class="num {cls(d["flow"]["zl"])}">{money(d["flow"]["zl"])} / '
                   f'{_n(d["flow"].get("net_pct")) }%</td>'
                   f'<td>&lt;10% 温和 · 30%+ 强势</td></tr>')
    else:
        flowrow = ('<tr><td>主力净额 / 净占比</td><td class="num">—</td>'
                   '<td><b>未取到</b>（盘前/限流），评级中该维度<b>按中性计、不计负分</b></td></tr>')
    _blank = '<td class="num">—</td><td>盘前快照无此项（开盘后才有）</td>'
    tbl = f'''<table class="kv">
<tr><th>项目</th><th>数值</th><th>判读</th></tr>
<tr><td>开盘 / 最高 / 最低</td><td>{s['open']} / <b>{s['high']}</b> / {s['low']}</td><td>{'低开' if s['open'] < s['prev'] else '高开'} {(s['open'] / s['prev'] - 1) * 100:+.1f}%{('，' + d['touch_at'][:2] + ':' + d['touch_at'][2:] + ' 触板') if d.get('touch_at') else ''}</td></tr>
<tr><td>前收盘 / 涨停价</td><td>{s['prev']} / {s['limit_up']}</td><td>{'已封死' if s.get('sealed') else ('未封板' if not _pre else '上一交易日未封板')}</td></tr>
<tr><td>成交量 / 成交额</td><td>{s['vol'] / 1e4:.1f} 万手 / {_n((s.get('amount') or 0) / 1e4, ' 亿', 2) if not _pre else '—'}</td><td>{('盘中为当日<b>累计</b>量，与 20 日<b>全天</b>均量口径不可比，仅供参考（现 <b>' + f"{d.get('vr20', 0):.2f}" + '</b> 倍）') if d.get('intraday') else ('为 20 日均量的 <b>' + f"{d.get('vr20', 0):.2f}" + '</b> 倍')}</td></tr>
<tr><td>换手率</td><td>{_n(s.get('hs'), '%')}</td><td>{'—' if _pre else ('活跃' if s['hs'] > 3 else '一般')}</td></tr>
<tr><td>量比</td><td><b>{_n(s.get('lb'))}</b></td><td>{'—' if _pre else ('量能未有效放大' if s['lb'] < 2 else '量能有效')}</td></tr>
<tr><td>日内均价 VWAP</td><td><b>{_n(d.get('vwap'), '', 3)}</b></td><td>{'盘前无分时数据，不可用' if _pre else f"现价 {s['price']} {'低于' if s['price'] < d.get('vwap', 0) else '高于'}均价"}</td></tr>
<tr><td>主动买卖</td>{_blank if _pre else f"<td>外盘 {s['outer'] / 1e4:.1f} 万 {'&gt;' if s['outer'] > s['inner'] else '&lt;'} 内盘 {s['inner'] / 1e4:.1f} 万</td><td>主动买占 {(s['outer'] / max(1, s['outer'] + s['inner'])) * 100:.1f}%</td>"}</tr>
<tr><td>买1 / 卖1 挂单</td>{_blank if _pre else f"<td class='num'>{s['bids'][0][1] if s['bids'] else 0} / {s['asks'][0][1] if s['asks'] else 0} 手</td><td>{'近端承接薄' if s['bids'] and s['bids'][0][1] < 500 else '近端承接正常'}</td>"}</tr>
{flowrow}
<tr><td>大盘（{hm}）</td><td>{'　|　'.join(ibits)}</td><td>—</td></tr>
{d.get('anon') and '<tr><td>资金占用</td><td>—（本报告为<b>脱敏版</b>，不含账户资金与持仓口径）</td><td>—</td></tr>' or f"<tr><td>资金占用</td><td><b>{n100(s['price'], d['cash'])} 股 = {n100(s['price'], d['cash']) * s['price']:.0f} 元</b>（config 资金 {d['cash']:.0f}）</td><td>双边费用约 {n100(s['price'], d['cash']) * s['price'] * d['fee']:.0f} 元，需涨 {d['fee'] * 100:.2f}% 才回本</td></tr>"}
</table>'''
    A(C('一、数据速览',
        SC('核心盘口', kp, cnt=f'{hm} 快照')
        + SC('明细（价格 / 量能 / 盘口 / 资金 / 大盘 / 资金占用）', tbl),
        badge=f"{hm} {sess}"))

    # ===== 分时形态 =====
    if d.get('minutes'):
        pts = d['minutes']
        li = (f'<li>开盘 {s["open"]} → 日内最低 {d["m_low"][1]}（{d["m_low"][0][:2]}:{d["m_low"][0][2:]}）'
              f'→ 日内最高 <b>{d["m_high"][1]}</b>（{d["m_high"][0][:2]}:{d["m_high"][0][2:]}）</li>')
        if d.get('touch_at'):
            li += (f'<li>触板时间 <b>{d["touch_at"][:2]}:{d["touch_at"][2:]}</b>，'
                   f'{"收盘封死" if s.get("sealed") else "未能封住（炸板）"}</li>')
        else:
            li += (f'<li>今日<b>未触及涨停</b>（最高 {s["high"]}，'
                   f'距涨停 {(s["high"] / s["limit_up"] - 1) * 100:.2f}%）</li>')
        li += (f'<li>自日内最高回落 <b>{d["from_high"]:.1f}%</b>；上影线 <b>{d["upper_shadow"]:.1f}%</b>；'
               f'日内 VWAP {d["vwap"]:.3f}</li>')
        li += ('<li>尾盘 5 点：' + ' '.join(f"{p[0][:2]}:{p[0][2:]}={p[1]}" for p in pts[-5:]) + '</li>')
        A(C('分时形态', SC('日内关键节点', f'<ul>{li}</ul>'), badge='腾讯分时'))

    # ===== 二、板块横向对比 =====
    if d['sectors']:
        def _fmt(v, unit, dec=2):
            """板块表数值格式化：None 显示 —（盘前/限流时板块主力净额取不到）。"""
            return '—' if v is None else f'{v:+.{dec}f}{unit}'

        def _row(x):
            tag = ' class=peertr' if x.get('peer') else ''
            b = x.get('basis')
            pt = (f' <span class=peertag>{b}基准</span>' if b else '')
            return (f"<tr{tag}><td><b>{esc(x['name'])}</b>"
                    f" <span class='tiny'>{x['bk']}</span>{pt}</td>"
                    f"<td>{x['n']}</td><td>{x['up']}</td>"
                    f"<td class='num {cls(x['zl'])}'>{_fmt(x['zl'], ' 亿')}</td>"
                    f"<td class='num'>{_fmt(x['med'], '%')}</td>"
                    f"<td class='num {cls(s['chg'])}'>{s['chg']:+.2f}%</td>"
                    f"<td class='num {cls(x.get('rel') or 0)}'>{_fmt(x.get('rel'), 'pp')}</td>"
                    f"<td>{x.get('rank') or '—'}/{x['n']}</td></tr>")

        rows = ''.join(_row(x) for x in d['sectors'])
        best, peer = pick_sectors(d)
        concl = ''
        if best:
            ref = peer or best
            rel = ref['rel']
            rb = ref.get('basis')
            rlab = f'{rb}基准' if rb else '基准板块'
            reltxt = (f'<span class="hi">跑输 {abs(rel):.2f} 个百分点</span>' if rel < 0
                      else f'领先 {rel:.2f} 个百分点')
            bases = [x for x in d['sectors'] if x.get('basis') and x.get('rel') is not None]
            blist = '、'.join(f"{x['basis']}基准 {esc(x['name'])}（{x['n']} 只）"
                              f"{x['rel']:+.2f}pp" for x in bases) or '—'
            brel = best.get('rel') if best.get('rel') is not None else 0
            concl = SC(
                '相对强度（行业／题材双基准，取更不利者）',
                f"<p style='margin:2px 0'>{esc(name)} 报 <b>{s['chg']:+.2f}%</b>；"
                f"两个基准分别为 {blist}。主判据取<b>更不利</b>的 "
                f"<b>{esc(ref['name'])}</b>（{rlab}，{ref['n']} 只成分），"
                f"全量中位数 <b>{ref['med']:+.2f}%</b>，{reltxt}，"
                f"绝对排名 <b>{ref.get('rank')}/{ref['n']}</b>。</p>"
                + ("<p class='tiny'>⚠️ <b>基准分歧</b>：两个基准方向相反，本次一律按更不利者计分"
                   " —— 绝不允许挑对自己最有利的那个板块当分母（P35）。</p>"
                   if d.get('basis_divergence') else '')
                + (f"<p class='tiny'>⚠️ 基准<b>不参与挑选</b>：行业取东财口径（每票恰一个），"
                   f"题材取所属概念里今日主力净额最大者，二者都由规则固定，不由分析者挑。"
                   f"资金最热的相关板块为 <b>{esc(best['name'])}</b>（{best['n']} 只，"
                   f"{brel:+.2f}pp），仅作方向参考，不作为相对强度分母。</p>"))
        lu = []
        for x in d['sectors']:
            for c, nm, ch in x['limit_up']:
                if (str(c), str(nm)) not in [(z[0], z[1]) for z in lu]:
                    lu.append((str(c), str(nm), ch, x['name']))
        sub = ''
        if lu:
            tb = ('<table><tr><th>代码</th><th>名称</th><th>涨幅</th><th>所属板块</th></tr>'
                  + ''.join(f"<tr><td>{c}</td><td>{esc(nm)}</td><td class='up'>{ch:+.2f}%</td>"
                            f"<td>{esc(bk)}</td></tr>" for c, nm, ch, bk in lu[:15])
                  + '</table>')
            own = any(c == str(code) for c, _, _, _ in lu)
            sub = SC('板块内涨停／准涨停名单（资金在接力什么类型）',
                     tb + f"<p style='margin:6px 0 0'><b>{esc(name)} "
                          f"{'在' if own else '不在'}</b>该名单内 → 资金接力的类型"
                          f"{'包含' if own else '不包含'}它。</p>")
        body = (SC('板块相对强度总览',
                   f'<table><tr><th>板块</th><th>成分数</th><th>红盘数</th><th>板块主力</th>'
                   f'<th>涨幅中位数（全量）</th><th>个股涨幅</th><th>相对强度</th><th>个股排名</th></tr>'
                   f'{rows}</table>'
                   f'<p class="tiny">⚠️ 中位数按<b>板块全量成分</b>计算。若只取涨幅前 100 名，'
                   f'其"中位数"≈全市场 90 分位，会系统性把中游票误判为弱势票。</p>',
                   cnt=f'共 {len(d["sectors"])} 个相关板块')
                + concl + sub)
        # ★ G9：主营构成校验 —— 板块对比之前先确认"它靠什么赚钱"
        #   否则会拿一个与主业无关的板块当基准（标的甲主营 81% 教育，却拿"通信技术"比）。
        mb = d.get('mainbiz') or {}
        mprod = mb.get('by_product') or []
        if mprod:
            brows = ''.join(f"<tr><td>{esc(nm)}</td><td class='num'>"
                            f"{(f'{r:.2f}%' if r is not None else '—')}</td></tr>"
                            for nm, r in mprod[:5])
            bnames = '、'.join(f"{x['basis']}基准 <b>{esc(x['name'])}</b>"
                               for x in d['sectors'] if x.get('basis')) or '—'
            top = mprod[0]
            same = any((x['name'] in top[0]) or (top[0] in x['name'])
                       or (x['name'][:2] in top[0])
                       for x in d['sectors'] if x.get('basis'))
            warn = ('' if (same or top[1] is None or top[1] < 50) else
                    f"<p class='tiny'>⚠️ <b>题材与主业可能不同源</b>：第 1 大主营是 "
                    f"<b>{esc(top[0])}</b>（{top[1]:.2f}%），而本次板块基准为 {bnames} —— "
                    f"股价驱动可能来自<b>题材标签</b>而非主营，业绩支撑与估值锚须分开看（G9）。</p>")
            body += SC(
                f"主营构成（按产品 · {mb.get('date') or '最新报告期'}）",
                f"<table><tr><th>主营项目</th><th>收入占比</th></tr>{brows}</table>"
                f"<p class='tiny'>本次板块基准：{bnames}。"
                f"主营构成＝业绩与估值锚；板块基准＝资金流向。二者不同源时，涨的是情绪不是业绩。</p>"
                + warn)
        # 相对强度对比图（v1.2.5）：把"个股 vs 各所属板块中位"一次画出来。
        # 视觉重点在**最下面那条**——因为主判据取的是「更不利者」，不是最好看的那个。
        _items = [(x['name'], x['rel'], 'n=%d%s' % (x.get('n') or 0,
                                                    '·基准' if x.get('basis') else ''))
                  for x in d['sectors'] if x.get('rel') is not None]
        if _items:
            _items.sort(key=lambda t: t[1])
            body += SC('相对强度对比（个股涨幅 − 板块中位）',
                       charts.hbar_svg(_items, title=''),
                       cnt='自下而上：主判据取最不利者')
        tone = ('warn' if (peer and peer['rel'] is not None and peer['rel'] < 0)
                else ('ok' if peer else ''))
        A(C('二、板块横向对比', body, tone, badge='铁律第一条'))
    else:
        A(C('二、板块横向对比', SC(
            '⚠️ 本次板块行情不可用 —— 不是"板块普跌"',
            '<p>当前时段行情源<b>不提供个股涨跌幅</b>：腾讯/新浪快照在开盘前只返回'
            '"今日未开盘"空壳（开=0、量=0、涨幅恒为 0），东财 clist 的 f2/f3/f62 '
            '返回占位符 <code>-</code>。因此<b>板块内相对强度（成分涨幅中位数）本次无法计算</b>，'
            '板块面已按中性计、未加减分。</p>'
            '<p class="tiny">🔴 关键提醒：若无此校验，程序会输出"板块红盘率 0%、'
            '中位数 +0.00%、普跌 −3 分"——那是<b>取数失败的假象</b>，会真实地把评级打低。'
            '开盘后（09:30+）重跑即可拿到真实板块数据。</p>',
            tone='warn', cnt='数据不可用'), 'gray'))

    # ===== 三、多空研判 =====
    body = (SC('✅ 支持的一方', '<ul>' + ''.join(f'<li>{x}</li>' for x in pb) + '</ul>',
               tone='ok', cnt=f'{len(pb)} 条')
            + SC('⚠️ 反对的一方', '<ul>' + ''.join(f'<li>{x}</li>' for x in nb) + '</ul>',
                 tone='warn', cnt=f'{len(nb)} 条'))
    if d.get('flow_days'):
        t = ('<table><tr><th>日期</th><th>主力</th><th>超大单</th><th>大单</th><th>中单</th>'
             '<th>小单</th></tr>')
        for tm, zl, xs, zs, dsz, xos in d['flow_days']:
            t += (f"<tr><td>{tm[-5:]}</td><td class='num {cls(zl)}'>{zl / 1e4:+.0f}</td>"
                  f"<td class='num {cls(xs)}'>{xs / 1e4:+.0f}</td>"
                  f"<td class='num {cls(zs)}'>{zs / 1e4:+.0f}</td>"
                  f"<td class='num {cls(dsz)}'>{dsz / 1e4:+.0f}</td>"
                  f"<td class='num {cls(xos)}'>{xos / 1e4:+.0f}</td></tr>")
        t += '</table>'
        signs = ' '.join(('+' if _fl(x[1]) > 0 else '−') for x in d['flow_days'])
        neg = sum(1 for x in d['flow_days'] if _fl(x[1]) < 0)
        t += (f'<p class="tiny">近 {len(d["flow_days"])} 日主力符号 <b>{signs}</b>，'
              f'负值 {neg}/{len(d["flow_days"])} 天。单位：万元。</p>')
        body += SC('资金结构（日线级，近 12 日）', t)
    else:
        body += SC('资金结构（日线级，近 12 日）',
                   '<p class="tiny">日线级资金流接口（<code>fflow/daykline</code>）当前'
                   '<b>限流不可用</b>（实测三主机均返回 <code>rc:100</code> 或空）。'
                   '本节的"近 N 日资金连续性"因此缺失，评级中该项按 0 分计（不计负分）。'
                   '盘中实时主力净额（<code>ulist</code>）不受影响，仍见上文。</p>')
    # ===== 资金流向（图）=====
    _fd = d.get('flow_daily') or []
    if _fd:
        _ftot = sum(v for _dd, v in _fd)
        A(C('资金流向',
            SC('近 14 个交易日主力净额',
               charts.flow_bars_svg(_fd),
               cnt='红 = 净流入　绿 = 净流出　合计 %+.0f 万' % _ftot),
            badge='东财日线口径', tone='warn' if _ftot < 0 else 'ok'))

    A(C('三、多空研判', body, tone='warn' if len(nb) > len(pb) else 'ok'))

    # ===== 四、技术分析 =====
    if ind:
        b = ind['boll']
        kq = ind.get('kdj', (float('nan'),) * 3)
        t1 = f'''<table>
<tr><th>指标</th><th>读数</th><th>判读</th></tr>
<tr><td>MA5 / MA10 / MA20 / MA60</td><td>{ind['ma5']:.3f} / {ind['ma10']:.3f} / {ind['ma20']:.3f} / {ind['ma60']:.3f}</td><td class="{'up' if ind['bull'] else 'dn'}">{'完整多头排列' if ind['bull'] else '非多头排列'}</td></tr>
<tr><td><b>BOLL(20,2)</b></td><td>上 {b['up']:.3f} / 中 {b['mid']:.3f} / 下 {b['low']:.3f}</td><td class="{'dn' if b['pctb'] >= 95 else ''}">现价位于通道 <b>{b['pctb']:.1f}% 分位</b></td></tr>
</table>'''
        t2 = f'''<table>
<tr><th>指标</th><th>读数</th><th>判读</th></tr>
<tr><td>MACD(12,26,9)</td><td>DIF {ind['macd']['dif']:+.4f} / DEA {ind['macd']['dea']:+.4f} / 柱 {ind['macd']['macd']:+.4f}</td><td class="{'up' if ind['macd']['dif'] > ind['macd']['dea'] else 'dn'}">{'零轴上方金叉' if ind['macd']['dif'] > ind['macd']['dea'] and ind['macd']['dif'] > 0 else ('金叉' if ind['macd']['dif'] > ind['macd']['dea'] else '死叉')}</td></tr>
<tr><td><b>RSI(6/14/24)</b></td><td>{ind['rsi6']:.2f} / <b>{ind['rsi14']:.2f}</b> / {ind['rsi24']:.2f}</td><td class="{'dn' if ind['rsi14'] > 70 else ''}">{'超买(&gt;70)' if ind['rsi14'] > 70 else ('超卖(&lt;30)' if ind['rsi14'] < 30 else '中位')}</td></tr>
<tr><td><b>CCI(20)</b></td><td><b>{ind['cci20']:.2f}</b></td><td class="{'dn' if ind['cci20'] > 100 else ''}">{'超买(&gt;100)' if ind['cci20'] > 100 else '中性'}</td></tr>
<tr><td>KDJ(9,3,3)</td><td>K {kq[0]:.1f} / D {kq[1]:.1f} / J {kq[2]:.1f}</td><td>{'K 上穿 D' if kq[0] > kq[1] else 'K 下穿 D'}</td></tr>
<tr><td>WR(14)</td><td>{ind['wr14']:.1f}</td><td>{'超买区' if ind['wr14'] > -20 else ('超卖区' if ind['wr14'] < -80 else '中性')}</td></tr>
<tr><td>MOM(10)</td><td>{ind['mom10']:+.2f}%</td><td>{'动能强，注意透支' if ind['mom10'] > 10 else '中性'}</td></tr>
</table>'''
        t3 = f'''<table>
<tr><th>指标</th><th>读数</th><th>判读</th></tr>
<tr><td>量能</td><td>{d.get('vr20', 0):.2f} 倍 20 日均量</td><td>{'放量' if d.get('vr20', 0) > 1.5 else '平量'}</td></tr>
<tr><td>60 日位置</td><td><b>{ind['pos60']:.0f}%</b></td><td>{'短期偏高' if ind['pos60'] > 70 else '中低位'}</td></tr>
<tr><td>250 日位置</td><td>{ind['pos250']:.0f}%</td><td>{'长期偏高' if ind['pos250'] > 70 else '长期仍低'}</td></tr>
</table>'''
        # K 线图（v1.2.5）：报告里最直观的一张图，放技术分析卡首位。
        # ⚠️ 数据键是 d['k']（gather 里已剔除当日未完成根），不是 d['kl']。
        _kl = d.get('k') or []
        _chk_k = charts.candlestick_svg(_kl, n=55,
                                        title='近 55 日 K 线 · MA5/10/20 · 成交量（红涨绿跌）'
                                        ) if len(_kl) >= 20 else ''
        A(C('四、技术分析',
            (_chk_k and SC('K 线走势与量能', _chk_k, cnt='近 60 个交易日') or '')
            + SC('趋势与通道（均线 / BOLL）', t1, cnt='平台参数面板口径')
            + SC('动量与超买超卖（MACD / RSI / CCI / KDJ / WR / MOM）', t2)
            + SC('量能与位置', t3),
            badge='全指标 · 日线级'))

    # ===== 五、回测 =====
    if d.get('bt_err'):
        A(C('五、历史回测', f'<p><b>回测未能执行</b>：{esc(d["bt_err"])}'
                            f'（K 线源不可用时自动跳过，不影响其余章节）</p>', 'warn'))
    elif d.get('bt'):
        kk = d['bt_k']
        t = (f'<p>样本：本股 {len(kk)} 根日 K（{kk[0]["d"]} ~ {kk[-1]["d"]}）。'
             f'口径：信号日<b>收盘价买入</b>，次日卖出；不含费用（双边约 0.15~0.2%）。</p>'
             '<table><tr><th>形态</th><th>N</th><th>收盘卖胜率</th><th>均值</th>'
             '<th>开盘卖胜率</th><th>冲 +2% 概率</th><th>次日最高均</th><th>次日最低均</th>'
             '<th>破 −3% 概率</th></tr>')
        hl = hit['name'] if hit else None
        for r in d['bt']:
            mk = ' class="mark"' if hl and r['name'] == hl else ''
            wn = ' ⚠️' if r['n'] < 10 else ''
            t += (f"<tr{mk}><td>{esc(r['name'])}{wn}</td><td>{r['n']}</td>"
                  f"<td class='up'>{r['win_close']:.0f}%</td>"
                  f"<td class='{cls(r['avg_close'])}'>{r['avg_close']:+.2f}%</td>"
                  f"<td>{r['win_open']:.0f}%</td><td>{r['p_2']:.0f}%</td>"
                  f"<td class='{cls(r['avg_high'])}'>{r['avg_high']:+.2f}%</td>"
                  f"<td class='{cls(r['avg_low'])}'>{r['avg_low']:+.2f}%</td>"
                  f"<td>{r['p_break']:.0f}%</td></tr>")
        bb = d.get('bt_base')
        if bb:
            t += (f"<tr><td><b>基准（任意日）</b></td><td>{bb['n']}</td>"
                  f"<td>{bb['win_close']:.0f}%</td><td>{bb['avg_close']:+.2f}%</td>"
                  f"<td>{bb['win_open']:.0f}%</td><td>{bb['p_2']:.0f}%</td>"
                  f"<td>{bb['avg_high']:+.2f}%</td><td>{bb['avg_low']:+.2f}%</td>"
                  f"<td>{bb['p_break']:.0f}%</td></tr>")
        t += '</table>'
        if hl:
            t += f'<p class="tiny">黄底行 = 与今日形态最接近的样本。N &lt; 10 标 ⚠️，<b>样本不足不可外推</b>。</p>'
        cases = ''
        if hit:
            cases = SC(f'最接近今日参数的历史案例（{esc(hit["name"])}）',
                        '<table>'
                        '<tr><th>次日</th><th>当日涨</th><th>上影</th><th>量比</th>'
                        '<th>次日开盘</th><th>次日收盘</th><th>次日最高</th><th>次日最低</th></tr>'
                        + ''.join(
                            f"<tr><td>{c['d']}</td><td>{c['f']['chg']:+.2f}%</td>"
                            f"<td>{c['f']['upper']:.1f}%</td><td>{c['f']['vr']:.2f}x</td>"
                            f"<td class='{cls(c['open'])}'>{c['open']:+.2f}%</td>"
                            f"<td class='{cls(c['close'])}'>{c['close']:+.2f}%</td>"
                            f"<td class='{cls(c['high'])}'>{c['high']:+.2f}%</td>"
                            f"<td class='{cls(c['low'])}'>{c['low']:+.2f}%</td></tr>"
                            for c in hit['cases'][-8:])
                        + '</table>',
                        cnt=f'样本 N={hit["n"]}')
        # 回测可视化（v1.2.5）—— 视觉重点放在**离散度**上。
        # 均值 +0.5% 可能是"全部挤在 0 附近"，也可能是"一半 +5% 一半 −4%"；
        # 对 T+1 隔日模式，后者才是真实风险敞口。所以逐笔画出高低范围与开→收。
        vis = ''
        if hit and hit.get('cases'):
            _cs = [(c['d'][5:], c['open'], c['close'], c['high'], c['low'])
                   for c in hit['cases'][-8:]]
            vis += SC('次日表现：逐笔高低范围（看离散度，不只看均值）',
                      charts.backtest_visual_svg(_cs),
                      cnt='N=%d' % hit['n'])
            vis += SC('次日收盘收益分布',
                      charts.hist_svg([c['close'] for c in hit['cases']]),
                      cnt='分布越集中越可预期')
        A(C('五、历史回测', SC(f'形态统计（{len(d["bt"])} 组内置形态）', t) + cases + vis,
            badge='T+1 隔日模式'))

    # ===== 六、情景概率 =====
    body = ''
    if notes.get('verdict'):
        body += SC('结论', notes['verdict'], cnt='人工研判')
    if sc_rows:
        t = '<table><tr><th>情景</th><th>概率</th><th>依据</th></tr>'
        for a, b_, c_ in sc_rows:
            t += (f"<tr><td><b>{a}</b></td><td class='up'><b>{b_}</b></td><td>{c_}</td></tr>")
        t += '</table>'
        t += ('<p class="tiny">概率来自历史形态统计；样本量小的形态须再下调一档。'
              '当前情绪环境见下节。</p>')
        body += SC('情景 × 概率', t, cnt='高概率已标红')
    if notes.get('watch'):
        body += SC('盘中／尾盘需要盯的数',
                   '<ol>' + ''.join(f'<li>{x}</li>' for x in notes['watch']) + '</ol>',
                   cnt=f'{len(notes["watch"])} 项')
    if body:
        A(C('六、策略参考', body, badge='客观推演 · 非买卖指令'))

    # ===== 七、情绪温度 =====
    body = ''
    if d.get('mood'):
        mv = [v for v in d['mood'].values() if v.get('zl_yi') is not None]
        t = ('<table><tr><th>情绪指标</th><th>板指涨跌</th><th>主力净额</th></tr>')
        for nm, v in d['mood'].items():
            zl_ = v.get('zl_yi')
            t += (f"<tr><td>{esc(nm)}</td><td class='{cls(v.get('chg'))}'>"
                  f"{(pct(v['chg']) if v.get('chg') is not None else '—')}</td>"
                  f"<td class='num {cls(zl_)}'>"
                  f"{(f'{zl_:+.2f} 亿' if zl_ is not None else '— 未取到')}</td></tr>")
        t += '</table>'
        if not mv:
            t += ('<p class="tiny">⚠️ 板块级主力净额未取到（盘前/限流），'
                  '"价强钱撤"判定<b>不可用</b>，情绪面已按中性计、未加减分。</p>')
            body += SC('昨日涨停系资金', t, cnt='数据不可用')
        else:
            negs = [v for v in mv if v['zl_yi'] < 0]
            if negs:
                t += ('<p><b>"价强钱撤"</b>：板指在涨但主力净额为负——抬价的是中小资金，'
                      '大资金在借强势离场。此环境对接力型交易不友好。</p>')
            body += SC('昨日涨停系资金', t, tone=('warn' if negs else 'ok'),
                       cnt=('价强钱撤' if negs else '资金净流入'))
    if d.get('ladder_ok') is False:
        body += SC('连板梯队（真实扫描 streak）',
                   '<p>⚠️ 全市场涨幅字段未取到（盘前/限流），连板梯队<b>本次不可用</b>，'
                   '情绪面已按中性计、未据此加减分。<b>不要把它读成"情绪偏冷 0 只"</b>。</p>',
                   cnt='数据不可用')
    elif d.get('ladder'):
        n2 = sum(1 for x in d['ladder'] if x[2] >= 2)
        top = max((x[2] for x in d['ladder']), default=0)
        fail = d.get('ladder_failed', 0)
        s_ = (f'<p>今日 2 板及以上 <b>{n2}</b> 只，最高连板 <b>{top}</b> 板：'
              + '、'.join(f'{esc(nm)} {st}板' for _, nm, st in d['ladder'][:8])
              + (f'　<span class="tiny">（扫描涨幅前 80 名涨停股，{fail} 只 K 线取数失败已跳过，'
                 f'实际梯队可能略多）</span>' if fail else '')
              + '</p>')
        s_ += ('<p>' + ('🔴 情绪偏冷（≤3 只）→ 打板胜率塌方' if n2 <= 3 else
                       ('🟢 情绪活跃（≥10 只）' if n2 >= 10 else '⚪ 情绪中性')) + '</p>')
        body += SC('连板梯队（真实扫描 streak）', s_,
                   tone=('warn' if n2 <= 3 else ('ok' if n2 >= 10 else '')),
                   cnt=f'2板以上 {n2} 只')
    if body:
        A(C('七、市场环境：情绪温度', body))

    # ===== 八、风险提示 =====
    ul = []
    ul.append(f'所有数据为 <b>{snap_date} {hm}（{sess}）</b>快照，'
              f'{"盘前取到的行情即上一交易日收盘值，" if sess == "上一交易日收盘" else ""}'
              f'价格、量比、主力净额、均线、技术指标全部会变；'
              f'均线与指标按"实时价拼入历史序列"计算，<b>必须以收盘数据复核</b>。')
    if d.get('bt') and hit:
        ul.append(f'回测样本量有限（本次主形态 N={hit["n"]}）'
                  + ('' if hit['n'] >= 10 else '，且 N&lt;10 属<b>样本不足、不可外推</b>')
                  + '；样本区间含多段强势市况，<b>换环境后适用性下降</b>，统计结果不构成对未来的保证。')
    ul.append('资金流按成交单大小推断主体，存在拆单干扰；单日数据噪音大，须以 2~3 日连续性验证。')
    ul.append('板块内排名靠后的跟风票，板块退潮时跌幅常大于龙头；若最猛的主线正主在 688/300，'
              '无交易权限则只能吃主板影子票，联动强度天然打折。')
    if d.get('anon'):
        ul.append(f'T+1 隔日模式的双边交易成本约 <b>{d["fee"] * 100:.2f}%</b>（佣金 / 印花税 / 过户费），'
                  f'<b>日内 ±1% 的波动不等于收益</b>；小资金还要额外承担每边最低佣金的固定摩擦。')
        ul.append('超短模式的 alpha 在风控不在选股：止损位须在买入前算好并机械执行，'
                  '不因"形态还没坏"而放宽。')
    else:
        ul.append(f'按 config 资金 {d["cash"]:.0f} 元，双边费用约 '
                  f'{n100(s["price"], d["cash"]) * s["price"] * d["fee"]:.0f} 元'
                  f'（{d["fee"] * 100:.2f}%），日内 ±1% 的波动不等于收益。')
        ul.append(f'你的止损纪律为 <b>{d["stop"]:.1f}%</b>，兑现目标 {d["target"]:.1f}%；'
                  f'触发即执行，不因"形态还没坏"而放宽——超短模式的 alpha 在风控不在选股。')
    ul.append('<b>顶部评级为 100 分制机械打分结果，非券商研报、非投资建议、不构成任何买卖指令</b>；'
              '据此操作风险自担。')
    if (not d.get('anon')) and (not allowed(code)):
        pre = '、'.join(str(x) for x in (cfg.get('allowed_prefixes') or []))
        ul.append(f'🔴 <b>该标的不在 config 声明的可交易板块内</b>'
                  f'（allowed_prefixes = {pre}），无权限则不可操作，本节仅作分析参考。')
    ul.append('<b>以上为客观数据推演与主观概率估计，仅供参考，据此操作风险自担。</b>')
    A(C('八、风险提示',
        SC('风险清单（必读）', '<ul>' + ''.join(f'<li>{x}</li>' for x in ul) + '</ul>',
           tone='warn', cnt=f'{len(ul)} 条'),
        'warn'))

    if notes.get('oneline'):
        A(C('一句话', SC('结论摘要', notes['oneline'], cnt='人工研判')))

    A(f'''<div class="foot">
数据来源：腾讯行情快照（qt.gtimg.cn）、腾讯分时（ifzq.gtimg.cn）、东财 push2/push2delay（clist / slist / ulist / fflow）、
腾讯日 K（fqkline qfq → hfq → kline → 新浪，逐级降级）。K 线主源={d.get('k_src')}。<br>
⚠️ 若腾讯 <code>fqkline?qfq</code> 返回 501（实测会发生），K 线会自动降级为<b>不复权</b>源，
区间内有分红送转时涨停价判定存在误差，回测数值约有 ±0.1pp 级偏差。<br>
回测口径：信号日收盘价买入 → 次日卖出；主板涨停价 = round(昨日收盘 × 1.10, 2)；
"触及"判定 high ≥ 涨停价 − 0.011，"封板"判定 close ≥ 涨停价 − 0.001。<br>
评级口径：技术 30 + 资金 30 + 板块 25 + 情绪 15 = 100 分，<b>各维基准分 = 满分×50%</b>
（普通票 = 50 分 =「中性」档中心），加分项合计 = 基准分，理论极值 100，不靠上限截断凑分。<br>
映射券商五档：≥80 买入 / 65~79 增持 / 45~64 中性 / 30~44 减持 / &lt;30 卖出。<br>
报告生成时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}　|　本报告为个人研究记录，不构成投资建议。
</div>

</div>
</body>
</html>''')
    return '\n'.join(H)


# ------------------------------------------------------------------ PDF 导出（README P2-7）
def to_pdf(html_path):
    """用本机 Chrome headless 把 HTML 导成 PDF（便于存档与分享）。

    实测可用开关：--headless=new --print-to-pdf --no-pdf-header-footer
    ⚠️ 不要用 --screenshot 代替：长报告会被截断成首屏。
    """
    import subprocess
    cands = [
        r'C:\Program Files\Google\Chrome\Application\chrome.exe',
        r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
        os.path.expanduser(r'~\AppData\Local\Google\Chrome\Application\chrome.exe'),
        '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
        'google-chrome', 'chromium', 'chromium-browser',
    ]
    exe = next((c for c in cands if os.path.sep in c and os.path.exists(c)), None) or \
          next((c for c in cands if os.path.sep not in c), None)
    if not exe:
        print('      ⚠️ 未找到 Chrome，跳过 PDF（可手动打开 HTML 后"打印为 PDF"）')
        return None
    # ⚠️ 必须用**绝对路径**：Chrome 的 --print-to-pdf 不认相对路径（会写到它自己的工作目录），
    #    实测相对路径下 os.path.exists 恒为 False，表现为"Chrome 未生成有效 PDF"。
    #    另注：中文路径与含空格路径均可用（已实测），只有"相对路径"会失败。
    pdf = os.path.abspath(os.path.splitext(html_path)[0] + '.pdf')
    url = 'file:///' + os.path.abspath(html_path).replace('\\', '/')
    base = [exe, '--headless=new', '--disable-gpu', '--no-sandbox', '--virtual-time-budget=5000']
    out = ''
    # 先带 --no-pdf-header-footer；失败则退回不带（老版本 Chrome 不认该开关）
    for extra in (['--no-pdf-header-footer'], []):
        cmd = base + extra + ['--print-to-pdf=' + pdf, url]
        try:
            p = subprocess.run(cmd, timeout=180, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            out = (p.stdout or b'').decode('utf-8', 'replace').strip()
        except Exception as e:
            out = 'subprocess 异常：%s' % e
        if os.path.exists(pdf) and os.path.getsize(pdf) > 1024:
            print('      已导出 PDF: %s (%.1f KB)' % (pdf, os.path.getsize(pdf) / 1024))
            return pdf
    print('      ⚠️ Chrome 未生成有效 PDF。Chrome 输出：%s' % (out[:200] or '(空)'))
    print('         排查顺序：① 输出目录写权限 ② Chrome 版本是否支持 --headless=new ③ 是否被已运行的 Chrome 会话拦截')
    return None


# ------------------------------------------------------------------ 入口
def main():
    ap = argparse.ArgumentParser(description='一键生成卡片式 HTML 深度分析报告（含券商式评级）')
    ap.add_argument('code', help='6 位股票代码')
    ap.add_argument('--title', default='深度分析', help='报告标题（如 午盘深度分析）')
    ap.add_argument('--out', default=None, help='输出路径')
    ap.add_argument('--cash', type=float, default=None,
                    help='可用资金；省略时取 config.json 的 cash')
    ap.add_argument('--sector-top', type=int, default=4, help='展示几个板块')
    ap.add_argument('--notes', default=None, help='notes.json 路径（注入人工研判段落）')
    ap.add_argument('--no-bt', action='store_true', help='跳过回测（更快）')
    ap.add_argument('--no-ladder', action='store_true', help='跳过连板梯队扫描')
    ap.add_argument('--anon', action='store_true',
                    help='脱敏模式：不读取/不展示账户资金与持仓口径（报告给别人看时用）')
    ap.add_argument('--pdf', action='store_true',
                    help='额外导出 PDF（复用本机 Chrome headless，见 README P2-7）')
    a = ap.parse_args()

    cfg.require()          # 首次使用必须先做基础数据录入（资金/权限/费用率）

    notes = {}
    if a.notes and os.path.exists(a.notes):
        notes = json.load(open(a.notes, encoding='utf-8'))
    if a.title:
        notes.setdefault('title', a.title)

    cash = a.cash if a.cash else cfg.get('cash')
    if a.anon:
        print(f'[1/3] 采集 {a.code} 数据 …（脱敏模式：不读取也不展示账户资金）')
    else:
        print(f'[1/3] 采集 {a.code} 数据 …（资金 {cash:.0f} 元，来源：'
              f'{"命令行" if a.cash else "config.json"}）')
    d = gather(a.code, sector_top=a.sector_top, cash=cash,
               do_bt=not a.no_bt, do_ladder=not a.no_ladder, anon=a.anon)
    s = d['snap']
    sc = score(d, notes)
    _lb = s.get('lb')
    _lbt = '—' if (_lb is None or _lb != _lb) else f'{_lb:.2f}'
    print(f'      {s["name"]} {s["price"]} {s["chg"]:+.2f}%  量比{_lbt}  '
          f'{"[盘前·用上一交易日收盘]" if d.get("pre_open") else ""}'
          f'板块{len(d["sectors"])}个  回测形态{len(d.get("bt") or [])}个  '
          f'连板{len(d.get("ladder") or [])}只')
    _r = (f'评级 {sc["rating"]}（不予评级，主维度数据缺失）'
          if sc.get('no_rating') else f'评级 {sc["rating"]} {sc["total"]}/100')
    print(f'      {_r}  '
          + ' '.join(f'{n}{g:.0f}/{m}' for n, g, m, _ in sc['dims']))

    print('[2/3] 渲染 HTML …')
    html = build(d, notes)
    out = a.out or f'{s["name"]}{a.code}_{notes.get("title", "深度分析")}_{datetime.date.today():%Y%m%d}.html'
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'[3/3] 已生成: {os.path.abspath(out)}  ({len(html) / 1024:.1f} KB)')
    if a.pdf:
        to_pdf(out)


if __name__ == '__main__':
    main()
