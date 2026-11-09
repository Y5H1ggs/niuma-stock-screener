# -*- coding: utf-8 -*-
"""
_ths_worker.py — 同花顺 thsdk 取数桥（**必须用装了 thsdk 的解释器运行**）

为什么要有这个文件（架构决策，别改成直接 import）：
  `thsdk` 依赖 pandas + 56MB 原生库，而本工具链的主力解释器是纯标准库环境。
  直接把 `import thsdk` 写进 ds_ths.py，会让**没装 thsdk 的机器整套脚本 import 即崩**。
  所以拆成「子进程桥」：ds_ths.py（纯标准库）→ 本文件（thsdk 环境）→ 返回 JSON。

用法：
    python _ths_worker.py '{"op":"blocks","code":"600601"}'

协议：
    入参 argv[1] = JSON；出参 stdout = 单行 JSON {"ok":true,"data":...}
    失败 {"ok":false,"err":"...","exc":"APIError"}
    ★ stdout 只允许输出这一行 JSON —— 任何 print / 第三方库打屏都会污染管道。

★ 认证（2026-11-07 实测，纠正旧结论）：
  `thsdk.auth()` **无需扫码即返回 True**（走临时账号），此前「3.0 必须扫码」的判断
  是因为**直接调数据函数、从未先调 auth()**，报的 `NotAuthenticatedError` 是缺认证步骤。
  若某天临时账号被收紧，再退回 `auth_qrcode()` 扫码（会话会存盘复用）。
"""
import sys
import json
import traceback

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')


# ------------------------------------------------------------------ 基础
def _market(code: str) -> str:
    """6 位代码 → 同花顺市场前缀。

    ★ P103：实测板块成分里有 `USTM920821`（北交所），旧版把 92/43/83/87/88
      一律归 USHA/USZA → 拼出 `USHA920821` 这种不存在代码。北交所前缀是 **USTM**。
      已带 full_code（USHA600601）时直接回原前缀，避免二次误判。
    """
    c = str(code).strip()
    if len(c) > 6 and c[:4].isalpha():
        return c[:4]
    if c.startswith(('92', '43', '83', '87', '88')):
        return 'USTM'
    if c.startswith(('60', '68', '9', '5', '11', '51')):
        return 'USHA'
    return 'USZA'


def _full(code: str) -> str:
    c = str(code).strip()
    if len(c) > 6 and c[:4].isalpha():
        return c                      # 已经是 USHA600601 形式
    return _market(c) + c.zfill(6)


def _df(rows):
    """DataFrame → list[dict]，NaN 转 None，dict 列保留。"""
    out = []
    for rec in rows:
        d = {}
        for k, v in rec.items():
            if v is None:
                d[k] = None
            elif isinstance(v, dict):
                d[k] = v
            elif isinstance(v, float) and v != v:
                d[k] = None
            else:
                try:
                    if hasattr(v, 'item'):
                        v = v.item()
                except Exception:
                    pass
                d[k] = v
        out.append(d)
    return out


def _sec_of(rec):
    s = rec.get('security') or {}
    return s.get('code') or rec.get('code')


# ------------------------------------------------------------------ 各 op
def op_ping(ths, p):
    return {'version': getattr(ths, '__version__', '?')}


def op_blocks(ths, p):
    """个股所属板块（同花顺权威口径）。返回二级行业 + 全部题材/概念板块。"""
    df = ths.list_security_block_memberships(security=_full(p['code']))
    rows = []
    if hasattr(df, 'to_dict'):
        for rec in df.to_dict('records'):
            blk = rec.get('block') or {}
            src = str(rec.get('source_text') or '')
            parts = src.split(',')
            rows.append({
                'id': blk.get('id'),
                'name': blk.get('name') or (parts[1] if len(parts) > 1 else ''),
                'full_code': parts[2] if len(parts) > 2 else '',
                # source_text 末两位在实测里是 (成分数, 层级)，只作参考不当判据
                'raw': src,
            })
    ind = {}
    try:
        m = ths.get_security_industry(security=_full(p['code']))
        ind = {'code': (m.get('industry') or {}).get('security', {}).get('code'),
               'name': (m.get('industry') or {}).get('name')}
    except Exception as e:
        ind = {'err': str(e)}
    return {'industry': ind, 'blocks': rows, 'n': len(rows)}


def op_industry(ths, p):
    """二级行业 → 三级行业（带名称）。"""
    m = ths.get_security_industry(security=_full(p['code']))
    l2 = m.get('industry') or {}
    kids = ths.list_industry_children(industry=l2)
    names = {}
    try:
        cat = ths.list_block_constituents(block=0xC4B5, sort_begin=0, sort_count=0,
                                          sort_id='55', sort_order='A')
        names = {r['full_code']: r.get('name', '') for r in cat.to_dict('records')}
    except Exception:
        pass
    rows = []
    for rec in (kids.to_dict('records') if hasattr(kids, 'to_dict') else []):
        rows.append({'code': rec.get('full_code'),
                     'name': names.get(rec.get('full_code'), '')})
    return {'level2': l2.get('name'), 'level2_code': (l2.get('security') or {}).get('code'),
            'level3': rows}


