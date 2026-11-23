"""筛分核对（原批 = 筛下批 + 筛上批）的核心算法测试。

产品判定与一个逐整数动态规划参考实现对拍：参考实现不依赖切割点归并，
直接在 0..maxc 每个整数上检查累计走廊与筛分关系。
"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core import (  # noqa: E402
    e_at,
    parse_table,
    s_at,
    screening_compatible,
)


def table_from_sizes(sizes, partition_edges):
    """把具体粒径序列按给定边界（各档 lo 序列，自动补末尾）生成一张档表。"""
    max_size = max(sizes)
    edges = sorted({0, *(e for e in partition_edges if e <= max_size)})
    edges = edges + [max_size + 1]
    lines = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1] - 1
        cnt = sum(1 for s in sizes if lo <= s <= hi)
        lines.append(f"{lo} {hi} {cnt}")
    return parse_table("\n".join(lines))


def brute_screening(feed, under, over, aperture):
    """独立参考实现：逐整数扫描，判断是否存在满足一次筛分关系的原批累计函数 A。

    对每个整数切割点 c，A(c)=「原批粒径 < c」的粒数。三批档表给出 A 的
    逐点区间；筛分关系给出 c<=aperture 时 A 即筛下累计、c>aperture 时
    筛下累计恒为 N_U、A-N_U 即筛上累计，且 A(aperture)=N_U。

    与产品实现（只取切割点并集、逐点贪心）不同，这里在 0..maxc 每个整数上
    穷举可达的 A 值集合；只用于小规模对拍。
    """
    all_tables = list(feed) + list(under) + list(over)
    if not all_tables or not feed or not under or not over:
        return False
    nf = feed[0].total
    nu = under[0].total
    no = over[0].total
    if nf != nu + no:
        return False
    maxc = max(t.cuts[-1] for t in all_tables)
    maxc = max(maxc, aperture + 1)

    def interval(tables, c):
        return max(s_at(t, c) for t in tables), min(e_at(t, c) for t in tables)

    # 对每个整数 c 检查可达集合。A 非递减且每步只受一个区间约束，故可达集合
    # 始终是区间 [p, q]。
    p = q = 0
    for c in range(0, maxc + 1):
        lf, uf = interval(feed, c)
        lu, uu = interval(under, c)
        lo, uo = interval(over, c)
        if c <= aperture:
            low = max(p, lf, lu)
            up = min(uf, uu)
            if lo != 0:
                return False
            if c == aperture:
                # 筛孔以下全部归筛下：A(aperture)=N_U，但须先落在各走廊交集内。
                if not (low <= nu <= up):
                    return False
                low = up = nu
        else:
            if not (lu <= nu <= uu):
                return False
            low = max(p, lf, nu + lo)
            up = min(uf, nu + uo)
        p, q = low, up
        if p > q or p < 0 or q > nf:
            return False
    return p == q == nf


def brute_by_particles(feed_sizes, under_sizes, over_sizes, aperture):
    """最朴素的独立参考：直接检查具体粒子是否满足一次筛分关系。

    三批粒子数守恒，筛下批每个粒径 < aperture、筛上批每个 >= aperture，
    且原批粒子多重集恰为两产物合并。仅用于小规模。
    """
    if len(feed_sizes) != len(under_sizes) + len(over_sizes):
        return False
    if any(s >= aperture for s in under_sizes):
        return False
    if any(s < aperture for s in over_sizes):
        return False
    return sorted(feed_sizes) == sorted(list(under_sizes) + list(over_sizes))


def brute_enumerate_cdf(feed, under, over, aperture):
    """完全独立的穷举参考：直接枚举非递减累计函数 A(c) 的每个可能取值。

    不借用切割点/贪心技巧，只用档表给出的逐点区间上界 U 与下界 S：
      A(0)=0, A(maxc)=N_F, A 非递减；
      c <= aperture：A(c) 同时落在原批与筛下批走廊内，且筛上批下界为 0；
      c == aperture：A(c)=N_U；
      c > aperture：A(c)-N_U 落在筛上批走廊内，且 N_U 落在筛下批走廊内。
    仅对很小的规模调用。
    """
    if not feed or not under or not over:
        return False
    nf, nu, no = feed[0].total, under[0].total, over[0].total
    if nf != nu + no:
        return False
    all_tables = list(feed) + list(under) + list(over)
    maxc = max(max(t.cuts[-1] for t in all_tables) + 1, aperture + 1)

    def lo_hi(tables, c):
        return max(s_at(t, c) for t in tables), min(e_at(t, c) for t in tables)

    def allowed(c, a):
        lf, uf = lo_hi(feed, c)
        lu, uu = lo_hi(under, c)
        lo, uo = lo_hi(over, c)
        if not (lf <= a <= uf):
            return False
        if c <= aperture:
            if not (lu <= a <= uu):
                return False
            if lo != 0:
                return False
            return c != aperture or a == nu
        if not (lu <= nu <= uu):
            return False
        return lo <= a - nu <= uo

    from functools import lru_cache

    @lru_cache(maxsize=None)
    def can(c, prev):
        if c == maxc:
            return prev == nf
        for a in range(prev, nf + 1):
            if allowed(c, a) and can(c + 1, a):
                return True
        return False

    return can(0, 0)


class TestScreeningHandCases(unittest.TestCase):
    def test_basic_accept(self):
        feed = [parse_table("0 199 1000")]
        under = [parse_table("0 99 600")]
        over = [parse_table("100 199 400")]
        ok, reason = screening_compatible(feed, under, over, 100)
        self.assertTrue(ok, reason)

    def test_coarse_feed_and_products_accept(self):
        # 三批都只给整段粗档，筛分关系在档内完全可满足
        feed = [parse_table("0 199 1000")]
        under = [parse_table("0 100 600")]
        over = [parse_table("0 199 400")]
        ok, reason = screening_compatible(feed, under, over, 100)
        self.assertTrue(ok, reason)

    def test_refined_tables_accept(self):
        feed = [parse_table("0 49 250\n50 199 750")]
        under = [parse_table("0 49 250\n50 99 350")]
        over = [parse_table("100 149 150\n150 199 250")]
        ok, reason = screening_compatible(feed, under, over, 100)
        self.assertTrue(ok, reason)

    def test_boundary_particle_goes_to_oversize(self):
        # 原批 [0,99] 500 + [100,199] 500：恰好 100 微米的粒子归筛上
        feed = [parse_table("0 99 500\n100 199 500")]
        under = [parse_table("0 99 500")]
        over = [parse_table("100 199 500")]
        ok, reason = screening_compatible(feed, under, over, 100)
        self.assertTrue(ok, reason)

    def test_undersize_contains_crossing_particle_rejected(self):
        # 筛下批档表 [100,109] 有粒子：筛下批只能含 <100 的粒子
        feed = [parse_table("0 199 1000")]
        under = [parse_table("0 99 600\n100 109 1")]
        over = [parse_table("100 199 399")]
        ok, reason = screening_compatible(feed, under, over, 100)
        self.assertFalse(ok)
        self.assertIn("100", reason)

    def test_oversize_contains_sub_aperture_particle_rejected(self):
        feed = [parse_table("0 199 1000")]
        under = [parse_table("0 99 600")]
        over = [parse_table("0 99 100\n100 199 300")]
        ok, reason = screening_compatible(feed, under, over, 100)
        self.assertFalse(ok)
        self.assertIn("筛上批", reason)

    def test_total_not_conserved(self):
        feed = [parse_table("0 199 1000")]
        under = [parse_table("0 99 600")]
        over = [parse_table("100 199 500")]
        ok, reason = screening_compatible(feed, under, over, 100)
        self.assertFalse(ok)
        self.assertIn("总粒数", reason)

    def test_feed_split_conflicts_with_undersize(self):
        # 原批档表把 <100 钉成 700，但筛下批只有 600 粒
        feed = [parse_table("0 99 700\n100 199 300")]
        under = [parse_table("0 99 600")]
        over = [parse_table("100 199 400")]
        ok, reason = screening_compatible(feed, under, over, 100)
        self.assertFalse(ok)
        self.assertIn("筛孔", reason)
        self.assertIn("600", reason)

    def test_undersize_total_beyond_corridor_rejected(self):
        # 筛下批总粒数 600，但档表在 100 微米处至多允许 500 粒 <100
        feed = [parse_table("0 199 1000")]
        under = [parse_table("0 99 500\n100 199 100")]
        over = [parse_table("100 199 400")]
        ok, reason = screening_compatible(feed, under, over, 100)
        self.assertFalse(ok)
        self.assertIn("筛孔", reason)

    def test_internal_role_conflict_named(self):
        # 筛下批两张档表彼此冲突：产品应指出“筛下批”
        feed = [parse_table("0 199 1000")]
        under = [
            parse_table("0 49 250\n50 99 350"),
            parse_table("0 49 400\n50 99 200"),
        ]
        over = [parse_table("100 199 400")]
        ok, reason = screening_compatible(feed, under, over, 100)
        self.assertFalse(ok)
        self.assertIn("筛下批", reason)
        self.assertIn("不相容", reason)

    def test_undersize_bin_spanning_aperture_all_below_is_realizable(self):
        # 筛下批档表 [0,100] 610 只是“0..100 有 610 粒”，可取全部 <100，仍可行。
        feed = [parse_table("0 199 1000")]
        under = [parse_table("0 100 610\n101 199 0")]
        over = [parse_table("100 199 390")]
        ok, reason = screening_compatible(feed, under, over, 100)
        self.assertTrue(ok, reason)
        self.assertTrue(brute_screening(feed, under, over, 100))

    def test_undersize_bin_forcing_particle_above_aperture_rejected(self):
        # 筛下批总粒数 611，但筛孔 100 处其自身档表只允许 610 粒 <100
        feed = [parse_table("0 199 1000")]
        under = [parse_table("0 100 610\n101 199 1")]
        over = [parse_table("100 199 389")]
        ok, _ = screening_compatible(feed, under, over, 100)
        self.assertFalse(ok)
        self.assertFalse(brute_screening(feed, under, over, 100))

    def test_oversize_with_only_above_aperture_ok(self):
        feed = [parse_table("0 199 1000")]
        under = [parse_table("0 99 600")]
        over = [parse_table("100 100 1\n101 199 399")]
        ok, reason = screening_compatible(feed, under, over, 100)
        self.assertTrue(ok, reason)

    def test_empty_role(self):
        feed = [parse_table("0 199 1000")]
        ok, reason = screening_compatible(feed, [], [parse_table("100 199 400")], 100)
        self.assertFalse(ok)
        self.assertIn("筛下批", reason)

    def test_bad_aperture(self):
        feed = [parse_table("0 199 1000")]
        under = [parse_table("0 99 600")]
        over = [parse_table("100 199 400")]
        for bad in (0, -1, 2.0, True, "100"):
            ok, _ = screening_compatible(feed, under, over, bad)
            self.assertFalse(ok, f"aperture={bad!r} 应被拒绝")


class TestScreeningFuzz(unittest.TestCase):
    def test_same_source_always_accepted(self):
        rng = random.Random(20261123)
        for trial in range(120):
            size_hi = rng.randint(6, 24)
            aperture = rng.randint(1, size_hi)
            sizes = [rng.randint(0, size_hi) for _ in range(rng.randint(2, 60))]
            # 保证筛下、筛上都不为空
            sizes.append(rng.randint(0, aperture - 1))
            sizes.append(rng.randint(aperture, size_hi))
            feed_sizes = sizes
            under_sizes = [s for s in sizes if s < aperture]
            over_sizes = [s for s in sizes if s >= aperture]

            def make_tables(subset, max_size):
                tables = []
                for _ in range(rng.randint(1, 3)):
                    max_size = max(max_size, max(subset))
                    k = min(rng.randint(0, 3), max_size)
                    edges = [0] + sorted(rng.sample(range(1, max_size + 1), k))
                    tables.append(table_from_sizes(subset, edges))
                return tables

            feed = make_tables(feed_sizes, size_hi)
            under = make_tables(under_sizes, max(aperture - 1, max(under_sizes)))
            over = make_tables(over_sizes, max(over_sizes))

            ok, reason = screening_compatible(feed, under, over, aperture)
            self.assertTrue(ok, f"trial {trial}: 同源三批被误拒：{reason}")
            self.assertTrue(brute_screening(feed, under, over, aperture), f"trial {trial}")

    def test_random_mutations_match_brute(self):
        rng = random.Random(7)
        accepted = rejected = 0
        for trial in range(120):
            size_hi = rng.randint(5, 16)
            aperture = rng.randint(1, size_hi)
            sizes = [rng.randint(0, size_hi) for _ in range(rng.randint(4, 25))]
            sizes.append(rng.randint(0, aperture - 1))
            sizes.append(rng.randint(aperture, size_hi))
            under_sizes = [s for s in sizes if s < aperture]
            over_sizes = [s for s in sizes if s >= aperture]
            feed = [table_from_sizes(sizes, [0, rng.randint(1, size_hi)])]
            under = [table_from_sizes(under_sizes, [0])]
            over = [table_from_sizes(over_sizes, [aperture])]

            self.assertTrue(brute_by_particles(sizes, under_sizes, over_sizes, aperture))
            self.assertTrue(brute_enumerate_cdf(feed, under, over, aperture))

            # 随机重排某一角色的档粒数（保持总粒数），制造可能冲突的档表
            role = rng.choice(["feed", "under", "over"])
            tables = {"feed": feed, "under": under, "over": over}[role]
            base = tables[0]
            counts = [b.count for b in base.bins]
            rng.shuffle(counts)
            if sum(counts) == 0:
                continue
            text = "\n".join(
                f"{b.lo} {b.hi} {c}" for b, c in zip(base.bins, counts)
            )
            mutated = parse_table(text)
            if role == "feed":
                feed = [mutated]
            elif role == "under":
                under = [mutated]
            else:
                over = [mutated]

            got, _ = screening_compatible(feed, under, over, aperture)
            want = brute_enumerate_cdf(feed, under, over, aperture)
            self.assertEqual(got, want, f"trial {trial} role={role}")
            if got:
                accepted += 1
            else:
                rejected += 1
        self.assertGreater(accepted, 0)
        self.assertGreater(rejected, 0)

    def test_random_unrelated_tables_match_brute(self):
        rng = random.Random(11)
        for _ in range(120):
            size_hi = 14
            aperture = rng.randint(1, size_hi)
            nf = rng.randint(2, 20)

            def random_tables(lo, hi, total):
                span = hi - lo
                k = min(rng.randint(1, 3), max(span, 1))
                edges = [lo] + sorted(rng.sample(range(lo + 1, hi + 1), k)) if span > 0 else [lo]
                edges.append(hi + 1)
                pool = total
                rows = []
                for i in range(len(edges) - 1):
                    c = rng.randint(0, pool) if i < len(edges) - 2 else pool
                    rows.append(f"{edges[i]} {edges[i + 1] - 1} {c}")
                    pool -= c
                return [parse_table("\n".join(rows))]

            feed = random_tables(0, size_hi, nf)
            nu = rng.randint(1, nf - 1)
            under = random_tables(0, aperture - 1, nu)
            over = random_tables(aperture, size_hi, nf - nu)
            if not under or not over:
                continue
            got, _ = screening_compatible(feed, under, over, aperture)
            want = brute_screening(feed, under, over, aperture)
            self.assertEqual(got, want)


if __name__ == "__main__":
    unittest.main(verbosity=2)
