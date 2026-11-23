"""演示用种子数据：清空并重建数据库后写入批次档表。

批次设计：
  B-ALPHA ：两张相容的不同分档表（同一组粒子的初报 + 细分补报）；
  B-BETA  ：单张宽档表，P90 界天然较宽，体现区间不确定性；
  B-GAMMA ：小批量，便于页面上肉眼核对见证频数。

筛分演示（筛孔 100 微米，原批 1000 粒 = 筛下 600 + 筛上 400）：
  S-FEED  ：原批，初始只有一张粗档表 [0,199] 1000，待现场补报细分；
  S-UNDER ：筛下批，全部 <100（两张相容档表，证明只看档也能核对）；
  S-OVER  ：筛上批，全部 >=100；
  S-OTHER ：同规格但本场演示不登记，用于验证“核对期间/之外的批仍可补报”。
种子**不预登记**核对单，留待网页上现场演示登记这一步。
另打印冲突/不守恒/整张单复核拒绝的示例，供手动验证。

用法：python -m scripts.seed [数据库路径]
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import compatible, parse_table, screening_compatible  # noqa: E402
from app.db import (  # noqa: E402
    connect,
    create_batch,
    get_batch_by_name,
    init_db,
    insert_table,
    load_batch_tables,
)

B_ALPHA_T1 = """lo hi count
0 99 400
100 199 500
"""

B_ALPHA_T2 = """lo hi count
0 49 100
50 99 300
100 149 400
150 199 100
"""

# 与 B-ALPHA 不相容的补报：[0,99] 只有 400 粒，却声称 [0,49] 就有 450 粒
B_ALPHA_BAD = """lo hi count
0 49 450
50 99 0
100 199 450
"""

B_BETA_T1 = """lo hi count
0 100 500
101 200 500
"""

B_GAMMA_T1 = """lo hi count
0 9 20
10 19 30
20 29 50
"""

S_FEED_T1 = """lo hi count
0 199 1000
"""

# 原批的合法细分补报（相容，且整张核对单仍满足筛分关系）
S_FEED_T2 = """lo hi count
0 49 250
50 99 350
100 199 400
"""

# 与原批已入库的粗档表相容（粗档表对筛下分布没有约束），但把 [0,99] 的粒数
# 钉成 400（<100 只有 400），与筛下批的 600 冲突 —— 补报时整张核对单复核应拒绝。
S_FEED_BAD = """lo hi count
0 99 400
100 199 600
"""


S_UNDER_T1 = """lo hi count
0 99 600
"""

S_UNDER_T2 = """lo hi count
0 49 250
50 99 350
"""

S_OVER_T1 = """lo hi count
100 149 150
150 199 250
"""

# 筛上批被误做成跨过筛孔（含 <100 粒子），登记时应被拒绝
S_OVER_BAD = """lo hi count
0 99 100
100 199 300
"""

# 筛下批被误做成含 >=100 粒子（总粒数仍 600），与筛分关系冲突
S_UNDER_BAD = """lo hi count
0 99 500
100 199 100
"""


def _add_batch(conn, name, texts, note):
    batch_id = create_batch(conn, name)
    for i, text in enumerate(texts):
        table = parse_table(text)
        if i > 0:
            existing = load_batch_tables(conn, batch_id)
            ok, reason = compatible(existing + [table])
            if not ok:
                raise RuntimeError(f"种子数据自相矛盾：{name} {reason}")
        insert_table(conn, batch_id, table, f"{note} #{i + 1}")
    print(f"已写入 {name}：{len(texts)} 张档表")
    return batch_id


def main(db_path: str) -> None:
    if os.path.exists(db_path):
        os.remove(db_path)
    init_db(db_path)
    with connect(db_path) as conn:
        _add_batch(conn, "B-ALPHA", [B_ALPHA_T1, B_ALPHA_T2], "初报+细分补报（相容）")
        _add_batch(conn, "B-BETA", [B_BETA_T1], "宽档单表")
        _add_batch(conn, "B-GAMMA", [B_GAMMA_T1], "小批量")

        feed_id = _add_batch(conn, "S-FEED", [S_FEED_T1], "原批（粗档，尚未细分）")
        under_id = _add_batch(conn, "S-UNDER", [S_UNDER_T1, S_UNDER_T2], "筛下批")
        over_id = _add_batch(conn, "S-OVER", [S_OVER_T1], "筛上批")
        _add_batch(conn, "S-OTHER", [B_GAMMA_T1], "同规格但不在核对单上")

        feed = load_batch_tables(conn, feed_id)
        under = load_batch_tables(conn, under_id)
        over = load_batch_tables(conn, over_id)
        ok, reason = screening_compatible(feed, under, over, 100)
        if not ok:
            raise RuntimeError(f"种子筛分三批自相矛盾：{reason}")
        # 种子不预登记核对单，留待网页上现场演示“登记”这一步。
        print("S-FEED / S-UNDER / S-OVER 可按筛孔 100 微米现场登记核对单")

    # 验证示例确实会被拒绝
    bad_over = parse_table(S_OVER_BAD)
    bad_under = parse_table(S_UNDER_BAD)
    bad_feed = parse_table(S_FEED_BAD)
    with connect(db_path) as conn:
        feed = load_batch_tables(conn, int(get_batch_by_name(conn, "S-FEED")["id"]))
        under = load_batch_tables(conn, int(get_batch_by_name(conn, "S-UNDER")["id"]))
        over = load_batch_tables(conn, int(get_batch_by_name(conn, "S-OVER")["id"]))
    ok1, reason1 = screening_compatible(feed, under, [bad_over], 100)
    ok2, reason2 = screening_compatible(feed, [bad_under], over, 100)
    ok3, reason3 = screening_compatible(feed + [bad_feed], under, over, 100)
    ok4 = compatible(feed + [bad_feed])[0]
    ok5, _ = screening_compatible(feed + [parse_table(S_FEED_T2)], under, over, 100)
    assert not ok1 and not ok2 and not ok3, "冲突示例应被拒绝"
    assert ok4, "补报的跨筛孔档表本身应与原批已入库档表相容"
    assert ok5, "原批的合法细分补报应通过整张单复核"
    print("\n网页演示步骤：")
    print("1. 对 B-ALPHA 粘贴以下补报，应被拒绝且不入库：")
    print(B_ALPHA_BAD)
    print(f"   拒绝原因：{compatible([parse_table(B_ALPHA_T1), parse_table(B_ALPHA_T2), parse_table(B_ALPHA_BAD)])[1]}")
    print("2. 登记核对单：S-FEED（原批）/ S-UNDER（筛下批）/ S-OVER（筛上批），筛孔 100；")
    print("   若选错批次，会给出冲突原因且不写入，例如：")
    print(f"   筛上批越界：{reason1}")
    print(f"   筛下批越界：{reason2}")
    print("3. 对 S-FEED 补报以下细分表（与该批已入库档表相容，且整张单仍成立）：")
    print(S_FEED_T2)
    print("4. 再对 S-FEED 补报以下跨筛孔档表（与该批已入库档表相容，但与筛下批冲突）：")
    print(S_FEED_BAD)
    print("   整张核对单复核不通过，应被拒绝且不入库：")
    print(f"   {reason3}")
    print("5. 合并核算：勾选 S-UNDER + S-OVER 可以核算；勾选 S-FEED + 任一产物会被拦下；")
    print("   只勾 S-FEED 或 S-FEED + 未登记的 S-OTHER 可以核算。")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "powder.db"
    )
    main(path)
