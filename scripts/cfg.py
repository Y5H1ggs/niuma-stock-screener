# -*- coding: utf-8 -*-
"""
cfg.py — 用户基础数据配置（脱敏核心）

设计原则：**任何"个人参数"都不许写死在脚本里**。
资金规模、可交易板块、价格上限、费用率、止损纪律全部从 `config.json` 读取。
`config.json` 不进版本库（见 .gitignore），仓库里只放 `config.example.json`。

两种加载模式：
  cfg.get(key)      —— 宽松模式，缺 config.json 时回落到 DEFAULTS，库函数不会炸
  cfg.require()     —— 严格模式，入口脚本调用：没配好就打印录入指引并退出
"""
import os
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_PATH = os.path.join(ROOT, 'config.json')
EXAMPLE_PATH = os.path.join(ROOT, 'config.example.json')

DEFAULTS = {
    'cash': None,                                   # 可用资金（元）—— 必填
    'max_price': None,                              # 单票价格上限（元），null = 不限
    'allowed_prefixes': ['60', '000', '001', '002', '003'],   # 你有权限交易的板块前缀
    'exclude_st': True,                             # 是否剔除 ST / 退市风险
    'commission_rate': 0.00025,                     # 佣金费率（万2.5 写成 0.00025）
    'commission_min': 5.0,                          # ★ 每边最低佣金（元）—— 小资金最大的摩擦成本
    'stamp_tax_rate': 0.0005,                       # 印花税（仅卖出方收取）
    'transfer_fee_rate': 0.00001,                   # 过户费（双边收取）
    'fee_rate_roundtrip': None,                     # 旧版粗略比例口径（仅旧配置生效）
    'stop_loss_pct': -4.0,                          # 你的止损纪律（负值，%）
    'take_profit_pct': 2.0,                         # 兑现目标（%）
    'position_mode': 'single',                      # single = 全仓单吊单只
    'min_avg_amount_yi': 0.5,                       # 最小日均成交额（亿元），过滤流动性差的票
    # ---- 账户级风控（README P0-2）----
    'max_drawdown_pct': 15.0,                       # 已实现回撤达到该值 → 触发熔断（暂停开仓）
    'halt_days': 5,                                 # 熔断后暂停开仓的自然日数
    'risk_per_trade_pct': 5.0,                      # 单笔最大亏损占权益上限（%）——决定仓位上限
    'max_same_sector_streak': 1,                     # 连续同行业开仓上限（1 = 禁止连续同行业）
    'market': 'cn_main',                            # 市场档（P2-9 多市场适配），见 MARKETS
}

# 市场档（README P2-9）：把硬约束做成可配置项，换市场只改这里
MARKETS = {
    'cn_main': {
        'name': '沪深主板',
        'allowed_prefixes': ['60', '000', '001', '002', '003'],
        'limit_pct': 10.0,          # 涨跌停幅度（%）
        'lot': 100,                 # 最小交易单位（股）
        't_plus': 1,                # T+1
        'currency': 'CNY',
    },
    'cn_all': {
        'name': '沪深全市场（含创业板/科创板，需相应权限）',
        'allowed_prefixes': ['60', '000', '001', '002', '003', '300', '301', '688', '689'],
        'limit_pct': 20.0,
        'lot': 100, 't_plus': 1, 'currency': 'CNY',
    },
    'cn_bj': {
        'name': '北交所（需开通权限）',
        'allowed_prefixes': ['43', '83', '87', '88', '92'],
        'limit_pct': 30.0,
        'lot': 100, 't_plus': 1, 'currency': 'CNY',
    },
}

HELP = """
====================================================================
  ⚠️  未检测到 config.json —— 请先完成【基础数据录入】再启动
====================================================================
本工具不含任何预设的个人参数（资金规模、交易权限、止损纪律都需要你自己填），
目的是让每个使用者用自己的参数跑，而不是沿用别人的。

步骤：
  1) 把 {ex} 复制为 config.json（同目录）
  2) 打开填写以下几项（其余可保持默认）：

     cash                 必填。你的可用资金（元）。用于计算"可买股数 / 资金占用 / 费用"。
     allowed_prefixes     你有交易权限的板块前缀。默认 ["60","000","001","002","003"] = 沪深主板。
                          没有创业板/科创板权限就不要加 300 / 301 / 688。
     max_price            单票价格上限（元），null = 不限。资金小的话建议设 15 左右。
     fee_rate_roundtrip   双边综合费用率，默认 0.0018（约 0.18%）。
                          资金小、有 5 元最低佣金的，建议调到 0.002~0.003。
     stop_loss_pct        你的止损纪律（负值），默认 -4.0。这是超短模式的生死线。
     take_profit_pct      兑现目标（%），默认 2.0。

  3) 保存后重新运行即可。

不想手动建文件？直接说一句"帮我初始化 config"也可以。
====================================================================
""".format(ex=os.path.basename(EXAMPLE_PATH))

_cache = None


def exists():
    return os.path.exists(CFG_PATH)


