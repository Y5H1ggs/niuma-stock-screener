# -*- coding: utf-8 -*-
"""
ds_ths.py — 同花顺（thsdk）数据源，**纯标准库**封装（可选增强源）

为什么不是 `import thsdk`：
    thsdk 依赖 pandas + 56MB 原生库，而本工具链主力解释器是纯标准库环境。
    直接 import 会让没装 thsdk 的机器**整套脚本一启动就崩**。
    所以拆成「子进程桥」：本文件（纯标准库）→ `_ths_worker.py`（thsdk 环境）→ JSON 回传。
    **取不到就返回 None，绝不影响主流程。**

定位（2026-11-07 实测 / 11-09 复核）：
    ✅ 权威板块归属（35 个板块，含行业/概念/指数三层）—— 根治「板块代码凭记忆写错」
    ✅ 券商研报（8 篇，含评级/评级变动/EPS 预测）—— 东财研报一直是 0 篇
    ✅ 日级资金流 16 字段（主动/被动 × 超大/大/中/小）—— 东财只有 主力=超大+大
    ✅ 行业层级 元件 → 印制电路板 / 被动元件（三级）
    ✅ 第 4 路 K 线源（前复权，末根=最新完整交易日）
    ⚠️ 问财自然语言选股：非交易日/表述模糊时可能返回空表，不当判据用

认证（纠正旧结论）：
    `thsdk.auth()` **无需扫码即返回 True**（临时账号）。此前「3.0 必须扫码」是误判——
    当时直接调数据函数、从未先调 auth()，`NotAuthenticatedError` 只是缺认证步骤。

用法：
    import ds_ths
    ds_ths.available()                    -> (bool, 原因)
    ds_ths.blocks('600601')               -> {'industry': {...}, 'blocks': [...]}
    ds_ths.industry('600601')             -> {'level2': '元件', 'level3': [...]}
    ds_ths.members('印制电路板')           -> {'block_id': 53431, 'members': [...]}
    ds_ths.reports('600601', n=10)        -> [{'title','org','rating','eps',...}]
    ds_ths.flows('600601')                -> 16 字段 dict
    ds_ths.klines('600601', 250)          -> {'bars': [{'t','o','h','l','c','v','amt'}]}
    ds_ths.wencai('今日涨停', limit=20)    -> {'rows': [...]}

铁律（踩过的坑，别拆）：
  1. **失败一律降级为 None**，调用方用「—」表示，不许用 0 / 空串冒充（P67/P56）。
  2. 同花顺口径与东财/腾讯**不同源**：板块分类、资金流拆法都不一样。
     **只并列展示，禁止交叉相加或互相校验后断言某一方错**（P5）。
  3. `members()` 返回**全量成分**，含北交所 USTM920xxx（P101 同族）。
     权限过滤交给调用方按 cfg.allowed_prefixes 做，底层不替他筛。
  4. 研报 `time` 字段只有 MM-DD 无年份，`published_at` 恒为无效占位已清成 None；
     `rating_change` 是原始编码（如 "2,维持"），**别直接当评级文字展示**。
"""
import os
import sys
import json
import time
import copy
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
# ★ P70：保证 import cfg 命中本目录的 cfg.py，而不是别处的过期台账副本
if HERE not in sys.path:
    sys.path.insert(0, HERE)

WORKER = os.path.join(HERE, '_ths_worker.py')

_PY = None              # 已确认装了 thsdk 的解释器
_CHECKED = False
_REASON = ''
_TTL = {}               # key -> (ts, value)
TTL_SEC = 90            # 同类查询缓存（同板块反复查会限流，见操作节制铁律）
LAST_ERROR = ''         # 最近一次失败原因（取数失败要看得见，不许静默翻成空值）


