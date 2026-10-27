# -*- coding: utf-8 -*-
"""
ds.py — A股 T+1 选股便携数据源库（纯标准库，零依赖）

覆盖：实时快照 / 全市场列表 / 个股资金 / 资金流时序 / 板块归属 / 板块成分 /
      K线三源 / 回测用前复权日K / 情绪温度板块 / 技术指标计算。

用法：
    from ds import *
    snap = snapshot(['601169','600000'])
    rows = clist(fid='f3', fs=MARKET_MAIN, pages=8)
    k = kline_qq('600000', 800)
    ind = indicators([...closes...], live=7.77)

所有接口实测可用（2026-09）。限流降级链已内置。
"""
import urllib.request, json, ssl, time, math, re, sys
import urllib.parse

# ---------------------------------------------------------------- 基础
import cfg                        # 个人参数（资金/权限/费用）全部外置到 config.json

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

UT = 'fa5fd1943c7b386f172d6893dbfba10b'          # 当前可用；失效时换 token
UT_FALLBACK = ['b2884a393a59ad64002292a3e90d46a5', 'fa5fd1943c7b386f172d6893dbfba10b']
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0'

EM_HOSTS = ['push2delay.eastmoney.com', 'push2.eastmoney.com', 'push2his.eastmoney.com']
MARKET_MAIN = 'm:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23'   # 全市场（含主板/中小/创业/科创）
MARKET_BOARD = 'm:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23'   # 同上；如需纯主板请在代码层过滤

EMOTION_BK = {'昨日涨停': 'BK0815', '昨日首板': 'BK1630', '昨日连板': 'BK0816'}


# ---------------- 连接级熔断器（P75）----------------
# 背景：实测 `*.push2*.eastmoney.com` 全族被连接级阻断时（HTTP 000、0.2 秒内 RST），
#       `board_index()` 仍会发起 8 页 × 4 主机 = 32 次 get()，实测耗时 **234 秒**，
#       而且持续请求只会延长阻断。熔断后本次运行快速失败。
_BREAK = {'fails': 0, 'until': 0.0, 'tripped': False, 'hosts': set(), 'short_circuited': 0}
# ★ 按**主机族**独立熔断（P76 修正）：原实现是全局计数 —— 实测 2026-09-23 下午
#   `board_index()` 因 push2 全灭连续失败后拉起全局熔断，**连带把 qt.gtimg.cn（腾讯快照）
#   与 hq.sinajs.cn（新浪资金流）也一起短接**，表现为"新浪/腾讯也挂了"这种**假故障**，
#   把可用源误判成不可用。现在每个主机族各记各的，一个族挂掉不影响其它族。
_BREAK_BY_HOST = {}
BREAK_AFTER = 8         # 同一主机族连续失败多少次触发熔断
BREAK_COOLDOWN = 90.0   # 熔断冷却秒数


def _fam(host):
    """主机 → 主机族。
    `push2` / `push2his` / `push2delay` / `82.push2` … 是**同一套后端**（一起挂、一起恢复），
    归为一个族 'em-push2'；其余按域名各成一族（qt.gtimg.cn / hq.sinajs.cn / datacenter…）。"""
    h = str(host).lower()
    if 'push2' in h:
        return 'em-push2'
    return h


def _bstate(host):
    return _BREAK_BY_HOST.setdefault(
        _fam(host), {'fails': 0, 'until': 0.0, 'tripped': False, 'short_circuited': 0})


def breaker_state():
    """熔断器状态（含各族明细 `families`）。
    `tripped/fails/remain_s` 为**历史聚合值**（兼容旧调用方）；
    判断"某个源还能不能用"必须看 `families[族名]`，不要看聚合值。"""
    s = dict(_BREAK)
    s['hosts'] = sorted(_BREAK['hosts'])
    s['remain_s'] = max(0.0, round(_BREAK['until'] - time.time(), 1))
    now = time.time()
    s['families'] = {k: {'fails': v['fails'], 'tripped': v['tripped'],
                         'remain_s': max(0.0, round(v['until'] - now, 1)),
                         'short_circuited': v['short_circuited']}
                     for k, v in _BREAK_BY_HOST.items()}
    return s


def _reset_breaker():
    _BREAK['fails'] = 0
    _BREAK['until'] = 0.0
    _BREAK['tripped'] = False
    _BREAK_BY_HOST.clear()


def get(url, tries=3, enc='utf-8', ref='https://quote.eastmoney.com/', timeout=12, gap=0.8):
    """带重试的 GET。

    ⚠️ 两件事要分清（P75）：
      · **间歇空返回/断连** → 换 token / 换主机 + 间隔重试，通常几次内成功；
      · **连接级阻断**（实测 `*.push2*.eastmoney.com` 全部 HTTP 000、**0.2 秒内被 RST**，
        换主机 / 换 ut / 走代理 / 直连全部无效）→ 重试**毫无用处**，
        只会让 `board_index` 跑到 234 秒，并把"偶发被掐"拖成"持续被掐"。
    因此在单一 choke point 上加**连接级熔断器**：连续失败到阈值后，
    本次运行内直接快速失败，不再产生网络请求；状态用 `breaker_state()` 暴露。
    """
    host = url.split('/')[2] if '//' in url else url
    st = _bstate(host)
    if st['until'] > time.time():
        st['short_circuited'] += 1
        _BREAK['hosts'].add(host)
        _BREAK['short_circuited'] += 1
        return None
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Referer': ref})
            out = urllib.request.urlopen(req, timeout=timeout, context=CTX).read().decode(enc, 'ignore')
            st['fails'] = 0
            st['tripped'] = False
            _BREAK['fails'] = 0
            return out
        except Exception as e:
            last = e
            time.sleep(gap)
    st['fails'] += 1
    _BREAK['hosts'].add(host)
    _BREAK['fails'] += 1
    if st['fails'] >= BREAK_AFTER:
        st['tripped'] = True
        st['until'] = time.time() + BREAK_COOLDOWN
        _BREAK['tripped'] = True
        _BREAK['until'] = time.time() + BREAK_COOLDOWN
    return None


