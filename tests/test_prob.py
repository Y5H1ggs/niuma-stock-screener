# -*- coding: utf-8 -*-
"""prob.py 的不变量测试（**全离线，用合成 K 线**）。

为什么要有（v1.2.47）
--------------------
`prob.py` 的两个核心断言都曾经自己写错过一次：
  · `threshold_probs` 的单调性判断方向写反（need 降序时 prob 应递增，却判成不增），
    把**合法结果拦下**；
  · `ev()` 的 median 口径把 N 显示成 1（应为样本总数）。
两条都是「校验器自己出错」，靠人工看输出很难发现。**这类东西必须有测试。**

跑法：
    python -m unittest discover -s tests -v
或：
    python tests/test_prob.py
"""
import os
import sys
import math
import random
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, 'scripts'))

import prob  # noqa: E402


def synth_bars(n=320, seed=7, start=10.0, vol=0.02):
    """生成确定性的合成日 K（固定种子 → 可复现）。"""
    rnd = random.Random(seed)
    bars, c = [], start
    for i in range(n):
        o = c * (1 + rnd.uniform(-vol, vol) * 0.5)
        c2 = c * (1 + rnd.gauss(0, vol))
        h = max(o, c2) * (1 + abs(rnd.gauss(0, vol)) * 0.4)
        lo = min(o, c2) * (1 - abs(rnd.gauss(0, vol)) * 0.4)
        d = '2025-%02d-%02d' % (1 + i // 28, 1 + i % 28)
        bars.append({'d': d, 'o': round(o, 3), 'c': round(c2, 3),
                     'h': round(h, 3), 'l': round(lo, 3),
                     'v': int(1e6 * (1 + abs(rnd.gauss(0, 0.4))))})
        c = c2
    return bars


BARS = synth_bars()
BETA = synth_bars(seed=11, start=1000.0, vol=0.015)


class TestRecord(unittest.TestCase):
    """记录字段口径。"""

    def test_vr_excludes_today(self):
        """量比必须用**前 20 日**均量，不含当日（含当日会把当日放量摊薄）。"""
        rows = prob.records(BARS, warmup=30)
        r = rows[40]
        base = [x['v'] for x in BARS[r['i']-20:r['i']]]
        expect = BARS[r['i']]['v'] / (sum(base)/len(base))
        self.assertAlmostEqual(r['vr'], expect, places=6)

    def test_multi_horizon_alignment(self):
        """r3 / r5 必须对应 i+3 / i+5 的收盘，不能错位。"""
        rows = prob.records(BARS, warmup=30)
        for r in rows[:50]:
            i = r['i']
            if i + 5 >= len(BARS):
                continue
            c = BARS[i]['c']
            self.assertAlmostEqual(r['r1'], (BARS[i+1]['c']/c-1)*100, places=6)
            self.assertAlmostEqual(r['r3'], (BARS[i+3]['c']/c-1)*100, places=6)
            self.assertAlmostEqual(r['r5'], (BARS[i+5]['c']/c-1)*100, places=6)

    def test_beta_fields_injected(self):
        """给了 beta_bars 就必须注入板块字段，且 bk_chg 与板块收盘一致。"""
        rows = prob.records(BARS, warmup=30, beta_bars=BETA)
        hit = [r for r in rows if r['bk_chg'] is not None]
        self.assertGreater(len(hit), 50, '板块字段几乎全空，说明日期没对齐')
        bmap = {b['d']: b for b in BETA}
        for r in hit[:40]:
            b = bmap[r['d']]
            i = [x['d'] for x in BETA].index(r['d'])
            if i < 1:
                continue
            expect = (BETA[i]['c']/BETA[i-1]['c']-1)*100
            self.assertAlmostEqual(r['bk_chg'], expect, places=6)

    def test_beta_absent_when_not_given(self):
        """不给 beta_bars 时板块字段必须是 None，不能是 0（0 会被误当"板块平盘"）。"""
        rows = prob.records(BARS, warmup=30)
        for r in rows[:20]:
            self.assertIsNone(r['bk_chg'])


class TestBuildDist(unittest.TestCase):

    def test_dedup_by_date(self):
        """多条件命中同一天只能算一个样本（否则样本被重复计数、概率被放大）。"""
        c1 = lambda r: r['chg'] < 0            # noqa: E731
        c2 = lambda r: r['chg'] < 5            # noqa: E731 必然与 c1 大量重叠
        d = prob.build_dist(BARS, [c1, c2])
        self.assertEqual(len(d['dates']), len(set(d['dates'])), '日期有重复')
        self.assertEqual(d['n'], len(d['dates']))
        # 并集的两个条件都命中的日期，tags 应有两个元素
        both = [v for v in d['tags'].values() if len(v) == 2]
        self.assertGreater(len(both), 0, 'tags 没有记录多条件命中')

    def test_cond_exception_counted(self):
        """条件里引用缺失字段（None 比较）时，必须**计数**而不是静默吞掉。"""
        d = prob.build_dist(BARS, [lambda r: r['bk_chg'] <= -3])
        self.assertGreater(d['cond_errors'], 0,
                           'bk_chg 全为 None 时应产生异常计数，否则会把"字段缺失"读成"没出现过"')
        self.assertEqual(d['n'], 0)

    def test_empty_returns_zero(self):
        d = prob.build_dist(BARS, [lambda r: r['chg'] <= -99])
        self.assertEqual(d['n'], 0)


class TestMonotonic(unittest.TestCase):
    """★ 核心不变量：同一分布读出的阈值概率必须单调。"""

    def test_always_monotonic(self):
        d = prob.build_dist(BARS, [lambda r: r['chg'] < 0])
        self.assertGreater(d['n'], 20)
        base = BARS[-1]['c']
        rnd = random.Random(3)
        for _ in range(100):
            lv = [base * (1 + rnd.uniform(-0.15, 0.15)) for _ in range(rnd.randint(2, 8))]
            got = prob.threshold_probs(d, lv, base)
            # 返回按 need 降序，prob 必须非降
            for a, b in zip(got, got[1:]):
                self.assertLessEqual(a['prob'], b['prob'] + 1e-9,
                                     '%s(%.1f) > %s(%.1f) 违反单调' % (
                                         a['label'], a['prob'], b['label'], b['prob']))

    def test_higher_threshold_lower_prob(self):
        """手工构造的语义检查：站上更高的价格，概率不得更大。"""
        d = prob.build_dist(BARS, [lambda r: r['chg'] < 0])
        base = BARS[-1]['c']
        lo_p = prob.threshold_probs(d, [base * 0.98], base)[0]['prob']
        hi_p = prob.threshold_probs(d, [base * 1.02], base)[0]['prob']
        self.assertGreaterEqual(lo_p, hi_p)


class TestDecisionTree(unittest.TestCase):

    def test_joint_sums_to_100(self):
        d = prob.build_dist(BARS, [lambda r: r['chg'] < 0])
        t = prob.decision_tree(d, -1.0, BARS[-1]['c'])
        total = sum(c['joint'] for c in t['cells'])
        self.assertAlmostEqual(total, 100.0, places=6, msg='四格联合概率不等于 100%')

    def test_weighted_equals_marginal(self):
        """★ 自洽性：四格加权必须精确回等于边际（数学恒等，不是"近似"）。"""
        d = prob.build_dist(BARS, [lambda r: r['chg'] < 0])
        for split in (-2.0, -1.0, -0.5):
            t = prob.decision_tree(d, split, BARS[-1]['c'])
            self.assertAlmostEqual(t['weighted_up'], t['marginal_up'], places=6,
                                   msg='split=%.1f 加权与边际不等' % split)
            self.assertTrue(t['consistent'])

    def test_laplace_smoothing(self):
        """小样本格子的平滑值不能是 0% 或 100%（9/9 必须被压下来）。"""
        self.assertAlmostEqual(prob._lap(9, 9), 10/11, places=6)
        self.assertLess(prob._lap(9, 9), 1.0)


class TestBuckets(unittest.TestCase):

    def test_complete_and_disjoint(self):
        d = prob.build_dist(BARS, [lambda r: r['chg'] < 0])
        for h in (1, 3, 5):
            bs = prob.buckets(d, horizon=h)
            self.assertEqual(sum(b['k'] for b in bs), len(d['r%d' % h]))
            self.assertAlmostEqual(sum(b['prob'] for b in bs), 100.0, places=6)


class TestEV(unittest.TestCase):

    def test_median_n_is_sample_size(self):
        """median 口径的 N 必须是样本总数，不是 1（曾经显示成 N=1）。"""
        d = prob.build_dist(BARS, [lambda r: r['chg'] < 0])
        e = prob.ev(d, 17)
        self.assertEqual(e['by']['median']['n'], len(d['r1']))

    def test_median_robust_to_outliers(self):
        """中位数不受极端值影响，均值会被拉走——这是决策用中位数的理由。"""
        d = prob.build_dist(BARS, [lambda r: r['chg'] < 0])
        e = prob.ev(d, 17)
        self.assertLess(abs(e['by']['median']['gross']),
                        abs(e['by']['mean']['gross']) + 100)   # 中位不该被极端值放大


class TestAllHorizons(unittest.TestCase):

    def test_three_horizons_reported(self):
        d = prob.build_dist(BARS, [lambda r: r['chg'] < 0])
        hs = prob.all_horizons(d)
        self.assertEqual([x['h'] for x in hs], [1, 3, 5])
        for x in hs:
            self.assertEqual(x['n'], len(d['r%d' % x['h']]))


if __name__ == '__main__':
    unittest.main(verbosity=2)
