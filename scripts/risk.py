# -*- coding: utf-8 -*-
"""risk.py — 账户级风控闸门（README 路线图 P0-2）

为什么需要它：
  全仓单吊模式下，「单笔纪律」（-4% 止损）不足以控制**账户级**风险 ——
  连续三笔同行业票，遇到行业系统性走弱时三笔一起亏。止损只把**每笔**的亏损限定住，
  账户的**回撤路径**仍然可以是一条直线向下。账户级风控解决的就是这个。

四道闸门（全部机械判据，无主观成分）：
  G1 回撤熔断   已实现回撤 ≥ max_drawdown_pct → 禁止开仓，直到 halt_days 之后
  G2 行业敞口   与最近一笔同行业 → 拒绝（max_same_sector_streak = 1）
  G3 市值档     与最近一笔同市值档（微/小/中/大）→ 降档为半仓
  G4 风险预算   单笔潜在亏损 > 权益 × risk_per_trade_pct → 按上限强制减仓

用法：
  python risk.py check 601169                 # 是否允许开仓 + 建议仓位
  python risk.py status                       # 当前账户风控状态
  python risk.py plan 601169 --cash 6390.74   # 指定可用资金做仓位规划
"""
import os
import sys
import json
import io
import datetime

import cfg
import ds

# ⚠️ 不要在模块顶层重绑 sys.stdout（import 时会关闭调用方的 stdout），重绑放 __main__。

try:
    import journal
except Exception:
    journal = None


# ------------------------------------------------------------------ 工具
def _mktcap(code):
    """流通市值（元）。取不到返回 0。"""
    url = ('https://push2.eastmoney.com/api/qt/stock/get?secid=%s'
           '&fields=f20,f21,f116,f117&ut=%s' % (ds.secid(code), ds.UT))
    r = ds.get(url)
    try:
        d = json.loads(r).get('data') or {}
        return ds._fl(d.get('f21')) or ds._fl(d.get('f116'))
    except Exception:
        return 0.0


def cap_bucket(v):
    yi = (v or 0) / 1e8
    if yi <= 0:
        return '未知'
    if yi < 50:
        return '微盘<50亿'
    if yi < 100:
        return '小盘50-100亿'
    if yi < 300:
        return '中盘100-300亿'
    if yi < 1000:
        return '大盘300-1000亿'
    return '超大盘>1000亿'


def portfolio_snapshot(cash=None):
    """账户快照：可用资金 / 权益 / 持仓 / 回撤 / 熔断截止。"""
    pos, trades, acc = [], [], None
    if journal:
        pos = journal.open_positions()
        trades = journal.list_trades()
        acc = journal.account()
    c = cash if cash is not None else (acc['cash'] if acc else float(cfg.get('cash') or 0.0))
    held = sum(t['buy_total'] for t in pos)
    dd_cur, dd_max = journal.max_drawdown() if journal else (0.0, 0.0)
    return {
        'cash': c, 'held': held, 'equity': c + held, 'positions': pos,
        'trades': trades, 'dd_cur': dd_cur, 'dd_max': dd_max,
        'halt_until': (acc or {}).get('halt_until'),
    }


def _last_trade(trades):
    """最近一笔（按买入日 + id 排序）。"""
    ts = [t for t in trades if t.get('buy_date')]
    return sorted(ts, key=lambda t: (t['buy_date'], t['id']))[-1] if ts else None


def enforce_halt():
    """检查回撤并落熔断状态。返回 halt_until（无则 None）。"""
    if not journal:
        return None
    acc = journal.account()
    dd_cur, _ = journal.max_drawdown()
    thr = float(cfg.get('max_drawdown_pct', 15.0))
    today = datetime.date.today()
    hu = acc.get('halt_until')
    if dd_cur <= -thr:
        if not hu or datetime.date.fromisoformat(hu) < today:
            hu = (today + datetime.timedelta(days=int(cfg.get('halt_days', 5)))).isoformat()
            acc['halt_until'] = hu
            acc['note'] = '回撤 %.2f%% 触发熔断（阈值 %.0f%%）' % (dd_cur, thr)
            journal._save(journal.P_ACCOUNT, acc)
            return hu
    else:
        if hu and datetime.date.fromisoformat(hu) > today:
            # 回撤修复后不主动解除，等自然到期（机械、可预期）
            pass
    return hu