def _fl(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def allowed(code):
    """是否落在 config.json 声明的可交易板块内（默认沪深主板 60/000/001/002/003）。
    注意：这不是"市场规则"，而是**每个使用者自己的交易权限**——所以放在 config 里。"""
    c = str(code)
    pre = cfg.get('allowed_prefixes') or []
    return any(c.startswith(str(p)) for p in pre)


def secid(code):
    """东财 secid 前缀：沪 1. / 深 0. """
    c = str(code)
    return ('1.' if c.startswith('6') else '0.') + c


def tsym(code):
    """腾讯/新浪 symbol：sh/sz + code。
    ⚠️ 已带 sh/sz/bj 前缀的原样返回（指数必须显式前缀：上证 sh000001，
       不带前缀的 000001 会被判成深市 = 平安银行，这是历史踩过的坑）。"""
    c = str(code)
    if c[:2] in ('sh', 'sz', 'bj'):
        return c
    return ('sh' if c.startswith('6') else 'sz') + c


def n100(price, cash=None):
    """可买股数（100 股整手）。cash 缺省时取 config.json 里的 cash（个人资金不在代码里）。"""
    if cash is None:
        cash = cfg.get('cash') or 0
    p = _fl(price)
    return int(cash // (p * 100)) * 100 if p > 0 else 0


# ---------------------------------------------------------------- 1. 实时快照（腾讯）
def snapshot(codes, raw=False):
    """腾讯实时快照。返回 {code: {name,price,chg,prev,open,high,low,vol,amount,hs,lb,outer,inner,limit_up,bids,asks,time,sealed}}
    ⚠️ 按请求顺序对齐，勿用返回内代码做 key。
    ⚠️ 显式 sh/sz/bj 前缀会保留（指数必须显式传 sh000001），无前缀的走自动判断。"""
    codes = [str(c) if str(c)[:2] in ('sh', 'sz', 'bj') else str(c).lstrip('shsz') for c in codes]
    url = 'http://qt.gtimg.cn/q=' + ','.join(tsym(c) for c in codes)
    txt = get(url, enc='gbk', ref='https://gu.qq.com/')
    out = {}
    if not txt:
        return out
    for i, seg in enumerate(txt.strip().split(';')):
        if '~' not in seg:
            continue
        f = seg.split('~')
        if len(f) < 50 or i >= len(codes):
            continue
        code = codes[i]
        prev = _fl(f[4]); px = _fl(f[3]); lim = round(prev * 1.1, 2)
        try:
            bids = [(f[9 + k * 2], int(_fl(f[10 + k * 2]))) for k in range(5)]
            asks = [(f[19 + k * 2], int(_fl(f[20 + k * 2]))) for k in range(5)]
        except Exception:
            bids, asks = [], []
        out[code] = {
            'code': code, 'name': f[1], 'price': px, 'chg': _fl(f[32]), 'prev': prev,
            'open': _fl(f[5]), 'high': _fl(f[33]), 'low': _fl(f[34]),
            'vol': int(_fl(f[6])), 'amount': _fl(f[37]), 'hs': _fl(f[38]), 'lb': _fl(f[49]),
            'outer': int(_fl(f[7])), 'inner': int(_fl(f[8])),
            'limit_up': lim, 'bids': bids, 'asks': asks, 'time': f[30],
            # 封死 = 现价达涨停 + 卖盘五档全空 + 买一有量
            # ⚠️ 原判据 (asks[0][0] == lim) 会漏判：封死时卖档价格字段返回 "0.00" 而非涨停价
            'sealed': (prev > 0 and px >= lim - 1e-6 and bool(asks)
                       and all(q == 0 for _, q in asks) and any(q > 0 for _, q in bids)),
        }
    return out


# ---------------------------------------------------------------- 2. 全市场列表（东财 clist）
def clist(fid='f3', fs=MARKET_MAIN, pages=8, pz=100, fields='f12,f14,f2,f3,f6,f8,f10,f20,f62,f100'):
    """全市场/板块成分列表。fid=f3 涨幅降序，fid=f62 主力净额降序。
    ⚠️ pz 最大 100（设 2000 不生效）。自动分页。"""
    rows = []
    _m = {'fid': fid, 'fs': fs, 'pages_ok': 0, 'pages_failed': 0, 'total': None,
          'truncated': False, 'at': time.strftime('%H:%M:%S')}
    for pn in range(1, pages + 1):
        url = (f'https://{EM_HOSTS[0]}/api/qt/clist/get?pn={pn}&pz={pz}&po=1&np=1&fltt=2&invt=2'
               f'&fid={fid}&fs={fs}&fields={fields}&ut={UT}')
        r = get(url)
        if not r:
            # 换主机重试一次
            r = get(url.replace(EM_HOSTS[0], EM_HOSTS[1]))
        if not r:
            # ⚠️ **这里只 break，不报错** —— 调用方拿到的是"少了几页"的列表，
            #   和"全市场只有这么多"**长得一模一样**（P72/P79 同族：截断被伪装成"就这么多"）。
            #   不改返回类型（会破坏全部调用方），改为把事实记进 `clist_meta()`。
            _m['pages_failed'] += 1
            _m['truncated'] = True
            break
        try:
            j = json.loads(r)
            diff = (j.get('data') or {}).get('diff') or []
            total = (j.get('data') or {}).get('total', 0)
            _m['total'] = total
            if not diff:
                if pn * pz <= total:
                    _m['truncated'] = True
                    _m['empty_page'] = pn
                break
            rows.extend(diff)
            _m['pages_ok'] += 1
            if pn * pz >= total:
                break
        except Exception:
            _m['pages_failed'] += 1
            _m['truncated'] = True
            break
        time.sleep(0.25)
    _m['rows'] = len(rows)
    _m['complete'] = (not _m['truncated']) and (not _m['pages_failed'])
    _CLIST_META.clear()
    _CLIST_META.update(_m)
    # ★ v1.2.23：**不完整必须喊出来**（此前只记进 clist_meta()，不主动查就看不到）。
    #   实测 2026-10-22：clist 只取到 1920/5563（涨幅降序前 20 页），
    #   却给出「涨 753 / 跌 765、中位 +0.00%、Σ主力 +40.8 亿」，
    #   **与创业板指 −3.05% 完全矛盾**。
    #   ★ 根因不是"少了几页"，是**采样方向偏了**：fid=f3 降序只覆盖涨幅榜上半段，
    #     用它算涨跌家数会系统性高估"上涨比例"。**这是 P91（样本偏差）的实时版**。
    if not _m['complete']:
        print('[ds.clist] ⚠️ 数据不完整：%d/%s 行（pages_ok=%d failed=%d truncated=%s）'
              ' —— 涨跌家数 / 主力合计等 **汇总统计一律不可用**，'
              '只能当作「涨幅降序的前 %d 条」使用；要全市场口径请改用指数 + board_index()。'
              % (len(rows), _m.get('total'), _m.get('pages_ok', 0),
                 _m.get('pages_failed', 0), _m.get('truncated'), len(rows)),
              file=sys.stderr)
    # ★ G8 去重（2026-09-21）：东财分页在**实时排序**（fid=f3/f62，po=1 按值降序）下页间会重叠
    #   —— 同一只票可能同时出现在 pn=2 与 pn=3（实测 600479/600933/002687/603020 各出现两次）。
    #   不去重会让候选池虚增、同一票重复占据注意力，还会让"筛出 N 只"这类统计失真。
    #   按代码保留首次出现顺序（列表本身有序，去重不得打乱排序语义）。
    seen, uniq = set(), []
    for x in rows:
        k = str(x.get('f12'))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(x)
    _m['uniq'] = len(uniq)
    return uniq


_CLIST_META = {}


def clist_meta():
    """**最近一次 `clist()` 的完整性元信息**（非破坏性出口，不改 clist 的返回类型）：

        {'fid','fs','pages_ok','pages_failed','total','truncated','empty_page',
         'rows','uniq','complete','at'}

    ⚠️ 为什么要它：`clist` 某页失败时只是 `break`，调用方拿到的是**少了几页**的列表，
       与"全市场只有这么多"**无法区分**（P72/P79 同族）。凡是要拿 clist 结果做
       "全量统计"（中位数/排名/筛出 N 只）的调用方，**必须先看 `complete`**：
       不完整时只做展示、**不得据此下"全市场/板块内"的结论**。
    """
    return dict(_CLIST_META)


# ---------------------------------------------------------------- 3. 个股资金（东财 ulist.np）
def ulist(codes, fields='f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87'):
    """实时资金口径，返回 {代码: 字段dict}。f62 主力净额 f66 超大单 f72 大单 f78 中单 f84 小单 f184 净占比%。

    ⚠️ `codes` 既可传**代码列表**，也可传**单个代码字符串**（P58）。
       曾只接受列表：传字符串时它把每个字符当成一个代码去拼 secids，
       接口返回空 data → 返回 `{}`，而调用方 `.get('f62') or 0` 只会看到 **0**，
       表现为"当日主力净额 = 0 万"这种**看起来完全正常的错误数据**
       （与 P55 同类：不报错、数字自洽、只是悄悄错）。字符串现在自动包成单元素列表。
    """
    if isinstance(codes, str):
        codes = [codes]
    secids = ','.join(secid(c) for c in codes)
    url = f'https://push2.eastmoney.com/api/qt/ulist.np/get?secids={secids}&fields={fields}&ut={UT}'
    r = get(url)
    if not r:
        # ★ P76 降级：push2 接口级 RST 时改走 datacenter-web（口径可交叉验证：
        #   datacenter 的 PRIME_INFLOW 实测 == 超大单净 + 大单净 == push2 f62，同日同值）
        fb = fund_flow_dc(codes)
        return fb if fb else {}
    try:
        out = {}
        for x in (json.loads(r).get('data') or {}).get('diff') or []:
            out[str(x.get('f12'))] = x
        return out
    except Exception:
        return {}


# ---------------------------------------------------------------- 4. 资金流时序（东财 fflow）
def fflow(code, klt=1, lmt=0, is_sector=False):
    """⚠️⚠️ klt=1 的 f52 是【从开盘累计】的主力净额，不是每分钟增量！禁止累加。
    返回 [(时间, 主力净额累计, 小单, 中单, 大单, 超大单), ...]"""
    sec = ('90.' + code) if is_sector else secid(code)
    url = (f'https://push2.eastmoney.com/api/qt/stock/fflow/kline/get?secid={sec}'
           f'&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56&klt={klt}&lmt={lmt}&ut={UT}')
    r = get(url)
    if not r:
        return []
    try:
        out = []
        for x in (json.loads(r).get('data') or {}).get('klines') or []:
            p = x.split(',')
            out.append((p[0], _fl(p[1]), _fl(p[2]), _fl(p[3]), _fl(p[4]), _fl(p[5])))
        return out
    except Exception:
        return []


def fflow_at(code, hhmm, klt=1, is_sector=False):
    """取最接近某时点的累计主力净额（元）。"""
    ks = fflow(code, klt=klt, is_sector=is_sector)
    pick = None
    for t, zl, *_ in ks:
        ts = t[11:16]
        if ts >= hhmm:
            pick = zl
            break
    if pick is None and ks:
        pick = ks[-1][1]
    return pick


# ---------------------------------------------------------------- 5. 板块归属（东财 slist）
def slist(code, pz=60):
    """反查个股真实所属板块（行业+概念+地域）。返回 [{bk, name}]。
    ⚠️ 禁止猜 BK 代码；只能靠这个接口反查。"""
    url = (f'https://push2delay.eastmoney.com/api/qt/slist/get?spt=3&fltt=2&invt=2'
           f'&fields=f12,f13,f14&secid={secid(code)}&ut={UT}&pi=0&pz={pz}&po=1&np=1')
    r = get(url) or get(url.replace('push2delay', 'push2'))
    if not r:
        # ★ P76 降级：datacenter 反查（比 slist 更全，多带 BOARD_TYPE / 入选理由）
        bo = board_of(code)
        if bo:
            return [{'bk': x['bk'], 'name': x['name'], 'type': x.get('type'),
                     'reason': x.get('reason'), '_src': 'dce'} for x in bo]
        return []
    try:
        return [{'bk': x.get('f12'), 'name': x.get('f14'), '_src': 'push2'}
                for x in (json.loads(r).get('data') or {}).get('diff') or []]
    except Exception:
        return []


def sector_members(bk, fid='f3', pages=6):
    """板块成分（分页取全量，避免"只取前100名导致误报排名"）。

    ★ P76 降级：clist 全灭 → datacenter 反查名单（**只给代码+名称，不给行情**）。
      行情刻意留空（f3=None），交给 `sector_stats()` 既有的腾讯快照回退补齐 ——
      这样 `src` 会被如实标成 'qq'/'dce+qq'，**不会把腾讯行情冒充成东财口径**。
      f62 一律 None（datacenter 名单无个股资金）→ `zl_yi` 自然为 None，**不伪造 0**。"""
    rows = clist(fid=fid, fs=f'b:{bk}', pages=pages,
                 fields='f12,f14,f2,f3,f62,f100')
    if rows:
        return rows
    mem = board_members_dc(bk)
    if mem:
        return [{'f12': x['f12'], 'f14': x['f14'], 'f2': None, 'f3': None,
                 'f62': None, 'f100': None, '_src': 'dce'} for x in mem]
    return []


def stock_profile(code):
    """东财**客观分类**：行业名(f127) + 概念名列表(f129) + 地域(f128) + 板块名(f128)。

    ⚠️ 为什么要用它（2026-09-21 新增，P35）：
      此前板块基准靠「拉该股所属板块 → 按资金排序 → 挑最热 / 按成分数挑最细」，
      任何"挑"的动作都可能挑出对自己最有利的解释 —— 同一只票能拿到 +0.53pp 或 −1.18pp，
      全看算法挑了哪个板块。行业是交易所口径的分类，**每只票恰好一个、不参与挑选**，
      因此它才是可信的相对强度锚点。
      实证：标的乙 f127=电力（而其涨停故事在"碳化硅"题材里）；
            标的甲 f127=教育（主营 81% 是教育，此前却拿"通信技术"做对比 —— G9）。
    ⚠️ **本函数有两条路径，返回的是两套不同的板块体系（P83）**：
       · push2 在线 → 东财「股票所属板块」（f127/f129），`_src='push2'`；
       · push2 掉线 → datacenter F10「**核心题材**」，`_src='dce'`，且多带 `industries` 候选链。
       **两套的板块集合/粒度并不相同。调用方必须读 `_src` 并在输出里标明口径来源**，
       跨时点比较必须各自重算后并列（同 P38）。
    返回 {} 表示取数失败（调用方必须按"不可用"处理，不得伪造）。"""
    u = (f'https://{EM_HOSTS[0]}/api/qt/stock/get?secid={secid(code)}'
         f'&fields=f57,f58,f127,f128,f129&ut={UT}')
    r = get(u) or get(u.replace(EM_HOSTS[0], EM_HOSTS[1]))
    if not r:
        # ★ P76 降级：行业/概念改由 datacenter 板块归属推导
        #   行业取 BOARD_TYPE='行业' 且 LEVEL 最小者（最粗的一级，与 f127 语义最接近）
        bo = board_of(code)
        if not bo:
            return {}
        inds = [x for x in bo if x.get('type') == '行业']
        inds.sort(key=lambda x: _fl(x.get('level'), 99))
        return {
            'code': str(code), 'name': None,
            'industry': (inds[0]['name'] if inds else None),
            # ⚠️ 必须回传**整个行业候选链**（电力设备→电池→电池化学品）：
            #   降级源下最粗的一级（如「电力设备」BK1200）可能不在 datacenter 行业报表里，
            #   调用方需顺着候选往下找到"能在板块索引里命中"的那个，否则行业基准整个丢失，
            #   P35「双基准取更不利者」会退化成单基准（自证校验会报警）。
            'industries': [x['name'] for x in inds],
            'area': None,
            'concepts': [x['name'] for x in bo if x.get('type') != '行业' and x.get('name')],
            'boards': bo, '_src': 'dce',
            '_src_note': '东财 F10「核心题材」体系（带入选理由；板块集合与 push2 不同源）',
        }
    try:
        d = json.loads(r).get('data') or {}
    except Exception:
        return {}
    if not d:
        return {}
    return {
        'code': str(d.get('f57') or code),
        'name': d.get('f58'),
        'industry': d.get('f127'),                       # 客观行业（每票恰一个）
        'area': d.get('f128'),
        'concepts': [c for c in str(d.get('f129') or '').split(',') if c.strip()],
        # ★ P83：**两条路都必须写 `_src`**。此前只有 datacenter 分支写、push2 分支不写，
        #   于是调用方无法分辨当前用的是哪套板块体系 —— 而两套的**板块集合并不相同**
        #   （10-09 实测 002045：push2 f129 给 13 个概念；datacenter F10「核心题材」给 21 个，
        #    多出"2026中报首亏/破增发价股/深股通/融资融券"等）。
        #   静默换口径的降级链，比"直接报错"危险得多 —— 报告必须显示这个字段。
        '_src': 'push2',
        '_src_note': '东财「股票所属板块」体系（f127 行业 / f129 概念）',
        'boards': None,          # push2 分支不提供带 reason 的板块明细，置 None（不是空列表）
    }


_BOARD_IDX = {}


def main_business(code, n=6):
    """主营构成（东财 F10，最新报告期）。用于 G9「板块对比前先核对主业」。

    返回 {'date':'2026-06-30', 'by_product':[(名称,占比%),…], 'by_industry':[…], 'by_region':[…]}；
    返回 {} 表示取数失败 —— 调用方必须按「不可用」处理，**不得据此臆断主业**。
    MAINOP_TYPE：1=按行业、2=按产品、3=按地区。**只有"按产品"能直接回答"它靠什么赚钱"**，
    行业/地区口径会被"华东 93%"这类信息稀释。
    为什么必须看它（G9 实证）：
      · 标的甲主营 教育 81.21% / 物联网 18.11%，却被拿"通信技术"当板块对比；
      · 标的乙主营 漆包线 64.87% / 光伏发电 18.16%，而涨停故事在"碳化硅"
        —— 碳化硅连主营前列都没进，涨的是**题材标签**，不是业绩。
    """
    mkt = 'SH' if str(code)[0] == '6' else ('BJ' if str(code)[0] in '48' else 'SZ')
    u = ('https://datacenter.eastmoney.com/securities/api/data/v1/get'
         '?reportName=RPT_F10_FN_MAINOP'
         '&columns=SECUCODE,REPORT_DATE,MAINOP_TYPE,ITEM_NAME,MAIN_BUSINESS_INCOME,MBI_RATIO'
         f'&filter=(SECUCODE%3D%22{code}.{mkt}%22)&pageNumber=1&pageSize=60'
         '&sortColumns=REPORT_DATE&sortTypes=-1&source=HSF10&client=PC')
    r = get(u, ref='https://emweb.securities.eastmoney.com/')
    if not r:
        return {}
    try:
        res = (json.loads(r).get('result') or {}).get('data') or []
    except Exception:
        return {}
    if not res:
        return {}
    d0 = str(res[0].get('REPORT_DATE') or '')[:10]
    keys = {'1': 'by_industry', '2': 'by_product', '3': 'by_region'}
    out = {'date': d0}
    for x in res:
        if str(x.get('REPORT_DATE') or '')[:10] != d0:
            continue
        k = keys.get(str(x.get('MAINOP_TYPE')))
        nm = str(x.get('ITEM_NAME') or '').strip()
        if not k or not nm or nm.startswith('其他(补充)'):
            continue
        ratio = (_fl(x.get('MBI_RATIO')) * 100) if _ok(x.get('MBI_RATIO')) else None
        out.setdefault(k, []).append((nm, round(ratio, 2) if ratio is not None else None))
    for k in ('by_product', 'by_industry', 'by_region'):
        if out.get(k):
            out[k] = sorted(out[k], key=lambda t: -(t[1] or 0))[:n]
    return out


def board_index(refresh=False):
    """全市场板块索引：行业(m:90+t:2) + 概念(m:90+t:3)，进程内缓存。

    返回 {'by_name': {名称: (bk, kind, zl亿, chg%)}, 'by_bk': {bk: (名称, kind, zl亿, chg%)}}
      kind ∈ {'行业','概念'}；zl/chg 可能为 None（盘前或该字段限流），
      **但名称与代码始终有效** —— 盘前也要能解析出板块，只是不参与资金打分。
    """
    if _BOARD_IDX and not refresh:
        return _BOARD_IDX
    by_name, by_bk = {}, {}
    meta = {'pages_ok': 0, 'pages_failed': 0, 'truncated': [], 'blocked': False}
    for fs, kind in (('m:90+t:2', '行业'), ('m:90+t:3', '概念')):
        seen = set()
        for pn in range(1, 9):
            # 熔断中：不再发请求（否则 8 页 × 4 主机 = 32 次重试，实测白跑 234 秒）
            # ⚠️ 只看 push2 族自己的状态，不能看全局（否则腾讯/新浪的可用性与它耦合）
            if _bstate(EM_HOSTS[0])['until'] > time.time():
                meta['blocked'] = True
                meta['truncated'].append('%s 余下页熔断跳过' % kind)
                break
            diff, total = None, 0
            # 每页最多换主机重试 4 次 —— **失败页不能 break**：
            # 该索引按 fid=f62 降序取（排序随资金榜实时变化），若某页失败就 break，
            # 索引会被**静默截断**，表现为"同一板块名两次查询一次命中一次 None"，
            # 且失败页之后的所有板块整体消失。这与 P68 同族：跳过失败页 = 制造有偏样本。
            for attempt, host in enumerate((EM_HOSTS[0], EM_HOSTS[1], EM_HOSTS[0], EM_HOSTS[1])):
                url = (f'https://{host}/api/qt/clist/get?pn={pn}&pz=100&po=1&np=1'
                       f'&fltt=2&invt=2&fid=f62&fs={fs}&fields=f12,f14,f3,f62&ut={UT}')
                r = get(url)
                if r:
                    try:
                        dd = (json.loads(r).get('data') or {})
                        diff = dd.get('diff') or []
                        total = dd.get('total') or 0
                        break
                    except Exception:
                        diff = None
                time.sleep(0.4 * (attempt + 1))
            if diff is None or (not diff and pn * 100 - 100 < total):
                meta['pages_failed'] += 1
                meta['truncated'].append('%s p%d' % (kind, pn))
                continue
            if not diff:
                break
            meta['pages_ok'] += 1
            for x in diff:
                bk = str(x.get('f12'))
                if not bk.startswith('BK') or bk in seen:
                    continue
                seen.add(bk)
                nm = x.get('f14')
                zl = _fl(x.get('f62')) / 1e8 if _ok(x.get('f62')) else None
                chg = _fl(x.get('f3')) if _ok(x.get('f3')) else None
                rec = (bk, kind, zl, chg)
                by_bk[bk] = (nm, kind, zl, chg)
                by_name.setdefault(nm, rec)      # 同名以先出现者为准（行业优先于概念）
            if pn * 100 >= total:
                break
            time.sleep(0.2)
    if not by_name:
        # ★ P76 降级：push2 clist 全灭 → 改用 datacenter 板块级资金流
        #   ⚠️ datacenter 的 NET_INFLOW 是**板块级口径**，与 sector_stats 的 Σ成分不同源，
        #      meta 里显式标 src='dce'，调用方必须据此标注，禁止与 Σ成分数值混用。
        for kind2, kd in (('concept', '概念'), ('industry', '行业')):
            bf = board_flow_dc(kind2)
            meta['dce_%s' % kind2] = len(bf)
            for bk, (nm, _k, zl, chg, _ex) in bf.items():
                if not nm or bk in by_bk:
                    continue
                rec = (bk, kd, zl, chg)
                by_bk[bk] = (nm, kd, zl, chg)
                by_name.setdefault(nm, rec)
        if by_name:
            meta['src'] = 'dce'
    else:
        meta['src'] = 'push2'
    # ★ 残页不入缓存（P83）：失败页会让索引**静默残缺**，表现为
    #   "同一板块名这次命中、下次 None"，而残缺索引一旦进了进程缓存，
    #   后续所有查询都被它污染 —— **且不报任何错**。
    #   宁可下次重算（有熔断器兜底），也不拿一份有洞的索引当权威。
    if meta['truncated'] or meta['blocked']:
        meta['cached'] = False
        meta['warn'] = ('索引存在残页（%s）→ 本次**不写入缓存**，下次调用会重算；'
                        '调用方不得把"名字查不到"当成"该板块不存在"'
                        % '、'.join(meta['truncated'][:6]))
        return {'by_name': by_name, 'by_bk': by_bk, 'meta': meta}
    meta['cached'] = True
    _BOARD_IDX.clear()
    _BOARD_IDX.update({'by_name': by_name, 'by_bk': by_bk, 'meta': meta})
    return _BOARD_IDX


def _ok(v):
    """字段是否为有效数值。
    ⚠️ 东财 clist 在盘前（09:15 前）与限流时会把 f2/f3/f62 全返回 "-" 占位符，
       而 _fl("-") == 0.0 —— 不校验就会被静默伪造成"全市场平盘、板块普跌 0%"，
       进而让评分把"取数失败"当成"利空"扣分（这是最危险的一类 bug）。"""
    if v is None:
        return False
    s = str(v).strip()
    return s != '' and s != '-' and s.lower() != 'nan'


def _snap_chg(codes, batch=60):
    """腾讯批量快照取行情（东财 clist 行情字段失效时的降级源）。
    返回 {code: {'chg':涨跌幅, 'price':现价}}；仅处理沪/深主板与创业板代码。
    ⚠️ 盘前（09:15 前）腾讯/新浪只返回"今日未开盘"空壳：开=0、量=0、涨幅恒为 0。
       这类快照无信息量，必须剔除，否则会把整块板块算成"全员 0.00%、红盘率 0%"。
    """
    out = {}
    cl = [str(c) for c in codes if str(c)[:1] in '036']
    today = time.strftime('%Y%m%d')
    for i in range(0, len(cl), batch):
        try:
            sp = snapshot(cl[i:i + batch])
        except Exception:
            continue
        for k, v in sp.items():
            if _fl(v.get('vol')) <= 0:
                continue          # 盘前空壳 / 停牌：无昨收涨跌幅信息
            ts = str(v.get('time') or '')
            # ★ P82 快照日期校验：**必须是今日的**。
            #   盘前（09:15 前）腾讯返回的是**上一交易日收盘**快照 —— vol>0、chg 是昨日涨幅，
            #   vol 判据拦不住它。若直接采用，"昨日涨停"板块会算成 **红盘 48/48 = 100%**，
            #   把"今天还没有数据"伪装成"接力环境极健康"。不报错、数字自洽、只是悄悄错
            #   （与 P58/P75 同族）。实测同一函数盘后给 19/54（35%）、盘前给 48/48，**方向相反**。
            if ts[:8].isdigit() and len(ts) >= 8 and ts[:8] != today:
                continue
            if v.get('chg') is not None:
                out[str(k)] = {'chg': v['chg'], 'price': v['price']}
    return out


_STOCK_FLOW_POOL = {}


def stock_flow_pool(pages=12, verbose=False):
    """全市场个股主力净额池 `{code: PRIME_INFLOW(元)}`（**进程内缓存**）。

    ★ 存在意义：push2 挂掉时 `sector_members` 的 `f62` 会**整列为空**，
      此时板块资金就没了（报告里显示「— 未取到」）。
      本函数用东财 datacenter 的**全市场个股资金报表一次分页取完**（实测 5200 只 / 约 5 秒），
      再按板块成分求和 → **把板块资金救回来**。

    ⚠️ **口径必须标注**：这是 **Σ成分**，与 `board_index()` 的**板块级口径不是一回事**
      （同日可以反号，历史实测锂电概念：板块级 −38.62 亿 vs Σ成分 +25.93 亿）。
      两者**只能并列展示，禁止交叉比较**（项目铁律）。
    """
    if _STOCK_FLOW_POOL:
        return _STOCK_FLOW_POOL
    for pg in range(1, pages + 1):
        r = dce_get('RPT_DMSK_TS_STOCKNEW', pz=500, page=pg)
        if not r:
            break
        for x in r:
            c = str(x.get('SECURITY_CODE') or '')
            v = x.get('PRIME_INFLOW')
            if c and v is not None:
                _STOCK_FLOW_POOL[c] = v
        if len(r) < 500:
            break
        time.sleep(0.35)
    if verbose:
        print('    全市场个股资金池：%d 只' % len(_STOCK_FLOW_POOL))
    return _STOCK_FLOW_POOL


def chip_distribution(code, days=180, nbins=40):
    """成本（筹码）分布估算 —— **换手衰减模型**，不是简单成交量堆积。

    ★ 算法：逐日把已有筹码按当日换手率衰减，新成交按当日 [low,high] 均匀分布。
        筹码(今日) = 筹码(昨日) × (1 − 换手率) + 今日成交量按价格分布
      → 这样"很久以前的高位筹码"会随时间被稀释，比"成交量直接累加"更接近真实。
    ⚠️ **这是估算，不是真实筹码数据**：真实筹码分布需 L2 逐笔成交 + 股东名册，
       免费源拿不到。本函数只能回答"哪个价位区间成交最密集"。

    ⚠️ **"万手哥/主力席位"同样拿不到** —— 需 L2 逐笔与龙虎榜席位数据（付费）。
       本函数**不能**区分"谁在买"，只能给出"筹码堆在哪个价位"。

    返回 dict：{bins:[(价格, 万股, 占比)], peak:[(价,占比)...], price_range, current}
    """
    k = kline_list(code, days + 12)
    if not k or len(k) < 60:
        return {}
    eq = {}
    try:
        eq = equity_struct(code)
    except Exception:
        eq = {}
    listed = eq.get('listed') or 0
    if not listed:
        return {}
    k = k[-days:]
    lo = min(x['l'] for x in k)
    hi = max(x['h'] for x in k)
    if hi <= lo:
        return {}
    step = max(0.01, round((hi - lo) / nbins, 2))
    prices = [round(lo + i * step, 2) for i in range(int((hi - lo) / step) + 2)]
    bins = {p: 0.0 for p in prices}
    for x in k:
        vol = x['v'] or 0                      # 手
        turn = (vol * 100.0) / listed          # 换手率（小数）
        turn = min(max(turn, 0.0), 0.5)        # 截断异常值
        for p in bins:
            bins[p] *= (1 - turn)
        pl, ph = x['l'], x['h']
        keys = [p for p in bins if pl <= p <= ph]
        if not keys:
            keys = [min(bins, key=lambda p: abs(p - (pl + ph) / 2))]
        per = vol / len(keys)
        for p in keys:
            bins[p] += per * 100 / 1e4         # 折成万股
    tot = sum(bins.values()) or 1
    arr = [(p, v, v / tot * 100) for p, v in sorted(bins.items(), reverse=True)]
    # 密集区：占比 > 3% 的连续块
    peaks = [(p, pct) for p, v, pct in arr if pct >= 3.0]
    cur = snapshot([code]).get(code, {}).get('price') or k[-1]['c']
    return {'bins': arr, 'peaks': peaks, 'price_range': (lo, hi),
            'current': cur, 'steps': step,
            'above': sum(pct for p, v, pct in arr if p > cur),
            'below': sum(pct for p, v, pct in arr if p <= cur)}


def shareholder_profile(code):
    """股东结构：户数变化（筹码集中度）+ 十大流通股东（含送转除权识别）。

    ⚠️ **送转陷阱**：若多个股东的环比变动是**同一个数字**（如都用 −26.1%），
       那不是减持，是**送股/转增导致的除权**。本函数会检测并标注。
    """
    out = {'holders': [], 'top10': [], 'transfer_note': None}
    r = dce_get('RPT_F10_EH_HOLDERNUM', pz=10, flt='(SECURITY_CODE="%s")' % code,
                sort='END_DATE', stype=-1)
    for x in (r or [])[:6]:
        out['holders'].append({'date': str(x.get('END_DATE'))[:10],
                               'num': x.get('HOLDER_TOTAL_NUM'),
                               'ratio': x.get('TOTAL_NUM_RATIO')})
    r2 = dce_get('RPT_F10_EH_FREEHOLDERS', pz=24, flt='(SECURITY_CODE="%s")' % code,
                 sort='END_DATE', stype=-1)
    if r2:
        latest = str(r2[0].get('END_DATE'))[:10]
        rows = [x for x in r2 if str(x.get('END_DATE'))[:10] == latest]
        rows.sort(key=lambda x: _fl(x.get('HOLDER_RANK'), 99))
        from collections import Counter
        qoqs = [round(_fl(x.get('FREE_RATIO_QOQ'), 0) * 100, 1) for x in rows[:10]
                if x.get('FREE_RATIO_QOQ') is not None]
        cnt = Counter(qoqs)
        same, n_same = (cnt.most_common(1)[0] if cnt else (None, 0))
        if n_same >= 3 and same is not None:
            out['transfer_note'] = ('%d 个股东的环比变动同为 %+.1f%% → '
                                    '判定为**送股/转增除权**，不是减持（勿误读）' % (n_same, same))
        for x in rows[:10]:
            out['top10'].append({
                'rank': int(_fl(x.get('HOLDER_RANK'), 0)),
                'name': str(x.get('HOLDER_NAME') or '')[:30],
                'hold_wan': _fl(x.get('HOLD_NUM')) / 1e4,
                'pct': _fl(x.get('FREE_HOLDNUM_RATIO')),
                'qoq': (_fl(x.get('FREE_RATIO_QOQ')) * 100) if x.get('FREE_RATIO_QOQ') is not None else None,
            })
        out['latest'] = latest
    return out


def business_profile(code, periods=6):
    """公司经营画像：**落到业务数据**（主营构成 / 上下游周转 / 费用结构 / 行业占比）。

    ★ 存在的意义：只给"营收 198 亿、净利 4.7 亿"这种数字，读者无法判断这家公司
      **靠什么赚钱、赚得难不难、在产业链里有没有话语权**。
      本函数用四个业务口径回答这四件事：
        ① 靠什么赚钱 → 分产品/分地区营收构成
        ② 赚得难不难 → 毛利率 + 三费占比 + 净利率
        ③ 有没有话语权 → **应付天数 vs 应收天数**（谁在被谁占款）
        ④ 在行业里多大 → 营收 / 同业合计

    ★ 上下游印证的核心指标：**现金转换周期 CCC = 应收天数 + 存货天数 − 应付天数**
      · CCC < 0：**用上游的钱做生意**（最强）
      · CCC 0~30 天：效率良好
      · CCC > 60 天：自己垫资重，资金链压力大
      半年报口径按 ×180 / 营收（或成本）折算天数。

    返回 dict；取不到返回 {}。
    """
    def _load(rpt):
        r = dce_get(rpt, pz=periods + 4, flt='(SECURITY_CODE="%s")' % code,
                    sort='REPORT_DATE', stype=-1)
        return {str(x.get('REPORT_DATE'))[:10]: x for x in (r or [])}
    B = _load('RPT_DMSK_FN_BALANCE')
    I = _load('RPT_DMSK_FN_INCOME')
    if not (B and I):
        return {}
    dates = sorted(set(B) & set(I), reverse=True)[:periods]
    if not dates:
        return {}

    def _v(d, src, k, scale=1e8):
        v = src[d].get(k)
        return (v / scale) if isinstance(v, (int, float)) else None

    rows = []
    for d in dates:
        rev = _v(d, I, 'TOTAL_OPERATE_INCOME')
        cost = _v(d, I, 'OPERATE_COST')
        ar = _v(d, B, 'ACCOUNTS_RECE')
        iv = _v(d, B, 'INVENTORY')
        ap = _v(d, B, 'ACCOUNTS_PAYABLE')
        dn = _v(d, I, 'DEDUCT_PARENT_NETPROFIT')
        days = 180 if d.endswith('06-30') else (90 if d.endswith('03-31') or d.endswith('09-30') else 360)
        ar_d = (ar / rev * days) if (ar is not None and rev) else None
        iv_d = (iv / cost * days) if (iv is not None and cost) else None
        ap_d = (ap / cost * days) if (ap is not None and cost) else None
        ccc = (ar_d + iv_d - ap_d) if None not in (ar_d, iv_d, ap_d) else None
        gp = (rev - cost) if (rev is not None and cost is not None) else None
        rows.append({
            'date': d, 'rev': rev, 'cost': cost, 'ar': ar, 'inv': iv, 'ap': ap, 'dnp': dn,
            'ar_days': ar_d, 'inv_days': iv_d, 'ap_days': ap_d, 'ccc': ccc,
            'gross_margin': (gp / rev * 100) if (gp is not None and rev) else None,
            'net_margin': (dn / rev * 100) if (dn is not None and rev) else None,
            'exp': {k: (_v(d, I, k) / rev * 100) if (_v(d, I, k) is not None and rev) else None
                    for k in ('SALE_EXPENSE', 'MANAGE_EXPENSE', 'FINANCE_EXPENSE')},
        })

    mb = {}
    try:
        mb = main_business(code) or {}
    except Exception:
        mb = {}
    return {'rows': rows, 'main': mb, 'code': code}


def cashflow_attribution(code, periods=8):
    """经营现金流归因 —— 把 OCF 拆开，并判断是「季节性」还是「真恶化」。

    ★ 为什么必须做这个（2026-10-27 实测教训）：
      单看「经营现金流为负」会得出**完全相反的结论**。
      贸易/出口型企业普遍「上半年铺货、下半年回款」，
      实测 002091：2025H1 **−2.15 亿**，但 2025Q3 就转 **+10.53 亿**、全年 **+23.45 亿**。
      只看 H1 就说"现金流恶化"是错的 —— **必须同口径比去年同期 + 看全年节奏**。

    ★ 归因口径（教科书式，可逐项核对）：
        OCF ≈ 扣非净利 − 营运资本净占用 + 其他
        营运资本净占用 = Δ应收账款 + Δ存货 − Δ应付账款
      「其他」= OCF − 扣非 + 营运资本净占用（残差，含税费/其他应收/非付现调整）

    ★ 三个必须同时看的健康判据：
      ① **应付 > 应收** → 公司在无偿占用上游资金（贸易商议价能力的体现，偏正面）
      ② **OCF 与上年同期同号且幅度接近** → 季节性，不是恶化
      ③ **全年 OCF 是否为正** → 最终能不能收回来

    返回 dict；取不到数据返回 {}。
    """
    def _load(rpt):
        r = dce_get(rpt, pz=periods + 6, flt='(SECURITY_CODE="%s")' % code,
                    sort='REPORT_DATE', stype=-1)
        return {str(x.get('REPORT_DATE'))[:10]: x for x in (r or [])}
    B = _load('RPT_DMSK_FN_BALANCE')
    I = _load('RPT_DMSK_FN_INCOME')
    C_ = _load('RPT_DMSK_FN_CASHFLOW')
    if not (B and I and C_):
        return {}
    dates = sorted(set(C_) & set(B) & set(I), reverse=True)[:periods]
    if not dates:
        return {}

    def _y(x, k):
        v = x.get(k)
        return (v / 1e8) if isinstance(v, (int, float)) else None

    def _row(d):
        b, i, c = B[d], I[d], C_[d]
        return {'date': d,
                'ocf': _y(c, 'NETCASH_OPERATE'),
                'icf': _y(c, 'NETCASH_INVEST'),
                'fcf': _y(c, 'NETCASH_FINANCE'),
                'sales_cash': _y(c, 'SALES_SERVICES'),
                'dnp': _y(i, 'DEDUCT_PARENT_NETPROFIT'),
                'ar': _y(b, 'ACCOUNTS_RECE'),
                'inv': _y(b, 'INVENTORY'),
                'ap': _y(b, 'ACCOUNTS_PAYABLE')}
    rows = [_row(d) for d in dates]

    # 同期对比（最新一期 vs 上年同一报告期）
    cur = rows[0]
    yoy = None
    if len(cur['date']) >= 10:
        y, m, dd = cur['date'].split('-')
        prev_key = '%d-%s-%s' % (int(y) - 1, m, dd)
        prev = next((r for r in rows if r['date'] == prev_key), None)
        if prev and cur['ocf'] is not None and prev['ocf'] is not None:
            dar = (cur['ar'] - prev['ar']) if (cur['ar'] is not None and prev['ar'] is not None) else None
            div = (cur['inv'] - prev['inv']) if (cur['inv'] is not None and prev['inv'] is not None) else None
            dap = (cur['ap'] - prev['ap']) if (cur['ap'] is not None and prev['ap'] is not None) else None
            wc = (dar + div - dap) if None not in (dar, div, dap) else None
            yoy = {'prev_date': prev['date'], 'ocf_prev': prev['ocf'],
                   'ocf_now': cur['ocf'], 'ocf_delta': cur['ocf'] - prev['ocf'],
                   'd_ar': dar, 'd_inv': div, 'd_ap': dap, 'wc_drag': wc,
                   'residual': (cur['ocf'] - (cur['dnp'] or 0) + (wc or 0))
                               if cur['ocf'] is not None else None}

    # 全年节奏（同一年内 Q1/H1/Q3/FY 的 OCF）
    rhythm = {}
    for r in rows:
        rhythm[r['date']] = r['ocf']

    judged = None
    if yoy:
        same_sign = (yoy['ocf_prev'] or 0) * (yoy['ocf_now'] or 0) > 0
        near = abs(yoy['ocf_delta']) <= max(abs(yoy['ocf_prev'] or 0) * 0.5, 1.0)
        if same_sign and near:
            judged = 'seasonal'      # 与去年同季同号且幅度接近 → 季节性
        elif same_sign:
            judged = 'worse' if abs(yoy['ocf_now']) > abs(yoy['ocf_prev']) else 'better'
        else:
            judged = 'reversed'      # 符号反转，需要警惕
    return {'rows': rows, 'yoy': yoy, 'rhythm': rhythm, 'judged': judged,
            'ap_gt_ar': (cur['ap'] or 0) > (cur['ar'] or 0)}


def sector_stats(bk, fallback=True, pre=False):
    """板块统计：家数/红盘数/主力合计/涨幅中位数/涨停名单/个股绝对排名。
    实现铁律 1（相对强度）+ 情绪温度。
    ⚠️ 东财 clist 盘前/限流时 f3 返回 "-"，此时自动降级用腾讯快照补行情
       （成分代码名单仍来自东财，这部分一直是有效的）。
    ⚠️ pre=True（本报告已回退到"上一交易日收盘"口径）时**强制禁用所有实时字段**：
       否则会出现"个股用昨收、板块用今日竞价"的**口径混用**，相对强度直接算错。
       返回 ok=False 表示本板块统计不可用，调用方必须按"中性、不加减分"处理。"""
    if pre:
        # 盘前/竞价口径下，唯一能给的只有"上一交易日各成分涨跌幅"，而那需要逐只拉 K 线
        # （全量成分 × 多板块 = 数百次请求，不现实）。因此如实返回不可用，绝不伪造 0。
        return {'n': 0, 'up': 0, 'zl_yi': None, 'median_chg': None, 'limit_up': [],
                'rows': [], 'ok': False, 'src': 'pre', 'n_valid': 0}

    rows = sector_members(bk, pages=8)
    if not rows:
        return {'n': 0, 'up': 0, 'zl_yi': None, 'median_chg': None, 'limit_up': [],
                'rows': [], 'ok': False, 'src': 'none', 'n_valid': 0}

    valid = [] if pre else [x for x in rows if _ok(x.get('f3'))]
    src = 'em'
    chg_by = {}
    if valid and len(valid) >= len(rows) * 0.5:
        chg_by = {str(x.get('f12')): _fl(x.get('f3')) for x in valid}
    elif fallback:
        snaps = _snap_chg([x.get('f12') for x in rows])
        chg_by = {k: v['chg'] for k, v in snaps.items()}
        src = 'dce+qq' if any(x.get('_src') == 'dce' for x in rows) else 'qq'

    if not chg_by:
        return {'n': len(rows), 'up': 0, 'zl_yi': None, 'median_chg': None,
                'limit_up': [], 'rows': rows, 'ok': False, 'src': 'fail', 'n_valid': 0}

    chg = list(chg_by.values())
    zl_valid = [_fl(x.get('f62')) for x in rows if _ok(x.get('f62'))]
    zl = (sum(zl_valid) / 1e8) if zl_valid else None
    zl_src = 'em-∑成分' if zl_valid else None
    if zl is None and not pre:
        # ★ push2 的 f62 整列为空 → 用**全市场个股资金池**按成分求和兜底。
        #   （实测：BK0815 等"行情系板块"的成分 f62 全空，但个股在全市场报表里都有）
        _pool = stock_flow_pool()
        _vals = [_pool[str(x.get('f12'))] for x in rows if str(x.get('f12')) in _pool]
        if _vals:
            zl = sum(_vals) / 1e8
            zl_src = 'dce-∑成分'
    chg_sorted = sorted(chg)
    med = chg_sorted[len(chg_sorted) // 2]
    lim = [(x.get('f12'), x.get('f14'), chg_by.get(str(x.get('f12'))))
           for x in rows if (chg_by.get(str(x.get('f12'))) or 0) >= 9.7]
    return {'n': len(rows), 'up': sum(1 for c in chg if c > 0), 'zl_yi': zl,
            'zl_src': zl_src, 'median_chg': round(med, 2), 'limit_up': lim, 'rows': rows,
            'ok': True, 'src': src, 'n_valid': len(chg_by), 'chg_by': chg_by}


# ---------------------------------------------------------------- 5b. datacenter-web 降级通道
# 背景（2026-09-23 实测，P76）：`*.push2*.eastmoney.com` 对本机做的是**接口级 RST**，不是 IP 级封禁：
#   · 同一 IP、同一 SNI：`/`、`/api/qt/`、`/pub/x.js` 均返回 HTTP 404（通），
#     而真正取数的 `/api/qt/<任意>/get` 一律在 **0.05~0.16 秒内被 RST（0 字节）**；
#   · 换主机(82.push2/push2his/push2delay) / 换 ut / 换 HTTP 80 / 走代理 / 绕过代理 **全部无效**；
#   · 同 IP 下 `datacenter-web.eastmoney.com`、`quote.eastmoney.com` **完全正常**；
#   · 证书链验证为**真·东财证书**（DigiCert 签发 `*.eastmoney.com`）→ **不存在中间人**，
#     故"路径可区分"只能是服务端策略，与出口 IP 无关。
# 结论：push2 域整体不可用时，把 datacenter-web 作为资金面 / 板块面 / 板块归属的**降级源**。
#
# ⚠️⚠️ 口径铁律（P76）：datacenter 的 `RPT_CONCEPT_FUNDFLOW.NET_INFLOW` 是**东财板块级口径**，
#   与 `sector_stats()` 的「Σ成分 f62」**可同号也可反号**，**禁止交叉比较、禁止互相替代**。
#   实测同日锂电池概念：板块级 −38.62 亿 / Σ成分 +25.93 亿。所有降级返回值都带 `src`，调用方必须标注。
DCE_HOST = 'datacenter-web.eastmoney.com'
DCE_BASE = 'https://%s/api/data/v1/get?' % DCE_HOST

# datacenter **独立**熔断器：绝不能与 push2 共用计数，否则 push2 挂掉会把 datacenter 一起短接。
_DCE_BREAK = {'fails': 0, 'until': 0.0, 'tripped': False, 'short_circuited': 0}
DCE_BREAK_AFTER = 6
DCE_BREAK_COOLDOWN = 60.0


def dce_state():
    """datacenter 通道熔断器状态（与 push2 的 breaker_state() 相互独立）。"""
    s = dict(_DCE_BREAK)
    s['remain_s'] = max(0.0, round(_DCE_BREAK['until'] - time.time(), 1))
    return s


def dce_get(report, columns='ALL', flt=None, pz=50, page=1, sort=None, stype=-1,
            timeout=14, tries=3, enc='utf-8'):
    """datacenter-web 报表查询 → list[dict]（失败/空返回 []）。

    ⚠️ 与 `get()` 分开实现：`get()` 的熔断器是**全局限流**，push2 全灭时会把 datacenter
       的请求一并短接（这正是"一次故障拖垮所有源"的老问题）。此处独立计数。
    ⚠️ 报表名不存在与"查得到但今日无数据"必须分开报：前者是写错报表名（永远空），
       后者是正常情况。调用方按需区分 —— 本函数统一返回 []，用 `dce_probe()` 探名。
    """
    if _DCE_BREAK['until'] > time.time():
        _DCE_BREAK['short_circuited'] += 1
        return []
    q = ['reportName=%s' % report, 'columns=%s' % columns,
         'pageNumber=%d' % page, 'pageSize=%d' % pz]
    if flt:
        q.append('filter=' + urllib.parse.quote(flt, safe='()'))
    if sort:
        q.append('sortColumns=' + sort)
        q.append('sortTypes=%d' % stype)
    url = DCE_BASE + '&'.join(q)
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA})
            raw = urllib.request.urlopen(req, timeout=timeout, context=CTX).read().decode(enc, 'ignore')
            j = json.loads(raw)
            _DCE_BREAK['fails'] = 0
            if not j.get('success'):
                return []                      # 报表名错 / 今日无数据：不是网络故障，不计熔断
            return ((j.get('result') or {}).get('data')) or []
        except Exception as e:
            last = e
            time.sleep(0.8)
    _DCE_BREAK['fails'] += 1
    if _DCE_BREAK['fails'] >= DCE_BREAK_AFTER:
        _DCE_BREAK['tripped'] = True
        _DCE_BREAK['until'] = time.time() + DCE_BREAK_COOLDOWN
    return []


def dce_pages(report, flt=None):
    """报表总页数（用于判断"取全了没"）。0 表示报表名不存在或查询失败。"""
    q = ['reportName=%s' % report, 'columns=ALL', 'pageNumber=1', 'pageSize=1']
    if flt:
        q.append('filter=' + urllib.parse.quote(flt, safe='()'))
    try:
        req = urllib.request.Request(DCE_BASE + '&'.join(q), headers={'User-Agent': UA})
        j = json.loads(urllib.request.urlopen(req, timeout=14, context=CTX).read().decode('utf-8', 'ignore'))
        if not j.get('success'):
            return 0
        return ((j.get('result') or {}).get('pages')) or 0
    except Exception:
        return 0


def fund_flow_dc(code, days=1):
    """★ 降级：个股资金流（datacenter `RPT_DMSK_TS_STOCKNEW`）→ {代码: 字段dict}。

    字段**刻意对齐 `ulist()` 的 f 编码**，便于在 `ulist()` 内部无缝降级：
        f62 主力净额 = PRIME_INFLOW（实测 = 超大单净 + 大单净，口径可交叉验证 ✅）
        f66 超大单净 = SUPERDEAL_INFLOW − SUPERDEAL_OUTFLOW
        f72 大单净   = BIGDEAL_INFLOW − BIGDEAL_OUTFLOW
    额外字段（datacenter 独有，很有价值）：cost 主力成本 / cost20 / cost60 /
        org 机构参与度 / score 综合分 / rank 全市场排名 / focus 关注度。
    ⚠️ 本报表**无中单/小单**（f78/f84）→ 这两个键**不写入**，调用方 `_ok()` 判定为不可用。
    ⚠️ 只有日频、且是**当日收盘后入表**（15:05 前后）—— 盘中调用会拿到"上一交易日"。
    返回 {} 表示失败（**不得伪造 0**）。"""
    if isinstance(code, str):
        code = [code]
    out = {}
    for c in code:
        c = str(c)
        rows = dce_get('RPT_DMSK_TS_STOCKNEW',
                       flt='(SECURITY_CODE="%s")' % c, pz=max(1, days),
                       sort='TRADE_DATE', stype=-1)
        if not rows:
            continue
        r = rows[0]
        sup = _fl(r.get('SUPERDEAL_INFLOW')) - _fl(r.get('SUPERDEAL_OUTFLOW'))
        big = _fl(r.get('BIGDEAL_INFLOW')) - _fl(r.get('BIGDEAL_OUTFLOW'))
        prime = r.get('PRIME_INFLOW')
        rec = {
            'f12': c,
            'f14': r.get('SECURITY_NAME_ABBR'),
            'f62': prime if _ok(prime) else (sup + big if (sup or big) else None),
            'f66': sup,
            'f72': big,
            'f2': r.get('CLOSE_PRICE'),
            'f3': r.get('CHANGE_RATE'),
            'date': str(r.get('TRADE_DATE') or '')[:10],
            'cost': r.get('PRIME_COST'), 'cost20': r.get('PRIME_COST_20DAYS'),
            'cost60': r.get('PRIME_COST_60DAYS'), 'org': r.get('ORG_PARTICIPATE'),
            'score': r.get('TOTALSCORE'), 'rank': r.get('RANK'), 'focus': r.get('FOCUS'),
            'ratio': r.get('RATIO'), 'ratio3': r.get('RATIO_3DAYS'),
            'turnover': r.get('TURNOVERRATE'),
            '_src': 'dce', '_days': rows,
        }
        out[c] = rec
    return out


def fund_flow_days_dc(code, n=12):
    """★ 降级：个股资金流**历史序列**（近 n 个交易日）→ [(日期, 主力净额, 超大单净, 大单净), …] 升序。
    用途：替代 `fflow(code, klt=101)` 画"近 n 日主力净额"柱子图。"""
    rows = dce_get('RPT_DMSK_TS_STOCKNEW', flt='(SECURITY_CODE="%s")' % str(code),
                   pz=max(1, n), sort='TRADE_DATE', stype=-1)
    out = []
    for r in rows:
        sup = _fl(r.get('SUPERDEAL_INFLOW')) - _fl(r.get('SUPERDEAL_OUTFLOW'))
        big = _fl(r.get('BIGDEAL_INFLOW')) - _fl(r.get('BIGDEAL_OUTFLOW'))
        out.append((str(r.get('TRADE_DATE') or '')[:10],
                    _fl(r.get('PRIME_INFLOW')), sup, big))
    out.sort(key=lambda z: z[0])
    return out


def board_flow_dc(kind='concept', date=None, pz=600):
    """★ 降级：**板块级**资金流 → {bk: (名称, kind, 主力净额亿, 涨跌%)}。

    ⚠️ 口径 = **东财板块级**，与 `board_index()`（push2 clist 的 f62）同源可比值；
       但**绝不等于** `sector_stats()['zl_yi']`（那是 Σ成分 f62）—— 二者可反号，禁止混用。
    kind: 'concept'→RPT_CONCEPT_FUNDFLOW / 'industry'→RPT_INDUSTRY_FUNDFLOW
    date: 'YYYY-MM-DD'，None=取最新交易日（按 TRADE_DATE 降序第一页反查）。
    """
    rep = 'RPT_CONCEPT_FUNDFLOW' if kind == 'concept' else 'RPT_INDUSTRY_FUNDFLOW'
    if not date:
        probe = dce_get(rep, pz=1, sort='TRADE_DATE', stype=-1)
        if not probe:
            return {}
        date = str(probe[0].get('TRADE_DATE') or '')[:10]
    rows = dce_get(rep, flt="(TRADE_DATE='%s')" % date, pz=pz, sort='NET_INFLOW', stype=-1)
    out = {}
    for r in rows:
        bk = str(r.get('BOARD_CODE') or '')
        if not bk.startswith('BK'):
            bk = 'BK' + bk.zfill(4)
        out[bk] = (r.get('BOARD_NAME'), kind, _fl(r.get('NET_INFLOW')) / 1e8,
                   _fl(r.get('CHANGE_RATE')) if _ok(r.get('CHANGE_RATE')) else None,
                   {'sup': _fl(r.get('SUPERDEAL_NET')) / 1e8,
                    'big': _fl(r.get('BIGDEAL_NET')) / 1e8,
                    'mid': _fl(r.get('MIDDEAL_NET')) / 1e8,
                    'small': _fl(r.get('SMALLDEAL_NET')) / 1e8})
    return out


def board_of(code, pz=40):
    """★ 降级：个股**真实板块归属**（datacenter `RPT_F10_CORETHEME_BOARDTYPE`）。
    返回 [{'bk': 'BK0574', 'name': '锂电池概念', 'type': '行业'|'概念'|…, 'reason': 入选理由}]，按相关度排序。
    替代被封的 `slist()`；且**比 slist 更全**（多带 BOARD_TYPE / 入选理由）。
    ⚠️ 报表里的 BOARD_CODE 无 "BK" 前缀（如 "574"），此处统一补成 "BK0574" 以便与 push2 代码互通。"""
    rows = dce_get('RPT_F10_CORETHEME_BOARDTYPE', flt='(SECURITY_CODE="%s")' % str(code),
                   pz=pz, sort='BOARD_RANK', stype=1)
    out = []
    for r in rows:
        c = str(r.get('BOARD_CODE') or '')
        if not c:
            continue
        if not c.startswith('BK'):
            c = 'BK' + c.zfill(4)
        out.append({'bk': c, 'name': r.get('BOARD_NAME'),
                    'type': r.get('BOARD_TYPE'), 'level': r.get('BOARD_LEVEL'),
                    'reason': r.get('SELECTED_BOARD_REASON')})
    return out


def board_members_dc(bk, pz=500, max_pages=8):
    """★ 降级：板块**成分股名单**（反查 `RPT_F10_CORETHEME_BOARDTYPE`）。
    只给代码+名称（**不含行情**）—— 行情必须另走 `_snap_chg()`（腾讯批量），
    因为 datacenter 该报表没有个股当日涨跌幅字段。

    ⚠️ 必须 `pz>=500` + 翻页：实测"锂电池概念"有 **442** 个成分、"储能概念" **486** 个，
       而 `pz=200` 时服务端**静默只回前 200 条**（不报错、`success=True`），
       表现为"成分名单里找不到本股" → 进而把"板块内排名"算成"不在该板块"
       （与 P72「空页≠失败页」同族：**截断被伪装成"没有"**）。
    ⚠️ 返回 [] 时**不得**当成"板块空"—— 可能是报表限流，调用方须标"不可用"。"""
    c = str(bk)
    if c.startswith('BK'):
        c = c[2:].lstrip('0') or '0'
    out, seen = [], set()
    for pg in range(1, max_pages + 1):
        rows = dce_get('RPT_F10_CORETHEME_BOARDTYPE', flt='(BOARD_CODE="%s")' % c,
                       pz=pz, page=pg)
        if not rows:
            break
        for r in rows:
            cd = str(r.get('SECURITY_CODE') or '')
            if cd and cd not in seen:
                seen.add(cd)
                out.append({'f12': cd, 'f14': r.get('SECURITY_NAME_ABBR')})
        if len(rows) < pz:
            break
        time.sleep(0.15)
    return out


def moneyflow_sina(code, n=12):
    """★ 降级：新浪**资金流历史序列** → [(日期, 净流入额, 特大单净额, 涨跌幅%, 收盘, 换手率×100), …] **升序**。

    ⚠️ **第 6 个字段不是换手率本身，而是换手率的 100 倍**（等于「成交量(手) ÷ 流通股本(百万股)」）。
       实测核对（2026-10-09）：茅台 f=22.57 → 换手 0.226%；某小盘票 f=2440 → 换手 24.4%；
       且同一只票 `K线vol / f` 恒定（等于流通股本/百万股），证明它就是换手率×100。
       **要真换手率必须自行 /100** —— 本函数的调用方已各自处理并注明。
    用途：push2 `fflow/daykline` 不可用时，替代"近 n 日主力净额"柱状图。

    ⚠️ 口径是**新浪自己的**（`netamount`=净流入额、`r0_net`=特大单净额），
       **不等于东财 f52/f62** —— 图上必须标注 `src=sina`，**禁止与东财数值同图相加或直接比较**。
    ⚠️ `Referer` 必须是新浪自己的域名：给 `quote.eastmoney.com` 会 **403**（实测 2026-09-23）。
    ⚠️ 数据**当日收盘后入表**，盘中调用末条为上一交易日（与 P74 同族，须核对日期对齐）。"""
    sym = tsym(code)
    url = ('https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/'
           'MoneyFlow.ssl_qsfx_zjlrqs?page=1&num=%d&sort=opendate&asc=0&daima=%s' % (n, sym))
    raw = get(url, ref='https://finance.sina.com.cn/')
    if not raw:
        return []
    try:
        out = []
        for x in json.loads(raw):
            cr = x.get('changeratio')
            out.append((str(x.get('opendate') or ''),
                        _fl(x.get('netamount')), _fl(x.get('r0_net')),
                        round(_fl(cr) * 100, 2) if _ok(cr) else None,
                        _fl(x.get('trade')) if _ok(x.get('trade')) else None,
                        _fl(x.get('turnover')) if _ok(x.get('turnover')) else None))
        out.sort(key=lambda z: z[0])
        return out
    except Exception:
        return []


def flow_days_any(code, n=12):
    """近 n 日资金流 → `(rows, src)`，rows **升序**、统一 6 元组：
        **(日期, 主力净额, 超大单, 大单, 中单, 小单)**

    ⚠️ **v1.2.19 修顺序 BUG**：东财 `fflow` 的原生顺序是
       `(日期, 主力, 小单, 中单, 大单, 超大单)` —— **与报告表头正好相反**，
       旧实现原样返回，导致渲染层把「小单」当「超大单」显示、
       把「超大单」当「小单」显示（**列全错位**）。
       现统一转成**与产品表头一致**的顺序，调用方不需要再猜。

    ⚠️ **新浪降级源的字段归位**：新浪只有「特大单净额」→ 归到**超大单**列，
       中/大/小单**置 None（不填 0）**。旧实现把特大单塞进了"小单"列，语义完全错。
       ⚠️ 且新浪「特大单」与东财「超大单」**口径不可直接比较**，src 必须传到渲染层标注。

    ⚠️ 两源都失败 → `([], None)`，渲染层按"该表不可用"处理，**不得补 0**。"""
    ks = fflow(code, klt=101, lmt=n)
    if ks:
        # 东财原生 (d, 主力, 小, 中, 大, 超大) → 目标 (d, 主力, 超大, 大, 中, 小)
        return ([(str(x[0])[:10], x[1], x[5], x[4], x[3], x[2]) for x in ks][-n:], 'em')
    sn = moneyflow_sina(code, n)
    if sn:
        # 新浪 (d, 主力净额, 特大单净额, 涨跌幅, 收盘, 换手×100)
        #   → 特大单归「超大单」列，其余三列无数据置 None
        return ([(d, zl, r0, None, None, None) for d, zl, r0, _c, _p, _to in sn][-n:], 'sina')
    return ([], None)


# ---------------------------------------------------------------- 6. K线三源
def kline_em(code, lmt=260, klt=101, fqt=1):
    """东财 K 线。返回 [{d,o,c,h,l,v}]。⚠️ 盘中三主机可能同时限流(klines=[])→直接切 sina。"""
    url = (f'https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid(code)}'
           f'&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56&klt={klt}&fqt={fqt}'
           f'&end=20500101&lmt={lmt}&ut={UT}')
    r = get(url, tries=2)
    if not r:
        return []
    try:
        ks = (json.loads(r).get('data') or {}).get('klines') or []
        if not ks:
            return []
        return [{'d': p[0], 'o': _fl(p[1]), 'c': _fl(p[2]), 'h': _fl(p[3]),
                 'l': _fl(p[4]), 'v': _fl(p[5])} for p in (x.split(',') for x in ks)]
    except Exception:
        return []


def kline_sina(code, datalen=80):
    """新浪日K。⚠️ 末根是前一交易日，判当日位置需 append live 价。
    ⚠️ 新浪 volume 单位是【股】，已 /100 归一到【手】以对齐腾讯口径。返回 [{d,o,c,h,l,v}]。"""
    url = (f'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/'
           f'CN_MarketData.getKLineData?symbol={tsym(code)}&scale=240&ma=no&datalen={datalen}')
    r = get(url, ref='https://finance.sina.com.cn/')
    if not r:
        return []
    try:
        txt = re.sub(r'([{,])(\w+):', r'\1"\2":', r)      # JS 对象字面量 → JSON
        arr = json.loads(txt)
        return [{'d': x['day'], 'o': _fl(x['open']), 'c': _fl(x['close']),
                 'h': _fl(x['high']), 'l': _fl(x['low']), 'v': _fl(x.get('volume')) / 100}
                for x in arr]
    except Exception:
        return []


def kline_qq(code, n=800):
    """腾讯日K（回测首选，稳定）。返回 [{d,o,c,h,l,v}]，v 单位手（已对齐）。

    降级链（2026-09-16 实测）：
      1) fqkline/get?...,qfq   前复权  —— 实测可能返回 HTTP 501
      2) fqkline/get?...,hfq   后复权
      3) kline/kline?...,day   不复权  —— 实测 501 期间仍可用（800 根）
      4) 新浪 getKLineData               —— 兜底
    ⚠️ 走 3)/4) 时是不复权价，若区间内有分红送转，涨停价判定会有误差。
    """
    sym = tsym(code)
    for url in (
        f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{n},qfq',
        f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{n},hfq',
        f'https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param={sym},day,,,{n}',
    ):
        r = get(url, ref='https://gu.qq.com/')
        if not r:
            continue
        try:
            data = json.loads(r).get('data', {}).get(sym, {})
            arr = data.get('qfqday') or data.get('hfqday') or data.get('day') or []
            if arr:
                return [{'d': x[0], 'o': _fl(x[1]), 'c': _fl(x[2]), 'h': _fl(x[3]),
                         'l': _fl(x[4]), 'v': _fl(x[5])} for x in arr if len(x) >= 6]
        except Exception:
            continue
    return kline_sina(code, datalen=min(n, 1023))


def kline(code, n=260):
    """统一入口：东财 → 新浪 → 腾讯，逐级降级。
    返回 **二元组** `([{d,o,c,h,l,v}], 'eastmoney'|'sina'|'tencent')` —— 注意不是列表。

    ⚠️ 本接口是本项目**最容易误用**的一个（P55），因为用错时**不报错**：
       `charts.candlestick_svg(ds.kline(c))` 拿到的是 2 元素元组，长度检查通过，
       遍历时元素是 'eastmoney' 字符串 → 最终**静默渲染出一张空白图**；
       喂给 `indicators()` 则报 `string indices must be integers`（那个报错也看不出真因）。
       选哪个：
         · 只要 K 线数组 → `kline_list(code, n)`
         · 同时要数据源名 → `k, src = kline_src(code, n)`
    """
    k = kline_em(code, lmt=n)
    if k:
        return k, 'eastmoney'
    k = kline_sina(code, datalen=n)
    if k:
        return k, 'sina'
    return kline_qq(code, n=n), 'tencent'


# 语义别名：名字里点明"返回值带数据源名"，避免调用方以为它只给数组
kline_src = kline


def kline_list(code, n=260):
    """统一入口，**只返回 K 线数组** [{d,o,c,h,l,v}]（丢掉源名）。

    等价于 `kline(code, n)[0]`，但返回值类型不可能被误当成元组。
    画图 / 算指标一律用这个。
    """
    return kline(code, n)[0]


# ---------------------------------------------------------------- 7. 情绪温度
def mood(pre=False):
    """情绪温度三指标之一：昨日涨停/首板/连板的板指涨跌 + 主力净额。
    ⚠️ 东财 clist 盘前/限流时 f3/f62 返回 "-"，此时 zl_yi 必须置 None（而非 0），
       否则调用方会把"取数失败"误判成"资金净流入 → 接力环境健康"（把失败当利好，最危险的错法）。
    ⚠️ pre=True 时禁用实时字段（口径须与个股一致：都用上一交易日收盘）。"""
    out = {}
    rows = clist(fid='f3', fs='m:90+t:3', pages=1, fields='f12,f14,f3,f62')
    idx = {str(x.get('f12')): x for x in rows}
    for name, bk in EMOTION_BK.items():
        x = idx.get(bk)
        if x and _ok(x.get('f62')) and not pre:
            out[name] = {'chg': _fl(x.get('f3')) if _ok(x.get('f3')) else None,
                         'zl_yi': _fl(x.get('f62')) / 1e8, 'up': None, 'n': None}
        else:
            # 用成分合计兜底（成分名单仍来自东财，这部分有效）
            try:
                st = sector_stats(bk, pre=pre)
            except Exception:
                st = {'ok': False, 'zl_yi': None, 'up': 0, 'n': 0}
            # ⚠️ ok=False（含"快照非今日""成分行情取不到"）时 up/n 必须置 None：
            #   否则 st 里残留的 n=48/up=0 会被渲染层当成有效红盘率，印出「0/48（0%）」——
            #   把"没数据"伪装成"红盘率 0%"（与 P81 同族，方向相反）。
            _oks = bool(st.get('ok'))
            # ★ v1.2.21：**资金与行情要分开判断**。
            #   盘前 sector_stats 会因"实时行情不可用"返回 ok=False，
            #   但 `PRIME_INFLOW` 来自 datacenter（**上一交易日收盘后即有**）→ **盘前完全可用**。
            #   旧实现把两者绑在 ok 上，导致盘前连资金一起丢掉（明明能算却报"未取到"）。
            zl_ = st.get('zl_yi') if _oks else None
            zl_src_ = st.get('zl_src') if _oks else None
            if zl_ is None:
                _pool = stock_flow_pool()
                _v = [_pool[str(x.get('f12'))] for x in (st.get('rows') or [])
                      if str(x.get('f12')) in _pool]
                if _v:
                    zl_ = sum(_v) / 1e8
                    zl_src_ = 'dce-∑成分'
            out[name] = {'chg': (st.get('median_chg') if _oks else None),
                         'chg_src': ('成分中位数' if _oks else None),
                         'zl_yi': zl_,
                         'zl_src': zl_src_,
                         'up': (st.get('up') if _oks else None),
                         'n': (st.get('n') if _oks else None)}
    return out


# ---------------------------------------------------------------- 8. 技术指标
def ma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else float('nan')


def ema(vals, n):
    k = 2 / (n + 1); e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def rsi(vals, n=14):
    """RSI = 100*ag/(ag+al)，勿写补数。"""
    if len(vals) < n + 1:
        return float('nan')
    ag = al = 0.0
    for i in range(-n, 0):
        d = vals[i] - vals[i - 1]
        ag += max(d, 0.0); al += max(-d, 0.0)
    ag /= n; al /= n
    return 100 * ag / (ag + al) if (ag + al) > 0 else 50.0


def boll(vals, n=20, k=2):
    m = ma(vals, n)
    sd = math.sqrt(sum((v - m) ** 2 for v in vals[-n:]) / n) if len(vals) >= n else float('nan')
    return {'mid': m, 'up': m + k * sd, 'low': m - k * sd,
            'pctb': (vals[-1] - (m - k * sd)) / (2 * k * sd) * 100 if sd else float('nan')}


def macd(vals, f=12, s=26, sig=9):
    def ema_series(v, n):
        k = 2 / (n + 1); out = [v[0]]
        for x in v[1:]:
            out.append(x * k + out[-1] * (1 - k))
        return out
    ef, es = ema_series(vals, f), ema_series(vals, s)
    dif = [a - b for a, b in zip(ef, es)]
    dea = ema_series(dif, sig)
    return {'dif': dif[-1], 'dea': dea[-1], 'macd': 2 * (dif[-1] - dea[-1])}


def cci(h, l, c, n=20):
    if len(c) < n:
        return float('nan')
    tp = [(h[i] + l[i] + c[i]) / 3 for i in range(-n, 0)]
    m = sum(tp) / n
    md = sum(abs(x - m) for x in tp) / n
    return (tp[-1] - m) / (0.015 * md) if md else 0.0


def kdj(h, l, c, n=9, m1=3, m2=3):
    """标准 KDJ：K=2/3*K_prev+1/3*RSV, D=2/3*D_prev+1/3*K, J=3K-2D。"""
    if len(c) < n:
        return (float('nan'),) * 3
    K = D = 50.0
    for i in range(n, len(c) + 1):
        hh, ll = max(h[i - n:i]), min(l[i - n:i])
        rsv = (c[i - 1] - ll) / (hh - ll) * 100 if hh != ll else 50.0
        K = (m1 - 1) / m1 * K + 1 / m1 * rsv
        D = (m2 - 1) / m2 * D + 1 / m2 * K
    return K, D, 3 * K - 2 * D


def wr(h, l, c, n=14):
    if len(c) < n:
        return float('nan')
    hh, ll = max(h[-n:]), min(l[-n:])
    return (hh - c[-1]) / (hh - ll) * -100 if hh != ll else -50.0


def mom(c, n=10):
    return (c[-1] / c[-1 - n] - 1) * 100 if len(c) > n else float('nan')


def pos(c_highs, c_lows, live, n=60):
    """位置百分位：(live − n 日最低) / (n 日最高 − n 日最低) × 100。

    ⚠️ **序列长度不足 n 时返回 `None`（不可用），不再静默缩小窗口。**
    为什么（P84，2026-10-09 实测）：
      原实现是 `max(h[-n:])` —— 序列不足 n 时 Python 切片**静默退化为"全部可用"**，
      于是调用方写 `ds.kline(code, 130)` 却把它当"250 日位置"用，
      实测同一只票 **24.1%（130 日）vs 10.0%（真 250 日），差 14 个百分点**，
      而这个值还参与评分（`pos<35 → +2 分`）。
      **"少了就少算"是最难发现的一类错：不报错、数字自洽、只是悄悄错。**

    想同时拿到"差多少根"用 `pos_ex()`；调用方**必须自己处理 None**
    （P67：`None` 进 `%`/f-string 会直接崩）。
    """
    if not c_highs or not c_lows:
        return None
    if len(c_highs) < n or len(c_lows) < n:
        return None
    w_h, w_l = max(c_highs[-n:]), min(c_lows[-n:])
    return (live - w_l) / (w_h - w_l) * 100 if w_h != w_l else 50.0


def pos_ex(c_highs, c_lows, live, n=60):
    """带元信息的位置（推荐入口）：
    `{'pct': float|None, 'n_req': n, 'n_have': 可用根数, 'ok': bool}`。

    凡是要把位置印进报告 / 参与打分的调用方一律用它 ——
    这样"不可用"能被**如实标注**，而不是被一个缩了窗口的数字冒充。
    """
    nh = min(len(c_highs or []), len(c_lows or []))
    v = pos(c_highs, c_lows, live, n)
    return {'pct': v, 'n_req': n, 'n_have': nh, 'ok': v is not None}


def indicators(closes, highs=None, lows=None, live=None):
    """一次算全套。live 传当日实时价（拼入序列尾部）。

    ⚠️ `pos60`/`pos250` 在序列长度不足时返回 **`None`**（P84），调用方必须处理；
       同时回传 `pos60_ok`/`pos250_ok`/`n_hist`，便于如实标注「不可用（仅 N 根）」。
       要 250 日位置，喂进来的序列**至少要有 250 根**（建议 260~300）。
    """
    c = list(closes)
    h = list(highs) if highs else c
    l = list(lows) if lows else c
    n_hist = min(len(c), len(h), len(l))
    if live is not None:
        c.append(live); h.append(live); l.append(live)
    _p60 = pos_ex(h, l, c[-1], 60)
    _p250 = pos_ex(h, l, c[-1], 250)
    return {
        'ma5': ma(c, 5), 'ma10': ma(c, 10), 'ma20': ma(c, 20), 'ma60': ma(c, 60),
        'bull': (ma(c, 5) > ma(c, 10) > ma(c, 20) > ma(c, 60)) if len(c) >= 60 else None,
        'boll': boll(c), 'macd': macd(c),
        'rsi6': rsi(c, 6), 'rsi14': rsi(c, 14), 'rsi24': rsi(c, 24),
        'cci20': cci(h, l, c, 20), 'wr14': wr(h, l, c, 14), 'mom10': mom(c, 10),
        'pos60': _p60['pct'], 'pos250': _p250['pct'],
        'pos60_ok': _p60['ok'], 'pos250_ok': _p250['ok'], 'n_hist': n_hist,
    }


# ---------------------------------------------------------------- 8b. 标准量比
def _elapsed_min():
    """当日已开盘分钟数（09:30 起算，剔除 11:30~13:00 午休，上限 240）。0 = 盘前。"""
    t = time.localtime()
    m = t.tm_hour * 60 + t.tm_min
    if m < 570:
        return 0
    if m <= 690:
        return m - 570
    if m < 780:
        return 120
    if m <= 900:
        return 120 + (m - 780)
    return 240


def vol_ratio(code, live_vol=None, lb=None):
    """**标准「量比」**（行情软件口径，P84）：

        量比 = (当日累计成交量 ÷ 当日已开盘分钟数) ÷ (近 5 个【完整】交易日总量 ÷ 240)

    ★ 这就是行情软件里显示的那个"量比"。它和两种常见的自造口径都**不是一回事**：
      · 「当日累计 ÷ 20 日**全天**均量」—— 盘中必然 < 1，**它测的其实是"今天过了多少时间"**
        （实测 10:12 开盘 42 分钟，四只票一律 0.29~0.45；同刻真实量比是 2.53~3.82）；
      · 「近 3 日均量 ÷ 前 20 日均量」—— 含今日半日量，且窗口随 K 线**末根是不是今天**漂移。
      **名字叫"量比"就必须是量比，否则改名**（P84：擅自盗用行情软件术语 =
      主动制造一个永远对不上的对照物）。

    实现优先级：
      1) 传入 `lb`（腾讯快照 `f49`）→ **直接采信**，它就是该口径；
      2) 否则用 K 线自算，且**今日未走完的那根一律剔除**（否则把半日量当整日量）。
      盘前（elapsed=0）无法定义 → `ok=False`；收盘后 elapsed=240 → 退化为"全天量 ÷ 5 日均量"
      （即行情软件的收盘量比）。

    返回 `{'vr','src','elapsed_min','today_vol','avg5','ok','note'}`；**绝不返回 0 冒充失败**。
    """
    el = _elapsed_min()
    out = {'vr': None, 'src': None, 'elapsed_min': el, 'today_vol': None,
           'avg5': None, 'ok': False, 'note': ''}
    if el <= 0:
        out['note'] = '盘前（09:30 前）无当日成交量，量比不可用'
        return out
    if lb is not None:
        try:
            v = float(lb)
            if v > 0:
                out.update(vr=v, src='qq.lb', ok=True,
                           note='腾讯快照 f49（= 行情软件口径）')
                return out
        except (TypeError, ValueError):
            pass
    # 自算：必须剔除今日未走完的那根
    k = kline_list(code, 300)
    if not k:
        out['note'] = 'K 线取数失败'
        return out
    today = time.strftime('%Y-%m-%d')
    tb = k[-1] if k[-1]['d'] == today else None
    hist = k[:-1] if tb else k
    if len(hist) < 5:
        out['note'] = '完整交易日不足 5 天（仅 %d 天），量比不可用' % len(hist)
        return out
    tv = live_vol if live_vol is not None else (tb['v'] if tb else None)
    if not tv or tv <= 0:
        out['note'] = '无当日成交量（K 线末根为上一交易日且未传 live_vol）'
        return out
    avg5 = sum(x['v'] for x in hist[-5:]) / 5.0
    if avg5 <= 0:
        out['note'] = '近 5 完整日均量为 0'
        return out
    out.update(vr=(tv / el) / (avg5 / 240.0), src='self', today_vol=tv, avg5=avg5, ok=True,
               note='自算：(%.0f 手 ÷ %d 分) ÷ (%.0f 手 ÷ 240 分)' % (tv, el, avg5))
    return out


def vol_ratio_of(code, snap=None):
    """便捷封装：有快照就用快照的 `lb` 与 `vol`，否则退回 `vol_ratio()` 自算。"""
    if snap is None:
        snap = (snapshot([code]) or {}).get(code) or {}
    return vol_ratio(code, live_vol=snap.get('vol'), lb=snap.get('lb'))


# ---------------------------------------------------------------- 9. 财务时点数据（PIT）
def financials(code, page_size=40):
    """个股历史业绩报表（含**实际公告日**）——PIT 时点回测的基础。

    返回 list[dict]，按报告期倒序：
      {'report':'2026-06-30', 'notice':'2026-08-20', 'eps':0.075, 'revenue':1.42e9,
       'netprofit':..., 'roe':5.21, 'rev_yoy':..., 'np_yoy':..., 'bps':...,
       'ocf_ps':..., 'gross_margin':...}

    ⚠️ 为什么必须用 NOTICE_DATE 而不是 REPORTDATE（look-ahead bias）：
       2026 中报的"报告期"是 2026-06-30，但**实际公告日是 2026-08-20**。
       若回测在 07-15 用这份财报做决策，用的就是当时根本不存在的未来信息 ——
       这是任何带财务因子的回测高估收益的**第一大来源**。
    """
    url = ('https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_LICO_FN_CPD'
           '&columns=ALL&filter=(SECURITY_CODE=%22' + code + '%22)'
           '&sortColumns=REPORTDATE&sortTypes=-1&pageSize=' + str(page_size) + '&pageNumber=1'
           '&source=WEB&client=WEB')
    r = get(url, ref='https://data.eastmoney.com/')
    if not r:
        return []
    try:
        rows = (json.loads(r).get('result') or {}).get('data') or []
    except Exception:
        return []
    out = []
    for x in rows:
        rd, nd = x.get('REPORTDATE'), x.get('NOTICE_DATE')
        if not rd:
            continue
        out.append({
            'report': str(rd)[:10],
            'notice': str(nd)[:10] if nd else None,
            'eps': _fl(x.get('BASIC_EPS')),
            'revenue': _fl(x.get('TOTAL_OPERATE_INCOME')),
            'netprofit': _fl(x.get('PARENT_NETPROFIT')),
            'roe': _fl(x.get('WEIGHTAVG_ROE')),
            'rev_yoy': _fl(x.get('YSTZ')),
            'np_yoy': _fl(x.get('SJLTZ')),
            'bps': _fl(x.get('BPS')),
            'ocf_ps': _fl(x.get('MGJYXJJE')),
            'gross_margin': _fl(x.get('XSMLL')),
        })
    return out


def fundamentals_at(code, date, fin=None):
    """PIT 取值：返回 date（'YYYY-MM-DD'）当天**已公告**的最新一期财报。

    规则：notice ≤ date 的报告期中 report 最大者。
    公告日缺失时按「报告期 + 45 天」保守估计 —— **宁可晚用，绝不早用**（保守方向才安全）。
    """
    fin = fin if fin is not None else financials(code)
    if not fin:
        return None
    cands = []
    for f in fin:
        nd = f['notice']
        if not nd:
            try:
                import datetime as _dt
                nd = (_dt.date.fromisoformat(f['report']) + _dt.timedelta(days=45)).isoformat()
            except Exception:
                continue
        if nd <= date:
            cands.append(f)
    return max(cands, key=lambda f: f['report']) if cands else None


def announce_calendar(code, fin=None):
    """公告日历 [(公告日, 报告期)] —— 人工核对"某日当时能看到什么"用。"""
    fin = fin if fin is not None else financials(code)
    return sorted([(f['notice'], f['report']) for f in fin if f['notice']])


__all__ = ['snapshot', 'clist', 'ulist', 'fflow', 'fflow_at', 'slist', 'sector_members',
           'sector_stats', 'kline_em', 'kline_sina', 'kline_qq', 'kline', 'kline_src', 'kline_list', 'mood',
           'ma', 'ema', 'rsi', 'boll', 'macd', 'cci', 'kdj', 'wr', 'mom', 'pos',
           'indicators', 'allowed', 'secid', 'tsym', 'n100', 'get', 'UT', 'MARKET_MAIN',
           'financials', 'fundamentals_at', 'announce_calendar']


# ---------------------------------------------------------------- 10. 外部源扩充（v1.2.18）
# ★ 2026-10-09 实测接入三路**免费 / 无需 token / 纯 HTTP** 的新源：
#   ① 巨潮资讯 cninfo —— 官方公告与定期报告（**证监会指定披露平台**）
#   ② 东财 datacenter `RPT_F10_EH_EQUITY` —— 股本结构（**带公告日，天然 PIT**）
#   ③ 东财 datacenter `RPT_DMSK_FN_{BALANCE,INCOME,CASHFLOW}` —— 三大报表
#
# ⚠️ **实测淘汰的源（别再试）**：
#   · 雪球 xueqiu —— 只拿到 `acw_tc`、缺 `xq_a_token` 时**稳定 400**，需复杂 cookie 流程 → 不接入
#   · 同花顺 quantapi / iFinD —— **付费产品**（iFinD 年费 ≈2.88 万起）→ 不接入，保持零成本
#   · 同花顺 F10 页面 —— HTTP 200 但**内容是 JS 渲染**，HTML 里搜不到"总股本" → 不可解析
#   · 东财 `RPT_VALUEANALYSIS_DET` —— **报表名不存在**（返回成功但零行，属 P85 一族）

_CNINFO_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/126.0 Safari/537.36')


def announcements(code, n=20):
    """巨潮资讯（cninfo）公告 → `[(日期, 标题, 类型, PDF链接), …]` **倒序**（最新在前）。

    src = `cninfo`。**证监会指定的信息披露平台**，公告与财报的**官方原文**来源。

    ⚠️ **用途定位**：行情类问题**不要**用它（行情走腾讯/新浪/东财）。
      它的独特价值在**事件与口径** —— "某件事官方究竟哪天披露的""财报原文怎么写的"。
    ⚠️ 免费、无需 token；接口是 POST 表单且 **`Referer` 必填**（缺失会被拒）。
    ⚠️ **会限流**：实测连续请求十几次后，**连茅台都返回 0 条**，且间隔 25 秒重试 4 次仍未恢复。
       —— 现象是 **HTTP 200 + `totalAnnouncement=0`**，**静默**，与"这只票真的没有公告"长得一模一样。
       故**本函数只适合低频调用**（如出报告时查一次）；**不要放进循环**。
       要判断"是不是被限流"，用 `cninfo_alive()` 拿茅台当探针。
    """
    if not code:
        return []
    sym = tsym(code)
    if sym.startswith('sz'):
        mkt, stock = 'szse', '%s,gssz%s' % (code, code)
    elif sym.startswith('sh'):
        mkt, stock = 'sse', '%s,gssh%s' % (code, code)
    else:
        return []
    body = urllib.parse.urlencode({
        'stock': stock, 'tabName': 'fulltext', 'pageSize': str(n), 'pageNum': '1',
        'column': mkt, 'category': '', 'plate': '', 'seDate': '', 'searchkey': '',
        'secid': '', 'sortName': '', 'sortType': '', 'isHLtitle': 'true',
    }).encode()
    try:
        req = urllib.request.Request(
            'http://www.cninfo.com.cn/new/hisAnnouncement/query', data=body,
            headers={'User-Agent': _CNINFO_UA,
                     'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                     # ⚠️ **Referer 必须是这一条具体路径**：只给根域 `http://www.cninfo.com.cn/`
                     #   会 **HTTP 200 + 0 条**（静默拒绝）—— 又一次「成功但空」，
                     #   与 P85 一族同源（"没有"被伪装成"没有数据"）。实测 2026-10-09。
                     'Referer': 'http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice'})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode('utf-8', 'replace'))
    except Exception:
        return []
    out = []
    for a in (d.get('announcements') or []):
        ts = a.get('announcementTime')
        try:
            day = time.strftime('%Y-%m-%d', time.localtime(ts / 1000)) if ts else ''
        except Exception:
            day = ''
        url = ('http://static.cninfo.com.cn/' + a['adjunctUrl']) if a.get('adjunctUrl') else ''
        out.append((day, (a.get('announcementTitle') or '').strip(),
                    a.get('announcementTypeName') or '', url))
    return out


