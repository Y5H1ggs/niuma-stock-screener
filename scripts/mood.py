# -*- coding: utf-8 -*-
"""
mood.py — 情绪温度三指标（决定"今天该不该做接力"）

用法：
    python mood.py

输出：
    ① 昨日涨停/首板/连板 的板指涨跌 + 主力净额 → 判定"兑现型上涨"还是"健康接力"
    ② 连板梯队高度（全市场 2 板及以上家数）
    ③ 资金主线 TOP 板块 vs 涨停家数 TOP 板块 是否错位
"""
import time
from ds import clist, mood as _mood, kline_qq, MARKET_MAIN, _fl

CONCEPT_FS = 'm:90+t:3'
INDUSTRY_FS = 'm:90+t:2'


def trend_ladder(max_check=160):
    """全市场真实连板梯队：扫描今日涨停股，用 K 线数连板 streak。
    返回 [(code, name, streak, chg)]，streak>=2 为连板。"""
    ups = [x for x in clist(fid='f3', fs=MARKET_MAIN, pages=3, fields='f12,f14,f2,f3')
           if _fl(x.get('f3')) >= 9.5]
    ladder = []
    for x in ups[:max_check]:
        code = str(x['f12'])
        k = kline_qq(code, 30)
        if len(k) < 3:
            continue
        s = 0
        for i in range(len(k) - 1, 0, -1):
            if k[i - 1]['c'] > 0 and k[i]['c'] / k[i - 1]['c'] - 1 >= 0.095:
                s += 1
            else:
                break
        if s >= 2:
            ladder.append((code, x.get('f14'), s, _fl(x.get('f3'))))
        time.sleep(0.15)
    ladder.sort(key=lambda t: -t[2])
    return ladder


def main():
    print('=' * 96)
    print('一、昨日涨停 / 首板 / 连板 情绪温度')
    print('=' * 96)
    m = _mood()
    verdict = []
    for name, v in m.items():
        chg = v.get('chg')
        zl = v['zl_yi']
        ztxt = ('%+.2f亿' % zl) if zl is not None else '**取数失败**'
        if chg is None or zl is None:
            # 任一取数失败：如实标注，不猜测、不按 0 参与判读（P43/P58）
            print(f'  {name}: 板指 '
                  f'{("%+.2f%%" % chg) if chg is not None else "-"}  主力 {ztxt}')
            continue
        tag = '🔴兑现型上涨(价强钱撤)' if (chg > 0 and zl < 0) else ('🟢健康接力' if (chg > 0 and zl > 0) else '⚪弱势')
        print(f'  {name}: 板指 {chg:+.2f}%  主力 {zl:+.2f}亿  {tag}')
        verdict.append((chg > 0 and zl < 0))

    print('\n' + '=' * 96)
    print('二、连板梯队高度（真实扫描涨停股 streak）')
    print('=' * 96)
    ladder = trend_ladder()
    print(f'  今日 2 板及以上家数 = {len(ladder)} 只')
    if len(ladder) <= 3:
        print('  🔴 情绪偏冷（≤3 只）→ 打板胜率塌方，不宜重仓接力')
    elif len(ladder) >= 10:
        print('  🟢 情绪活跃（≥10 只）')
    else:
        print('  ⚪ 情绪中性')
    for code, nm, s, chg in ladder[:12]:
        print(f'    {code} {nm:<9} {s}板  {chg:+.2f}%')

    print('\n' + '=' * 96)
    print('三、资金主线 vs 涨停分布 是否错位')
    print('=' * 96)
    ind = clist(fid='f62', fs=INDUSTRY_FS, pages=1, fields='f12,f14,f3,f62')
    top_zl = sorted(ind, key=lambda x: -_fl(x.get('f62')))[:8]
    print('  【主力净额 TOP8 行业】')
    for x in top_zl:
        print(f"    {x.get('f14'):<12} 板指{x.get('f3')}%  主力{_fl(x.get('f62'))/1e8:+.2f}亿")

    print('\n  判读：把上面 TOP 板块，与"当日涨停家数 TOP 板块"对照。')
    print('  · 若涨停票集中在非主线小板块（林业/造纸/黄酒/橡胶/医疗…）= 零散轮动')
    print('  · 追这类涨停票 = 接脱离资金方向的板块（铁律1）')

    print('\n' + '=' * 96)
    if verdict and all(verdict):
        print('⚠️ 结论：昨日涨停系全线"价强钱撤" → 今日不追涨停接力，优先趋势/潜力口径。')
    else:
        print('结论：情绪温度见上，结合连板梯队高度综合判定。')


if __name__ == '__main__':
    main()