# ------------------------------------------------------------------ 主判据
def check(code, cash=None, stop_pct=None):
    """对单个候选做账户级风控审查。返回 dict。"""
    snapshot = portfolio_snapshot(cash)
    equity = snapshot['equity']
    reasons, level = [], 'full'

    s = ds.snapshot([code]).get(code)
    if not s:
        return {'code': code, 'allow': False, 'level': 'reject',
                'reasons': ['快照取数失败'], 'suggest_shares': 0, 'equity': equity}
    price = s['price']

    # ---- G1 回撤熔断
    hu = enforce_halt() or snapshot['halt_until']
    if hu and datetime.date.fromisoformat(hu) >= datetime.date.today():
        reasons.append('G1 熔断：账户已实现回撤 %.2f%%（阈值 %.0f%%），禁止开仓至 %s'
                       % (snapshot['dd_cur'], float(cfg.get('max_drawdown_pct', 15)), hu))
        return {'code': code, 'name': s['name'], 'allow': False, 'level': 'reject',
                'reasons': reasons, 'suggest_shares': 0, 'suggest_cost': 0,
                'equity': equity, 'price': price}

    # ---- 行业 / 市值档
    prof = ds.stock_profile(code)
    sector = prof.get('industry') or '未知'
    cap = _mktcap(code)
    bucket = cap_bucket(cap)

    # ⚠️ 排除"正在检查的这只票自身"——否则复检持仓票时必然命中"与上一笔同行业"
    prior = [t for t in snapshot['trades'] if t['code'] != code]
    last = _last_trade(prior)
    if last:
        try:
            last_prof = ds.stock_profile(last['code'])
            last_sector = last_prof.get('industry') or '未知'
        except Exception:
            last_sector = '未知'
        last_bucket = cap_bucket(_mktcap(last['code']))
        streak = int(cfg.get('max_same_sector_streak', 1))
        if streak >= 1 and sector == last_sector and sector != '未知':
            reasons.append('G2 行业敞口：上一笔 %s(%s) 同属「%s」，全仓单吊下等于连续押同一条线 → 拒绝'
                           % (last['name'], last['code'], sector))
            return {'code': code, 'name': s['name'], 'allow': False, 'level': 'reject',
                    'reasons': reasons, 'suggest_shares': 0, 'suggest_cost': 0,
                    'equity': equity, 'price': price, 'sector': sector, 'cap': bucket}
        if bucket != '未知' and bucket == last_bucket:
            level = 'half'
            reasons.append('G3 市值档：与上一笔同处「%s」→ 降档为半仓' % bucket)

    # ---- G4 风险预算
    sp = float(stop_pct if stop_pct is not None else cfg.get('stop_loss_pct', -4.0))
    risk_pct = float(cfg.get('risk_per_trade_pct', 5.0))
    max_loss = equity * risk_pct / 100.0
    max_amt = max_loss / (abs(sp) / 100.0) if sp else equity
    if max_amt < snapshot['cash']:
        reasons.append('G4 风险预算：止损 %.0f%% 下，单笔最大亏损须 ≤ 权益 %.0f%%（%.0f 元）→ 买入额上限 %.0f 元'
                       % (sp, risk_pct, max_loss, max_amt))

    # ---- 仓位
    lot = int(cfg.market().get('lot') or 100)
    budget = min(snapshot['cash'], max_amt)
    if level == 'half':
        budget *= 0.5
    shares = int(budget / price / lot) * lot if price > 0 else 0
    if shares <= 0:
        need = price * lot
        return {'code': code, 'name': s['name'], 'allow': False, 'level': 'reject',
                'reasons': reasons + ['可用现金仅 %.2f 元，不足以买入 1 手 %s（需 %.2f 元）'
                                      % (snapshot['cash'], s['name'], need)],
                'suggest_shares': 0,
                'suggest_cost': 0, 'equity': equity, 'price': price,
                'sector': sector, 'cap': bucket}

    cost = cfg.buy_cost(price, shares)
    return {
        'code': code, 'name': s['name'], 'allow': True, 'level': level,
        'reasons': reasons or ['四道闸门均通过'],
        'suggest_shares': shares, 'suggest_cost': round(cost, 2),
        'stop_price': round(cfg.stop_price(price, shares), 4),
        'break_even': round(cfg.break_even(price, shares), 4),
        'equity': round(equity, 2), 'cash': round(snapshot['cash'], 2),
        'price': price, 'sector': sector, 'cap': bucket,
        'dd_cur': snapshot['dd_cur'],
    }


def status():
    pf = portfolio_snapshot()
    hu = enforce_halt() or pf['halt_until']
    print('=' * 92)
    print('【账户级风控状态】')
    print('=' * 92)
    print('  可用资金   %10.2f 元' % pf['cash'])
    print('  持仓成本   %10.2f 元（%d 笔）' % (pf['held'], len(pf['positions'])))
    print('  账户权益   %10.2f 元' % pf['equity'])
    print('  当前回撤   %9.2f%%   （阈值 %.0f%%）' % (pf['dd_cur'], float(cfg.get('max_drawdown_pct', 15))))
    print('  历史最大回撤 %7.2f%%' % pf['dd_max'])
    print('  熔断状态   %s' % ('⛔ 暂停开仓至 %s' % hu if hu and datetime.date.fromisoformat(hu) >= datetime.date.today() else '✅ 正常'))
    print('  连续同行业上限 %d 笔 | 单笔风险预算 %.1f%% 权益 | 市场档 %s' % (
        int(cfg.get('max_same_sector_streak', 1)), float(cfg.get('risk_per_trade_pct', 5.0)),
        cfg.market()['name']))
    last = _last_trade(pf['trades'])
    if last:
        print('  最近一笔   %s %s（%s）' % (last['buy_date'], last['name'], last['code']))


if __name__ == '__main__':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    argv = sys.argv[1:]
    cash = float(argv[argv.index('--cash') + 1]) if '--cash' in argv else None
    # 子命令词不算候选代码，避免把 'check' 当成股票代码
    args = [a for a in argv if not a.startswith('--') and a not in ('status', 'check', 'plan')]
    if not args:
        status()
    else:
        r = check(args[0], cash=cash)
        print('=' * 92)
        print('【风控审查】%s %s' % (r.get('code'), r.get('name') or ''))
        print('=' * 92)
        for x in r['reasons']:
            print('  ·', x)
        if r['allow']:
            print('  → 结论：允许开仓（%s）' % ('全仓' if r['level'] == 'full' else '半仓'))
            print('  → 建议 %d 股，约 %.2f 元 | 保本 %.3f | 止损 %.3f' % (
                r['suggest_shares'], r['suggest_cost'], r['break_even'], r['stop_price']))
        else:
            print('  → 结论：**拒绝开仓**')
        print('  （行业 %s | 市值档 %s | 权益 %.2f | 回撤 %.2f%%）' % (
            r.get('sector'), r.get('cap'), r['equity'], r.get('dd_cur', 0)))
