# -*- coding: utf-8 -*-
"""make_privacy_terms.py — 从交易台账**自动反推**隐私禁词（纯标准库）

## 为什么需要它（P126，2026-11-13）

`check_privacy.py` 的设计是对的：清单放仓库外、钩子拦提交信息。
但清单是**手写的**——122 个真实账户数值里只写了 20 个。

于是出现最坏的情况：**钩子正常运行、扫描了、然后显示 ✅ 通过**，
而真实的可用资金数值就这样进了 CHANGELOG 和 **commit message**。

> **一个只查了 16% 清单的闸门，比没有闸门更危险**——
> 因为它会给你一个「已检查」的错觉，让你不再检查。

根因不是疏忽，是**机制错了**：真实数值的来源是 `_journal/`（会持续变化），
而禁词清单靠人记得同步。**需要人记住的事，迟早会忘。**

## 做法

以 `_journal/*.json` 为**唯一真值源**，按字段名分类抽取并生成变形：

| 类 | 字段 | 生成的形态 |
|---|---|---|
| 账户金额 | `cash` `peak_equity` `pnl` `buy_total` `sell_total` | 原值 / 千分位 / 取整 |
| 成交价·价位 | `buy_price` `sell_price` `stop_price` `target_price` `break_even` `breakeven` | 原值 / 补零到 2~3 位 |
| 股数 | `shares` | 只生成「N 股」（纯数字太通用，会海量假阳性） |
| 标的 | `code` `secid` `name` `stock_name` | 原样（**与私有交易绑定的标的同样不能出现**，P100 铁律②） |

**刻意不拦**：`close_up` `brier` `auc_oos` 这类统计量、`open/close/high/low` 这类涨跌幅——
它们不是账户口径，拦了只会制造假阳性，进而诱导 `--no-verify` 绕过（那才是真失效）。

## 文件结构

`_privacy_terms.txt` 分两段，用标记分隔：

    # >>> AUTO-GENERATED BEGIN（勿手改，重跑本脚本覆盖）
    ...
    # <<< AUTO-GENERATED END

    # >>> MANUAL BEGIN（手工维护，生成器不会动）
    ...
    # <<< MANUAL END

首次运行会把现有内容整体视为「手工区」。

用法：
    python scripts/make_privacy_terms.py            # 重新生成
    python scripts/make_privacy_terms.py --dry-run  # 只看会生成多少条，不写文件
"""
import io
import json
import os
import sys
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JDIR = os.path.join(ROOT, '_journal')
TPATH = os.path.join(ROOT, '_privacy_terms.txt')

AUTO_B = '# >>> AUTO-GENERATED BEGIN（勿手改，重跑 make_privacy_terms.py 覆盖）'
AUTO_E = '# <<< AUTO-GENERATED END'
MAN_B = '# >>> MANUAL BEGIN（手工维护，生成器不会动）'
MAN_E = '# <<< MANUAL END'

# 字段分类。**只收账户口径**，统计量一律不收（见模块 docstring）
MONEY = ('cash', 'peak_equity', 'pnl', 'buy_total', 'sell_total')
PRICE = ('buy_price', 'sell_price', 'stop_price', 'target_price',
         'break_even', 'breakeven')
SHARES = ('shares',)


def collect():
    """从台账抽取 {字段类: set(数值)}"""
    got = {'money': set(), 'price': set(), 'shares': set(),
           'codes': set(), 'names': set()}
    if not os.path.isdir(JDIR):
        return got
    for fn in sorted(os.listdir(JDIR)):
        if not fn.endswith('.json'):
            continue
        try:
            data = json.load(io.open(os.path.join(JDIR, fn), encoding='utf-8'))
        except Exception:
            continue

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        if k in MONEY:
                            got['money'].add(round(float(v), 2))
                        elif k in PRICE:
                            got['price'].add(round(float(v), 4))
                        elif k in SHARES:
                            got['shares'].add(int(round(float(v))))
                    elif isinstance(v, str) and k in ('code', 'secid'):
                        got['codes'].add(v.strip())
                    elif isinstance(v, str) and k in ('name', 'stock_name'):
                        got['names'].add(v.strip())
                    else:
                        walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        walk(data)
    return got