def op_members(ths, p):
    """板块成分（权威行业成分）。block 传名称或 id。"""
    blk = p.get('block')
    if isinstance(blk, str) and not blk.strip().isdigit():
        r = ths.resolve_block(name=blk.strip())
        blk = r.get('id') if isinstance(r, dict) else None
    else:
        blk = int(blk)
    df = ths.block_constituents(blk, start=0, count=int(p.get('count') or 500))
    out = []
    # ★ P102：`block_constituents` 只给 full_code（如 USZA300936 / USTM920821），
    #   **没有 security 子字典**；按旧写法 code/market 全 null，却仍返回 ok:true。
    #   这里从 full_code 反解，并对取不到的显式返回 None（不许填 0 / 空串冒充）。
    for rec in (df.to_dict('records') if hasattr(df, 'to_dict') else []):
        fc = str(rec.get('full_code') or '')
        mk = fc[:4] if len(fc) > 4 else None
        cd = fc[4:] if len(fc) > 4 else None
        s = rec.get('security') if isinstance(rec.get('security'), dict) else {}
        out.append({'code': s.get('code') or cd, 'market': s.get('market') or mk,
                    'full_code': fc or None})
    return {'block_id': blk, 'members': out, 'n': len(out)}


def op_reports(ths, p):
    """券商研报（东财 0 篇的补位源）。"""
    df = ths.list_security_research_reports(security=_full(p['code']),
                                            traversal={'mode': 'page'})
    rows = _df(df.to_dict('records')) if hasattr(df, 'to_dict') else []
    for r in rows:
        r.pop('abstract', None)          # 摘要过长，正文另取
        # ★ 源里 published_at 恒为 '0001-01-01T00:00:00Z'（无效占位），
        #   time 只有 'MM-DD' 无年份。别把占位值当真实发布日期喂给下游。
        pa = str(r.get('published_at') or '')
        if not pa or pa.startswith(('0001-', '1970-')):
            r['published_at'] = None
    return {'reports': rows[:int(p.get('n') or 20)], 'n': len(rows),
            'note': 'time 字段为 MM-DD（无年份）；rating_change 为原始编码如 "2,维持"'}


def op_flows(ths, p):
    """日级资金流（16 字段：主动/被动 × 超大/大/中/小）。"""
    df = ths.list_security_daily_capital_flows(securities=[_full(p['code'])])
    rows = _df(df.to_dict('records')) if hasattr(df, 'to_dict') else []
    if not rows:
        return {'flow': None}
    f = rows[0]
    f.pop('security', None)
    return {'flow': f,
            'note': '最近一个已收盘交易日口径；盘前/非交易日调用返回的是上一交易日，非当日实时'}


def op_klines(ths, p):
    """同花顺日 K（前复权），末根=最新交易日。

    ★ P102：`klines()` 把日期放在 **DataFrame 索引**（name='time'），不在 columns 里。
      直接 `to_dict('records')` 会静默丢掉日期 → 每条 t=\"None\"，且 ok:true 不报错。
      必须先 `reset_index()` 把索引落成列，再取时间列。
    """
    df = ths.klines(_full(p['code']), count=int(p.get('count') or 250))
    rows = []
    if not hasattr(df, 'reset_index'):
        return {'bars': rows, 'n': 0, 'err': 'klines 未返回 DataFrame'}
    idx_name = df.index.name
    df = df.reset_index()
    tcol = None
    if idx_name and idx_name in df.columns:
        tcol = idx_name
    else:
        for c in df.columns:
            lc = str(c).lower()
            if lc in ('time', 'date', 'index') or 'time' in lc or 'date' in lc:
                tcol = c
                break
    for rec in df.to_dict('records'):
        raw = rec.get(tcol) if tcol else None
        rows.append({'t': str(raw)[:10] if raw is not None else None,
                     'o': rec.get('open'), 'h': rec.get('high'),
                     'l': rec.get('low'), 'c': rec.get('close'),
                     'v': rec.get('volume'), 'amt': rec.get('turnover')})
    return {'bars': rows, 'n': len(rows), 'time_col': str(tcol)}


def op_wencai(ths, p):
    q = str(p.get('query') or '').strip()
    mk = p.get('markets') or ['USHA', 'USZA']
    df = ths.query_wencai(query=q, markets=mk, limit=int(p.get('limit') or 20))
    rows = _df(df.to_dict('records')) if hasattr(df, 'to_dict') else []
    return {'rows': rows, 'n': len(rows), 'fields': list(getattr(df, 'columns', []))}


def op_depth(ths, p):
    r = ths.depth(_full(p['code']))
    return {'depth': r if not hasattr(r, 'to_dict') else _df(r.to_dict('records'))}


OPS = {'ping': op_ping, 'blocks': op_blocks, 'industry': op_industry,
       'members': op_members, 'reports': op_reports, 'flows': op_flows,
       'klines': op_klines, 'wencai': op_wencai, 'depth': op_depth}


# ------------------------------------------------------------------ 入口
def main():
    try:
        payload = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {'op': 'ping'}
    except Exception as e:
        print(json.dumps({'ok': False, 'err': 'bad payload: %s' % e}, ensure_ascii=False))
        return 0
    op = payload.get('op', 'ping')
    try:
        import thsdk
    except Exception as e:
        print(json.dumps({'ok': False, 'err': 'thsdk 未安装: %s' % e}, ensure_ascii=False))
        return 0
    try:
        if not thsdk.auth():
            raise RuntimeError('auth() 返回 False')
        data = OPS[op](thsdk, payload)
        print(json.dumps({'ok': True, 'data': data}, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({'ok': False, 'err': str(e), 'exc': type(e).__name__,
                          'tb': traceback.format_exc()[-600:]}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
