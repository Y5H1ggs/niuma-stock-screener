# -*- coding: utf-8 -*-
"""journal.py — 交易台账 + 预测台账（append-only）

用法：
    python journal.py add --code 601169 --name 标的丙 --price 10.00 --shares 500 --signal manual
    python journal.py close --id 20260922-601169 --price 11.50 --reason target
    python journal.py list [--open]
    python journal.py status
    python journal.py verify                 # 尝试用最新行情结算到期预测

为什么需要它（README 路线图 P1-3）：
  「后续都需要校准」的前提是**有可校准的原始记录**。没有台账，一切复盘都只能靠记忆，
  而记忆会系统性地只记得住赚钱的那几笔 —— 校准本身就先坏了。
  台账是唯一能支撑「止损执行率」「预测校准曲线」「按信号来源归因」的底座。

三个文件（都在 journal/ 下，不进版本库）：
  trades.json    交易流水：买入/卖出/费用/止损线/信号来源
  forecasts.json 预测流水：每次 predict.py 的概率输出 → 到期自动打分
  account.json   账户状态：可用资金 / 历史峰值 / 熔断截止日
"""
import os
import sys
import json
import io
import datetime

import cfg

# ⚠️ 注意：**不要**在模块顶层重绑 sys.stdout ——
# 被 import 时会把调用方的 stdout 包一层，旧 wrapper 被回收时连带关闭底层 buffer，
# 调用方随后 print 就报 "I/O operation on closed file"。重绑一律放进 __main__。别删这行注释。

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# ⚠️ 数据目录**不能叫 `journal`** —— 会与同目录的 `journal.py` 撞名：
#    从项目根目录 `import journal` 时，Python 会把 `journal/` 目录当成
#    namespace package 导入（而不是 journal.py），于是报
#    `AttributeError: module 'journal' has no attribute 'list_trades'`。
#    实测踩到过（P48）。加下划线前缀彻底消除歧义。
JDIR = os.path.join(ROOT, '_journal')
P_TRADES = os.path.join(JDIR, 'trades.json')
P_FORECASTS = os.path.join(JDIR, 'forecasts.json')
P_ACCOUNT = os.path.join(JDIR, 'account.json')


# ------------------------------------------------------------------ 基础 IO
def _ensure():
    os.makedirs(JDIR, exist_ok=True)


def _load(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, obj):
    """原子写：先写 .tmp 再 replace，避免半截文件。"""
    _ensure()
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _today():
    return datetime.date.today().isoformat()


# ------------------------------------------------------------------ 交易台账
def add_trade(code, name, buy_date, buy_price, shares, signal='manual',
              notes='', stop=None, target=None, stop_reason=None):
    """登记一笔买入。自动用 cfg 的费用模型算出含费支出 / 保本价 / 止损价并存档。

    stop_reason: 卖出纪律，如 'board_break'（脱离板块）/ 'triple_no'（三连否）/ 'stop_loss'
    """
    trades = _load(P_TRADES, [])
    tid = '%s-%s' % (buy_date.replace('-', ''), code)
    if any(t['id'] == tid for t in trades):
        tid += 'b'
    bc = cfg.buy_cost(buy_price, shares)
    rec = {
        'id': tid, 'code': code, 'name': name,
        'buy_date': buy_date, 'buy_price': buy_price, 'shares': shares,
        'buy_total': round(bc, 2),
        'stop_price': round(stop if stop is not None else cfg.stop_price(buy_price, shares), 4),
        'target_price': round(target if target is not None else buy_price * (1 + float(cfg.get('take_profit_pct', 2.0)) / 100), 4),
        'break_even': round(cfg.break_even(buy_price, shares), 4),
        'signal': signal, 'stop_reason': stop_reason, 'notes': notes,
        'sell_date': None, 'sell_price': None, 'sell_total': None,
        'pnl': None, 'pnl_pct': None, 'sell_reason': None,
        'created': datetime.datetime.now().isoformat(timespec='seconds'),
    }
    trades.append(rec)
    _save(P_TRADES, trades)
    return rec