def cninfo_alive():
    """巨潮是否可用（拿**茅台**当探针 —— 它必然有几千条公告）。

    返回 `True` / `False`。

    ★ 存在的意义：巨潮限流时返回 **HTTP 200 + 0 条**，与"这只票真的没有公告"
      **完全同形**。不区分就会把"取数失败"当成"结论"（P85 一族）。
      任何依赖 `announcements()` 的输出，都应先用本函数确认通道可用。
    """
    rows = announcements('600519', 3)
    return len(rows) > 0


def equity_struct(code):
    """股本结构 → dict（**含公告日，天然 PIT**）；失败返回 `{}`。

    src = `em/datacenter:RPT_F10_EH_EQUITY`。
    关键字段：`listed`（流通 A 股）/ `total`（总股本）/ `free`（自由流通）/ `notice`（公告日）。

    ⚠️ **换手率应当用它算**：`换手% = 成交量(股) / listed × 100` ——
      比新浪资金流里那个「换手×100」字段**少一道单位换算**，可作**独立第二源交叉验证**。
    """
    rows = dce_get('RPT_F10_EH_EQUITY', pz=6, flt='(SECURITY_CODE="%s")' % code)
    if not rows:
        return {}
    r = max(rows, key=lambda x: x.get('END_DATE') or '')
    return {
        'listed': r.get('LISTED_A_SHARES'), 'total': r.get('TOTAL_SHARES'),
        'free': r.get('FREELIQCI_SHARES'), 'end': (r.get('END_DATE') or '')[:10],
        'notice': (r.get('NOTICE_DATE') or '')[:10],
        '_src': 'em/datacenter:RPT_F10_EH_EQUITY',
    }