# ------------------------------------------------------------ 解释器发现
def _candidates():
    out = []
    env = os.environ.get('THS_PY')
    if env:
        out.append(env)
    try:
        import cfg
        v = cfg.get('ths_python')
        if v:
            out.append(v)
    except Exception:
        pass
    home = os.path.expanduser('~')
    for base in (os.path.join(home, '.workbuddy', 'binaries', 'python', 'envs'),
                 os.path.join(home, '.workbuddy', 'binaries', 'python', 'versions')):
        try:
            for d in sorted(os.listdir(base)):
                for sub in ('Scripts/python.exe', 'python.exe', 'bin/python'):
                    p = os.path.join(base, d, *sub.split('/'))
                    if os.path.isfile(p):
                        out.append(p)
        except Exception:
            pass
    out += [sys.executable, 'python', 'python3']
    seen, uniq = set(), []
    for p in out:
        if p and p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def _probe(py):
    try:
        r = subprocess.run([py, '-c', 'import thsdk,sys;sys.stdout.write("1")'],
                           capture_output=True, timeout=25)
        return r.returncode == 0 and b'1' in r.stdout
    except Exception:
        return False


def _ensure_py():
    """只做解释器探测，**不调 worker**（避免与 available() 互相递归）。"""
    global _PY, _REASON
    if _PY:
        return True, _REASON
    if not os.path.isfile(WORKER):
        _REASON = '缺少 _ths_worker.py'
        return False, _REASON
    for p in _candidates():
        if _probe(p):
            _PY = p
            break
    if _PY is None:
        _REASON = '未找到装了 thsdk 的 python（pip install thsdk pandas，或在 config.json 指定 ths_python）'
        return False, _REASON
    return True, _REASON


def _exec(payload, timeout=40):
    """跑 worker，返回解析后的 dict。自带解释器就绪兜底，可独立调用。"""
    ok, why = _ensure_py()
    if not ok:
        return {'ok': False, 'err': why}
    try:
        r = subprocess.run([_PY, WORKER, json.dumps(payload, ensure_ascii=False)],
                           capture_output=True, timeout=timeout)
        out = (r.stdout or b'').decode('utf-8', 'replace').strip()
        lines = out.splitlines() if out else []
        # ★ P103：worker 只允许输出一行 JSON，但第三方库可能在**之后**再打印，
        #   无条件取 splitlines()[-1] 会把噪声当协议。改成从后往前找协议行。
        line = ''
        for ln in reversed(lines):
            s = ln.strip()
            if s.startswith('{"ok"') or s.startswith("{'ok'"):
                line = s
                break
        if not line:
            err = (r.stderr or b'').decode('utf-8', 'replace')[-300:]
            tail = ' | '.join(x.strip()[:80] for x in lines[-3:])
            return {'ok': False, 'err': 'worker 未返回协议行: %s | stdout尾: %s' % (err, tail)}
        return json.loads(line)
    except subprocess.TimeoutExpired:
        return {'ok': False, 'err': 'worker 超时(%ss)' % timeout}
    except Exception as e:
        return {'ok': False, 'err': '%s: %s' % (type(e).__name__, e)}


def available():
    """返回 (是否可用, 原因)。首次会探测解释器 + ping 一次，之后走进程内缓存。"""
    global _PY, _CHECKED, _REASON
    if _CHECKED:
        return (_PY is not None), _REASON
    _CHECKED = True
    ok, why = _ensure_py()
    if not ok:
        return False, why
    r = _exec({'op': 'ping'})
    if not r.get('ok'):
        _PY = None                 # ★ 让后续 call() 重新探测（环境恢复后能自愈）
        _REASON = 'thsdk 认证失败: %s' % r.get('err', '')
        return False, _REASON
    _REASON = 'thsdk %s @ %s' % (r.get('data', {}).get('version', '?'), _PY)
    return True, _REASON


def call(op, _ttl=TTL_SEC, **kw):
    """统一入口：成功返回 data（dict），失败返回 None。**不抛异常**。

    缓存失败结果不缓存；`_ttl=0` 可强制穿透。
    """
    ok, why = _ensure_py()
    if not ok:
        return None
    key = (op, json.dumps(kw, sort_keys=True, ensure_ascii=False))
    if _ttl:
        hit = _TTL.get(key)
        if hit and time.time() - hit[0] < _ttl:
            return copy.deepcopy(hit[1])
    global LAST_ERROR
    r = _exec(dict(op=op, **kw))
    data = r.get('data') if r.get('ok') else None
    LAST_ERROR = '' if r.get('ok') else str(r.get('err', ''))
    if _ttl and data is not None:
        _TTL[key] = (time.time(), copy.deepcopy(data))
    # ★ P103：返回副本，调用方改返回值不许污染缓存（实测改 nested 会让下次命中变差）
    return copy.deepcopy(data) if data is not None else None