def close_trade(tid, sell_date, sell_price, reason='manual'):
    """平仓。reason ∈ {stop_loss, take_profit, board_break, triple_no, time_exit, manual}"""
    trades = _load(P_TRADES, [])
    hit = None
    for t in trades:
        if t['id'] == tid:
            hit = t
            break
    if not hit:
        return None
    sp = cfg.sell_proceeds(sell_price, hit['shares'])
    hit.update({
        'sell_date': sell_date, 'sell_price': sell_price,
        'sell_total': round(sp, 2),
        'pnl': round(sp - hit['buy_total'], 2),
        'pnl_pct': round((sp - hit['buy_total']) / hit['buy_total'] * 100, 3),
        'sell_reason': reason,
    })
    _save(P_TRADES, trades)
    _sync_account_from_trades()
    return hit


def list_trades(open_only=False):
    ts = _load(P_TRADES, [])
    return [t for t in ts if t['sell_date'] is None] if open_only else ts


def open_positions():
    return list_trades(open_only=True)


def find(tid):
    for t in _load(P_TRADES, []):
        if t['id'] == tid:
            return t
    return None


# ------------------------------------------------------------------ 预测台账
def record_forecast(code, made_on, probs, ref_price, neighbour_n=None, meta=None):
    """记录一次次日预测。probs 形如 {'open_up':41,'close_up':52,'high_ge1':74,'low_le1':81,'break3':15}

    ref_price = 预测基准价（通常为信号日收盘/现价）—— 结算时所有实际涨跌都相对它计算。
    """
    fs = _load(P_FORECASTS, [])
    fid = '%s-%s' % (made_on.replace('-', ''), code)
    if any(f['id'] == fid for f in fs):
        fid += 'b'
    rec = {
        'id': fid, 'code': code, 'made_on': made_on, 'ref_price': ref_price,
        'probs': dict(probs), 'neighbour_n': neighbour_n, 'meta': meta or {},
        'settle_date': None, 'actual': None, 'brier': None,
        'created': datetime.datetime.now().isoformat(timespec='seconds'),
    }
    fs.append(rec)
    _save(P_FORECASTS, fs)
    return rec


def pending_forecasts():
    return [f for f in _load(P_FORECASTS, []) if f['settle_date'] is None]


def brier(probs, actual):
    """多事件 Brier 分数：对每个已定义事件算 (p - o)^2 后取平均。0 最好，1 最差。"""
    items = []
    def add(p_key, outcome):
        if p_key in probs:
            items.append((probs[p_key] / 100.0 - (1.0 if outcome else 0.0)) ** 2)
    add('open_up', actual.get('open', 0) > 0)
    add('close_up', actual.get('close', 0) > 0)
    add('high_ge1', actual.get('high', 0) >= 1.0)
    add('low_le1', actual.get('low', 0) <= -1.0)
    add('break3', actual.get('low', 0) <= -3.0)
    return sum(items) / len(items) if items else None


def settle_forecast(fid, actual):
    """actual = {'date':..., 'open':+x%, 'close':+y%, 'high':+z%, 'low':-w%}（相对 ref_price）"""
    fs = _load(P_FORECASTS, [])
    for f in fs:
        if f['id'] == fid:
            f['actual'] = actual
            f['settle_date'] = actual.get('date') or _today()
            f['brier'] = round(brier(f['probs'], actual), 4) if f['brier'] is None else f['brier']
            _save(P_FORECASTS, fs)
            return f
    return None


# ------------------------------------------------------------------ 账户状态
def account():
    acc = _load(P_ACCOUNT, None)
    if acc is None:
        acc = {
            'cash': float(cfg.get('cash') or 0.0),
            'peak_equity': float(cfg.get('cash') or 0.0),
            'halt_until': None,
            'updated': _today(),
            'note': '由 journal.py 自动维护',
        }
        _save(P_ACCOUNT, acc)
    return acc


def set_cash(v, note=''):
    acc = account()
    acc['cash'] = round(float(v), 2)
    acc['peak_equity'] = max(acc.get('peak_equity') or 0.0, acc['cash'])
    acc['updated'] = _today()
    if note:
        acc['note'] = note
    _save(P_ACCOUNT, acc)
    return acc


def _sync_account_from_trades():
    """卖出后把卖出净得加回现金；买入占用在 add_trade 时由调用方处理。"""
    ts = _load(P_TRADES, [])
    acc = account()
    # 用"最近一次同步点"之后的已平仓交易累加，避免重复计入
    done = [t for t in ts if t['sell_date'] is not None]
    acc['peak_equity'] = max(acc.get('peak_equity') or 0.0, float(cfg.get('cash') or 0.0) + sum(t['pnl'] or 0 for t in done))
    acc['updated'] = _today()
    _save(P_ACCOUNT, acc)
    return acc