def variants(kind, v):
    """生成一个数值的所有要拦的字符串形态"""
    out = []
    if kind == 'money':
        out.append('%.2f' % v)                       # 形如 1234.56
        out.append('%.1f' % v)                       # 形如 1234.6
        if abs(v) >= 1000:
            out.append('{:,}'.format(v))             # 形如 1,234.56
            out.append('{:,}'.format(round(v, 0)))   # 形如 1,235
        out.append('%d' % round(v))                  # 形如 1235
    elif kind == 'price':
        out.append('%.2f' % v)                       # 形如 9.87
        s3 = '%.3f' % v
        out.append(s3)                               # 形如 9.870
        if s3.endswith('0'):
            out.append('%.4f' % v)                   # 形如 9.8700
    elif kind in ('codes', 'names'):
        out.append(str(v))
    elif kind == 'shares':
        n = int(v)
        out.append('%d 股' % n)                       # 形如 123 股
        out.append('%d股' % n)                        # 形如 123股
    # 去重、去空、去太短（如 0 / 1 这种）
    seen, res = set(), []
    for s in out:
        if len(s.replace(',', '').replace('.', '').replace('股', '')) < 2:
            continue
        if s not in seen:
            seen.add(s)
            res.append(s)
    return res


def split_existing():
    """读出 (手工区文本, 是否有标记)"""
    if not os.path.exists(TPATH):
        return '', False
    txt = io.open(TPATH, encoding='utf-8').read()
    if AUTO_B in txt and MAN_B in txt:
        head = txt.split(MAN_B, 1)[1]
        body = head.split(MAN_E, 1)[0]
        return body.strip('\n'), True
    # 无标记 → 整体当手工区
    return txt.strip('\n'), False


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    dry = '--dry-run' in sys.argv

    got = collect()
    if not (got['money'] or got['price'] or got['shares']
            or got['codes'] or got['names']):
        print('  ✗ 台账里没抽到任何账户数值（_journal 为空或不存在？）')
        print('    → 此时**不能**生成空清单，那会让闸门退化成「什么都不查」')
        return 1

    auto_lines = []
    for kind, label in (('money', '账户金额'), ('price', '成交价/价位'), ('shares', '持股数'),
                        ('codes', '交易过的标的代码'), ('names', '交易过的标的名称')):
        vals = sorted(got[kind])
        if not vals:
            continue
        auto_lines.append('# ---- %s（%d 个原始值）----' % (label, len(vals)))
        for v in vals:
            for s in variants(kind, v):
                # price 类加 ~ctx~ 前缀：价格本身不敏感，**价格 + 账户语境**才敏感。
                # 不加这个，`6.94` 会命中统计表里的 `+0.0605（+6.94）`——假阳性会
                # 诱导人用 --no-verify 绕过，闸门反而失效（P127）。
                auto_lines.append(('~ctx~' + s) if kind == 'price' else s)
        auto_lines.append('')

    manual, had_marker = split_existing()

    hdr = [
        '# 禁词清单 —— ⚠️ 本文件**不入仓库**（.gitignore 已屏蔽），只在本机存在。',
        '#',
        '# 为什么清单要放仓库外：把「要禁的具体字符串」写进会被提交的脚本，',
        '#   等于把禁词本身又泄露一次。',
        '#',
        '# ★ 自动区由 scripts/make_privacy_terms.py 从 _journal/ 反推生成。',
        '#   台账每更新一次（新交易 / 平仓），**必须重跑一次**，否则闸门会再次只覆盖 16%。',
        '#   手工区放「标的名称/代码」这类台账里没有的东西。',
        '#',
        '# 生成于 %s' % datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        '',
        AUTO_B,
    ]
    tail = [AUTO_E, '', MAN_B, manual, MAN_E, '']
    text = '\n'.join(hdr + auto_lines + tail)

    n_auto = len([l for l in auto_lines if l and not l.startswith('#')])
    n_man = len([l for l in manual.split('\n') if l.strip() and not l.lstrip().startswith('#')])
    print('  自动区 %d 条（%d 金额 / %d 价格 / %d 股数 / %d 代码 / %d 名称）' % (
        n_auto, len(got['money']), len(got['price']), len(got['shares']),
        len(got['codes']), len(got['names'])))
    print('  手工区 %d 条（%s）' % (n_man, '沿用原标记' if had_marker else '首次运行，整体视为手工区'))
    print('  合计 %d 条' % (n_auto + n_man))

    if dry:
        print('  （--dry-run，未写入）')
        return 0

    io.open(TPATH, 'w', encoding='utf-8').write(text)
    print('  已写入 %s' % os.path.basename(TPATH))
    print('  ⚠️ 下一步：python scripts/check_privacy.py --all 与 --history 各跑一次')
    return 0


if __name__ == '__main__':
    sys.exit(main())