def _read():
    global _cache
    if _cache is not None:
        return _cache
    data = dict(DEFAULTS)
    if os.path.exists(CFG_PATH):
        try:
            with open(CFG_PATH, encoding='utf-8') as f:
                user = json.load(f)
            if isinstance(user, dict):
                for k, v in user.items():
                    if not k.startswith('_'):
                        data[k] = v
        except Exception as e:
            raise SystemExit(f'config.json 读取失败（{e}）。请检查是否为合法 JSON（注意逗号/引号）。')
    _cache = data
    return data


def get(key, default=None):
    """宽松取值：库函数用。"""
    v = _read().get(key)
    return default if v is None else v


def all():
    return dict(_read())


def require():
    """严格闸门：入口脚本先调它。缺配置就打印录入指引并退出。"""
    if not exists():
        print(HELP)
        raise SystemExit(2)
    c = _read()
    if not c.get('cash') or float(c['cash']) <= 0:
        print(HELP)
        print('   ↑ 检测到 config.json 存在，但 cash（可用资金）没填或不是正数。\n')
        raise SystemExit(2)
    return c


# ------------------------------------------------------------ 市场档（P2-9 多市场适配）
def market():
    """当前市场档参数。换市场只改 config.json 的 market 字段，不动代码。"""
    return MARKETS.get(get('market') or 'cn_main', MARKETS['cn_main'])


def limit_pct():
    """涨跌停幅度（%），按市场档取（主板 10 / 创业科创 20 / 北交所 30）。"""
    return float(get('limit_pct') or market()['limit_pct'])


# ------------------------------------------------------------ 费用与纪律计算
def _rates():
    """返回 (佣金率, 最低佣金, 印花税率, 过户费率)。"""
    cr, cmin = get('commission_rate'), get('commission_min')
    st, tf = get('stamp_tax_rate'), get('transfer_fee_rate')
    if None in (cr, cmin, st, tf):
        rt = float(get('fee_rate_roundtrip') or 0.0018)   # 向后兼容旧配置
        return rt * 0.35, 0.0, rt * 0.5, rt * 0.15
    return float(cr), float(cmin), float(st), float(tf)


def buy_cost(price, shares):
    """买入总支出（含佣金 + 过户费）。"""
    cr, cmin, _st, tf = _rates()
    amt = price * shares
    return amt + max(cmin, amt * cr) + amt * tf


def sell_proceeds(price, shares):
    """卖出净得（扣佣金 + 印花税 + 过户费）。"""
    cr, cmin, st, tf = _rates()
    amt = price * shares
    return amt - max(cmin, amt * cr) - amt * st - amt * tf


def _solve(shares, target_net, hi=None):
    """二分求"卖出净得 = target_net"对应的价格。"""
    cr, cmin, st, tf = _rates()
    if hi is None:
        hi = max(target_net / max(shares, 1) * 3.0, 1.0)
    lo = 0.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        amt = mid * shares
        net = amt - max(cmin, amt * cr) - amt * st - amt * tf
        if net < target_net:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def break_even(price, shares):
    """含双边费用的真实保本卖出价（注意：高于成本价本身）。"""
    return _solve(shares, buy_cost(price, shares))


def stop_price(cost, shares, pct=None):
    """按 stop_loss_pct 计算的止损触发价。

    口径：亏损额 = 买入总支出 × pct，即**含费的真实亏损**，
    因此会比"成本价 × (1+pct)"略低一点（小资金下约低 0.1~0.3%）。
    """
    p = float(get('stop_loss_pct', -4.0) if pct is None else pct)
    return _solve(shares, buy_cost(cost, shares) * (1 + p / 100.0))


def roundtrip_fee(price, shares):
    """一次完整买入+卖出的总费用（元）。"""
    amt = price * shares
    return (buy_cost(price, shares) - amt) + (amt - sell_proceeds(price, shares))


if __name__ == '__main__':
    print('config.json 路径:', CFG_PATH)
    print('是否存在:', exists())
    if exists():
        for k, v in _read().items():
            print(f'  {k:22s} = {v}')
        c = _read()
        if c.get('cash'):
            px = min(float(c.get('max_price') or 10.0), 10.0)
            sh = int(float(c['cash']) / px / 100) * 100
            if sh > 0:
                be, sp = break_even(px, sh), stop_price(px, sh)
                print()
                print(f'  费用模型自检（按 {px:.2f} 元买 {sh} 股）:')
                print(f'    买入总支出   {buy_cost(px, sh):>10,.2f} 元')
                print(f'    双边总费用   {roundtrip_fee(px, sh):>10,.2f} 元 '
                      f'（占 {roundtrip_fee(px, sh) / buy_cost(px, sh) * 100:.3f}%）')
                print(f'    保本卖出价   {be:>10.4f} 元（需涨 {(be / px - 1) * 100:+.2f}%）')
                print(f'    止损触发价   {sp:>10.4f} 元（{c.get("stop_loss_pct")}% 含费口径）')