def equity_points():
    """按时间顺序输出 (日期, 累计已实现盈亏) 序列，用于回撤计算。"""
    done = [t for t in _load(P_TRADES, []) if t['sell_date'] is not None and t['pnl'] is not None]
    done.sort(key=lambda t: (t['sell_date'], t['id']))
    pts, cum = [], 0.0
    for t in done:
        cum += t['pnl']
        pts.append((t['sell_date'], cum, t))
    return pts


def max_drawdown():
    """返回 (当前回撤%, 历史最大回撤%)，基于已实现盈亏曲线。"""
    pts = equity_points()
    if not pts:
        return 0.0, 0.0
    peak = 0.0
    mdd = 0.0
    for _d, cum, _t in pts:
        peak = max(peak, cum)
        if peak > 0:
            mdd = min(mdd, (cum - peak) / abs(peak) * 100 if peak else 0)
    cur = pts[-1][1]
    peak = max(p for _d, p, _t in pts) if pts else 0
    cur_dd = (cur - peak) / abs(peak) * 100 if peak > 0 else 0.0
    return round(cur_dd, 2), round(mdd, 2)


# ------------------------------------------------------------------ CLI
def _arg(name, default=None):
    a = sys.argv
    if name in a:
        i = a.index(name)
        if i + 1 < len(a):
            return a[i + 1]
    return default


def _status():
    ts = _load(P_TRADES, [])
    done = [t for t in ts if t['sell_date'] is not None]
    acc = account()
    print('=' * 92)
    print('【交易台账状态】')
    print('=' * 92)
    print('  交易总数 %d（已平仓 %d，持仓 %d）' % (len(ts), len(done), len(ts) - len(done)))
    if done:
        win = sum(1 for t in done if (t['pnl'] or 0) > 0)
        tot = sum(t['pnl'] or 0 for t in done)
        print('  已实现胜率 %.1f%%（%d胜%d负）| 累计盈亏 %+.2f 元 | 单笔均值 %+.2f 元' % (
            win / len(done) * 100, win, len(done) - win, tot, tot / len(done)))
        print('  平均盈 %+.2f%% | 平均亏 %+.2f%%' % (
            sum(t['pnl_pct'] for t in done if (t['pnl'] or 0) > 0) / max(win, 1),
            sum(t['pnl_pct'] for t in done if (t['pnl'] or 0) <= 0) / max(len(done) - win, 1)))
        cd, md = max_drawdown()
        print('  当前回撤 %.2f%% | 历史最大回撤 %.2f%%' % (cd, md))
    op = open_positions()
    if op:
        print('  持仓：')
        for t in op:
            print('    %s %s %d股 @%.3f | 保本 %.3f 止损 %.3f' % (
                t['id'], t['name'], t['shares'], t['buy_price'], t['break_even'], t['stop_price']))
    pf = pending_forecasts()
    print('  预测台账：待结算 %d 条 / 共 %d 条' % (len(pf), len(_load(P_FORECASTS, []))))
    print('  账户：现金 %.2f 元 | 峰值 %.2f | 熔断至 %s' % (
        acc['cash'], acc['peak_equity'], acc.get('halt_until') or '无'))


if __name__ == '__main__':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'status'
    if cmd == 'add':
        r = add_trade(_arg('--code'), _arg('--name', ''), _arg('--date', _today()),
                      float(_arg('--price')), int(_arg('--shares')),
                      signal=_arg('--signal', 'manual'), notes=_arg('--notes', ''),
                      stop=float(_arg('--stop')) if _arg('--stop') else None)
        print('已登记：', json.dumps(r, ensure_ascii=False))
        print('  （现金余额不会自动调整，请用 `python journal.py cash <金额>` 校准）')
    elif cmd == 'cash':
        print('已更新：', json.dumps(set_cash(float(sys.argv[2])), ensure_ascii=False))
    elif cmd == 'close':
        r = close_trade(_arg('--id'), _arg('--date', _today()), float(_arg('--price')),
                        reason=_arg('--reason', 'manual'))
        print('已平仓：', json.dumps(r, ensure_ascii=False) if r else '找不到该笔')
    elif cmd == 'list':
        for t in list_trades(open_only='--open' in sys.argv):
            print(json.dumps(t, ensure_ascii=False))
    elif cmd == 'verify':
        print('提示：预测结算需要行情数据，请运行 calibrate.py settle')
    else:
        _status()