# ------------------------------------------------------------ 高层封装
def blocks(code):
    """个股所属板块（同花顺权威口径）+ 二级行业。"""
    return call('blocks', code=code)


def industry(code):
    """二级行业 → 三级行业（带名称）。"""
    return call('industry', code=code)


def members(block, count=500):
    """板块成分。block 传名称（如 '印制电路板'）或数字 id。

    ★ 返回**全量**，含 USTM920xxx 北交所票；调用方按 cfg.allowed_prefixes 过滤。
    """
    return call('members', block=block, count=count)


def reports(code, n=20):
    """券商研报列表。time 为 MM-DD 无年份，published_at 已清成 None。"""
    d = call('reports', code=code, n=n)
    return d.get('reports') if d else None


def flows(code):
    """日级资金流 16 字段（主动/被动 × 超大/大/中/小）。

    ★ **口径（P103，别盗用列名）**：返回的是**最近一个「已收盘」交易日**的资金流，
      不是当日实时。**盘前 / 非交易日调用拿到的是上一交易日**——实测 turnover
      与 `klines()` 末根 amt 完全一致（同为 11-06）。想做当日资金判断必须另取实时源。
      与东财 f62（主力=超大+大，且不分主动被动）**不同源 → 禁止相加或互校**（P5）。
    """
    d = call('flows', code=code)
    return d.get('flow') if d else None


def klines(code, count=250):
    """同花顺前复权日 K。bars 按时间升序，末根=最新完整交易日。"""
    return call('klines', code=code, count=count)


def wencai(query, limit=20, markets=None):
    """问财自然语言选股。非交易日/模糊表述可能返回空表，不当判据用。"""
    kw = {'query': query, 'limit': limit}
    if markets:
        kw['markets'] = markets
    return call('wencai', **kw)


def depth(code):
    """盘口五档 / 逐笔。交易时段外可能取不到 → None。"""
    d = call('depth', code=code)
    return d.get('depth') if d else None


def status():
    """给 health.py / 自检用的摘要。"""
    ok, why = available()
    return {'ok': ok, 'why': why, 'py': _PY, 'cached': len(_TTL)}


# ------------------------------------------------------------ CLI 自测
def _cli(argv):
    if not argv or argv[0] in ('-h', '--help'):
        print(__doc__)
        return 0
    cmd = argv[0]
    code = argv[1] if len(argv) > 1 else '600601'
    if cmd == 'check':
        ok, why = available()
        print('available:', ok, why)
        if not ok:
            return 1
        # ★ P105 同族：check 报 FAIL 却返回 0 = 把失败算成通过。任一项不可用都必须非零退出。
        failed = 0
        for name, fn in (('blocks', lambda: blocks(code)),
                         ('industry', lambda: industry(code)),
                         ('members', lambda: members('印制电路板')),
                         ('reports', lambda: reports(code, 2)),
                         ('flows', lambda: flows(code)),
                         ('klines', lambda: klines(code, 3))):
            try:
                v = fn()
                if isinstance(v, list):
                    n = len(v)
                elif isinstance(v, dict):
                    n = v.get('n', len(v))       # flows 等无 n 键的 dict 不许再误报 None
                elif v is None:
                    n = 'FAIL: %s' % (LAST_ERROR or 'unknown')
                    failed += 1
                else:
                    n = 1
                print('  %-9s -> %s' % (name, n))
            except Exception as e:
                print('  %-9s -> EXC %s' % (name, e))
                failed += 1
        if failed:
            print('  ❌ %d 项不可用（含服务端侧失败，已降级 None）；退出码 1' % failed)
            return 1
        return 0
    table = {'ping': lambda: status(), 'status': lambda: status(),
             'blocks': lambda: blocks(code), 'industry': lambda: industry(code),
             'members': lambda: members(code if len(argv) > 1 else '印制电路板'),
             'reports': lambda: reports(code), 'flows': lambda: flows(code),
             'klines': lambda: klines(code, int(argv[2]) if len(argv) > 2 else 10),
             'wencai': lambda: wencai(code)}
    if cmd not in table:
        print('未知命令:', cmd)
        return 1
    print(json.dumps(table[cmd](), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(_cli(sys.argv[1:]))