def fin_statements(code):
    """三大报表关键字段 → dict（缺项为 `None`，**绝不填 0**）；失败返回 `{}`。

    src = `em/datacenter:RPT_DMSK_FN_*`。每张表**各有自己的 `NOTICE_DATE`** → 可做 PIT。
    """
    out = {}
    b = dce_get('RPT_DMSK_FN_BALANCE', pz=4, flt='(SECURITY_CODE="%s")' % code)
    if b:
        r = max(b, key=lambda x: x.get('REPORT_DATE') or '')
        out.update({'report': (r.get('REPORT_DATE') or '')[:10],
                    'notice': (r.get('NOTICE_DATE') or '')[:10],
                    'assets': r.get('TOTAL_ASSETS'), 'liab': r.get('TOTAL_LIABILITIES'),
                    'equity': r.get('TOTAL_EQUITY'), 'debt_ratio': r.get('DEBT_ASSET_RATIO'),
                    'current_ratio': r.get('CURRENT_RATIO'), 'inventory': r.get('INVENTORY'),
                    'acct_rece': r.get('ACCOUNTS_RECE'), 'cash': r.get('MONETARYFUNDS'),
                    'industry': r.get('INDUSTRY_NAME')})
    i = dce_get('RPT_DMSK_FN_INCOME', pz=4, flt='(SECURITY_CODE="%s")' % code)
    if i:
        r = max(i, key=lambda x: x.get('REPORT_DATE') or '')
        out['deduct_np'] = r.get('DEDUCT_PARENT_NETPROFIT')
        out['income_report'] = (r.get('REPORT_DATE') or '')[:10]
    c = dce_get('RPT_DMSK_FN_CASHFLOW', pz=4, flt='(SECURITY_CODE="%s")' % code)
    if c:
        r = max(c, key=lambda x: x.get('REPORT_DATE') or '')
        out['ocf'] = r.get('NETCASH_OPERATE')
        out['cash_report'] = (r.get('REPORT_DATE') or '')[:10]
    if out:
        out['_src'] = 'em/datacenter:RPT_DMSK_FN_*'
    return out


def turnover_rate(code, vol_shares, asof=None):
    """**真实换手率（%）** = 成交量(手) × 100 ÷ 流通股本 × 100。

    `vol_shares` 传**手**数（K 线的 `v` 字段单位）。`asof` 传日期时按 PIT 取当时可见股本。
    取不到股本 → 返回 `None`（**不猜、不填 0**）。
    """
    st = equity_struct(code)
    listed = st.get('listed')
    if not listed or not vol_shares:
        return None
    return vol_shares * 100.0 / listed * 100.0


if __name__ == '__main__':
    # 自检
    s = snapshot(['601169', '600000'])
    for c, v in s.items():
        print(f"{c} {v['name']} {v['price']} {v['chg']:+.2f}% 量比{v['lb']} "
              f"内{v['inner']}/外{v['outer']} 封死={v['sealed']}")
    print('slist(600000):', [x['name'] for x in slist('600000')][:8])
    k, src = kline('600000', 60)
    print(f'kline src={src} n={len(k)} 末根={k[-1] if k else None}')
    print('mood:', mood())
