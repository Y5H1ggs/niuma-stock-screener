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
    'fee_rate_roundtrip': 0.0018,                   # 双边综合费用率（佣金+印花税+过户费）
    'stop_loss_pct': -4.0,                          # 你的止损纪律（负值，%）
    'take_profit_pct': 2.0,                         # 兑现目标（%）
    'position_mode': 'single',                      # single = 全仓单吊单只
    'min_avg_amount_yi': 0.5,                       # 最小日均成交额（亿元），过滤流动性差的票
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


if __name__ == '__main__':
    print('config.json 路径:', CFG_PATH)
    print('是否存在:', exists())
    if exists():
        for k, v in _read().items():
            print(f'  {k:22s} = {v}')
