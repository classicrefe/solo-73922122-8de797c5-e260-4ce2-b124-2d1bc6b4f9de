"""Flask 网页端到端测试：上传、相容/不相容补报、筛分核对单、合并核算、见证展示。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db import connect  # noqa: E402
from app.web import create_app  # noqa: E402

ALPHA_T1 = "0 99 400\n100 199 500\n"
ALPHA_T2 = "0 49 100\n50 99 300\n100 149 400\n150 199 100\n"
ALPHA_BAD = "0 49 450\n50 99 0\n100 199 450\n"
BETA_T1 = "0 100 500\n101 200 500\n"
GAMMA_T1 = "0 9 20\n10 19 30\n20 29 50\n"

FEED_T1 = "0 199 1000\n"
FEED_T2 = "0 49 250\n50 199 750\n"
FEED_BAD = "0 99 400\n100 199 600\n"   # 与原批已入库档表相容，但与筛下批 600 冲突
UNDER_T1 = "0 99 600\n"
UNDER_T2 = "0 49 250\n50 99 350\n"
UNDER_BAD = "0 99 500\n100 199 100\n"  # 筛下批含 >=100 粒子
OVER_T1 = "100 149 150\n150 199 250\n"
OVER_BAD = "0 99 100\n100 199 300\n"   # 筛上批含 <100 粒子
SPARE_T1 = "0 9 20\n10 19 30\n20 29 50\n"


class WebFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        os.unlink(self.tmp.name)
        self.app = create_app(db_path=self.tmp.name)
        self.client = self.app.test_client()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _upload(self, name, text, note=""):
        return self.client.post(
            "/upload",
            data={"batch_name": name, "table_text": text, "note": note},
            follow_redirects=True,
        )

    def _sheet(self, feed, under, over, aperture="100"):
        return self.client.post(
            "/screening",
            data={
                "feed_batch": str(feed),
                "undersize_batch": str(under),
                "oversize_batch": str(over),
                "aperture": str(aperture),
            },
            follow_redirects=True,
        )

    def _batch_id(self, name):
        with connect(self.tmp.name) as conn:
            row = conn.execute("SELECT id FROM batches WHERE name = ?", (name,)).fetchone()
        return int(row["id"]) if row else None

    def _table_count(self, name):
        with connect(self.tmp.name) as conn:
            row = conn.execute(
                """SELECT COUNT(*) FROM tables t JOIN batches b ON b.id = t.batch_id
                    WHERE b.name = ?""",
                (name,),
            ).fetchone()
        return int(row[0])

    def _setup_screening_batches(self):
        for name, text in [
            ("S-FEED", FEED_T1),
            ("S-UNDER", UNDER_T1),
            ("S-OVER", OVER_T1),
            ("S-SPARE", SPARE_T1),
        ]:
            self._upload(name, text)
        return (
            self._batch_id("S-FEED"),
            self._batch_id("S-UNDER"),
            self._batch_id("S-OVER"),
            self._batch_id("S-SPARE"),
        )

    def test_full_flow(self):
        # 首页可打开
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("粒径档表 P90", html)

        # 三个批次首次上传
        for name, text in [("B-ALPHA", ALPHA_T1), ("B-BETA", BETA_T1), ("B-GAMMA", GAMMA_T1)]:
            rv = self._upload(name, text)
            self.assertEqual(rv.status_code, 200)
            self.assertIn("入库", rv.get_data(as_text=True))

        # 相容补报成功
        rv = self._upload("B-ALPHA", ALPHA_T2, note="细分补报")
        body = rv.get_data(as_text=True)
        self.assertIn("补报成功", body)
        self.assertIn("现共 2 张档表", body)

        # 不相容补报被拒绝，且档表数不增加
        rv = self._upload("B-ALPHA", ALPHA_BAD)
        body = rv.get_data(as_text=True)
        self.assertIn("补报被拒绝", body)
        self.assertIn("未入库", body)

        # 批次详情仍只有 2 张档表
        with self.app.app_context():
            from app.db import connect, list_tables
            with connect(self.tmp.name) as conn:
                from app.db import get_batch_by_name
                bid = int(get_batch_by_name(conn, "B-ALPHA")["id"])
                self.assertEqual(len(list_tables(conn, bid)), 2)
        detail = self.client.get(f"/batch/{bid}").get_data(as_text=True)
        self.assertIn("档表 #", detail)
        self.assertEqual(detail.count("900 粒"), 2)  # 两张已入库档表，总粒数均为 900
        self.assertIn("[0, 49]", detail)  # 第二张细分档表确实在库

        # 解析失败不入库
        rv = self._upload("B-BAD", "0 9 5\n11 19 3\n")
        self.assertIn("解析失败", rv.get_data(as_text=True))

        # 勾选三批核算
        rv = self.client.post("/compute", data={"batch_ids": [1, 2, 3]}, follow_redirects=True)
        self.assertEqual(rv.status_code, 200)
        body = rv.get_data(as_text=True)
        self.assertIn("合并 P90 核算结果", body)
        # 总粒数 900+1000+100 = 2000，k=1800
        self.assertIn("2000", body)
        self.assertIn("1800", body)
        self.assertIn("下界见证", body)
        self.assertIn("上界见证", body)
        self.assertIn("B-ALPHA", body)
        # 端点以“微米”展示
        self.assertRegex(body, r"最小可能值.*微米")

    def test_compute_requires_selection(self):
        self._upload("B-ALPHA", ALPHA_T1)
        rv = self.client.post("/compute", data={}, follow_redirects=True)
        self.assertIn("请至少勾选一个批次", rv.get_data(as_text=True))

    def test_invalid_upload_messages(self):
        rv = self._upload("X", "0 9 0\n10 19 0\n")
        self.assertIn("整表粒数为 0", rv.get_data(as_text=True))
        rv = self.client.post(
            "/upload", data={"batch_name": "", "table_text": "0 9 1"}, follow_redirects=True
        )
        self.assertIn("批号不能为空", rv.get_data(as_text=True))

    # ------------------------------------------------------------------
    # 筛分核对单
    # ------------------------------------------------------------------
    def test_register_screening_accept(self):
        feed, under, over, _ = self._setup_screening_batches()
        body = self._sheet(feed, under, over).get_data(as_text=True)
        self.assertIn("登记成功", body)
        self.assertIn("筛孔 100 微米", body)
        # 首页展示核对单
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("已登记核对单", html)
        self.assertIn("S-FEED", html)
        # 批次详情显示角色
        detail = self.client.get(f"/batch/{feed}").get_data(as_text=True)
        self.assertIn("原批 · 核对单", detail)

    def test_register_screening_requires_three_distinct_batches(self):
        feed, under, over, _ = self._setup_screening_batches()
        body = self._sheet(feed, under, under).get_data(as_text=True)
        self.assertIn("三个不同", body)
        body = self._sheet(feed, feed, over).get_data(as_text=True)
        self.assertIn("三个不同", body)
        with connect(self.tmp.name) as conn:
            n = conn.execute("SELECT COUNT(*) FROM screening_sheets").fetchone()[0]
        self.assertEqual(n, 0)

    def test_register_screening_rejects_conflicts_without_writing(self):
        feed, under, over, spare = self._setup_screening_batches()
        body = self._sheet(feed, under, over).get_data(as_text=True)
        self.assertIn("登记成功", body)
        # 同一角色换一个未登记批再登记一次：原批/筛下批/筛上批仍会被拦
        body = self._sheet(feed, spare, over).get_data(as_text=True)
        self.assertIn("已登记", body)
        body = self._sheet(spare, under, over).get_data(as_text=True)
        self.assertIn("已登记", body)
        with connect(self.tmp.name) as conn:
            n = conn.execute("SELECT COUNT(*) FROM screening_sheets").fetchone()[0]
        self.assertEqual(n, 1)

    def test_register_screening_conflict_does_not_write(self):
        for name, text in [("S-FEED", FEED_T1), ("S-UNDER", UNDER_T1), ("S-OVER", OVER_BAD)]:
            self._upload(name, text)
        feed, under, over = (
            self._batch_id("S-FEED"),
            self._batch_id("S-UNDER"),
            self._batch_id("S-OVER"),
        )
        body = self._sheet(feed, under, over).get_data(as_text=True)
        self.assertIn("核对未通过", body)
        self.assertIn("不写入", body)
        self.assertIn("筛上批", body)
        with connect(self.tmp.name) as conn:
            n = conn.execute("SELECT COUNT(*) FROM screening_sheets").fetchone()[0]
        self.assertEqual(n, 0)

    def test_register_screening_rejects_non_integer_aperture(self):
        feed, under, over, _ = self._setup_screening_batches()
        for bad in ("100.0", "1e2", "abc", "", "-5", "100微米"):
            body = self._sheet(feed, under, over, aperture=bad).get_data(as_text=True)
            self.assertIn("整数微米", body, f"aperture={bad!r}")
        with connect(self.tmp.name) as conn:
            n = conn.execute("SELECT COUNT(*) FROM screening_sheets").fetchone()[0]
        self.assertEqual(n, 0)

    def test_supplement_passes_whole_sheet(self):
        feed, under, over, _ = self._setup_screening_batches()
        self._sheet(feed, under, over)
        # 相容补报：档表与原批已入库表相容，且整张单仍满足筛分关系
        body = self._upload("S-FEED", FEED_T2).get_data(as_text=True)
        self.assertIn("补报成功", body)
        self.assertIn("整张单复核通过", body)
        self.assertEqual(self._table_count("S-FEED"), 2)

    def test_supplement_to_product_checks_whole_sheet(self):
        feed, under, over, _ = self._setup_screening_batches()
        self._sheet(feed, under, over)
        # 筛下批细分表相容，整张单仍成立（原批粗档对细分无约束）
        body = self._upload("S-UNDER", "0 49 250\n50 99 350").get_data(as_text=True)
        self.assertIn("补报成功", body)
        self.assertIn("整张单复核通过", body)
        self.assertEqual(self._table_count("S-UNDER"), 2)
        # 筛上批细分表相容，整张单仍成立
        body = self._upload("S-OVER", "100 149 150\n150 199 250").get_data(as_text=True)
        self.assertIn("补报成功", body)
        self.assertEqual(self._table_count("S-OVER"), 2)

    def test_supplement_to_product_conflict_rejected(self):
        # 三批都用粗档登记：筛下批 [0,199] 600 只要求全部粒子 <100（可行）。
        for name, text in [
            ("C-FEED", FEED_T1),
            ("C-UNDER", "0 199 600\n"),
            ("C-OVER", "100 199 400\n"),
        ]:
            self._upload(name, text)
        feed = self._batch_id("C-FEED")
        under = self._batch_id("C-UNDER")
        over = self._batch_id("C-OVER")
        self.assertIn("登记成功", self._sheet(feed, under, over).get_data(as_text=True))

        # 与筛下批已入库粗表相容，但把 500 粒钉在 >=100，违反筛分关系
        body = self._upload("C-UNDER", "0 99 100\n100 199 500\n").get_data(as_text=True)
        self.assertIn("补报被拒绝", body)
        self.assertIn("整张单核对不通过", body)
        self.assertEqual(self._table_count("C-UNDER"), 1)

        # 与原批粗表相容、且整张单仍成立的细分补报照常入库
        body = self._upload("C-FEED", "0 49 250\n50 99 350\n100 199 400\n").get_data(as_text=True)
        self.assertIn("补报成功", body)
        self.assertIn("整张单复核通过", body)
        self.assertEqual(self._table_count("C-FEED"), 2)

    def test_supplement_rejected_by_whole_sheet_and_not_written(self):
        feed, under, over, _ = self._setup_screening_batches()
        self._sheet(feed, under, over)
        # FEED_BAD 与原批已入库档表相容，但整张单核对失败（筛下批只有 600 粒）
        body = self._upload("S-FEED", FEED_BAD).get_data(as_text=True)
        self.assertIn("补报被拒绝", body)
        self.assertIn("整张单核对不通过", body)
        self.assertIn("筛下批", body)
        self.assertEqual(self._table_count("S-FEED"), 1)
        # 档表未变，登记仍在
        with connect(self.tmp.name) as conn:
            n = conn.execute("SELECT COUNT(*) FROM screening_sheets").fetchone()[0]
        self.assertEqual(n, 1)

    def test_supplement_to_other_batch_during_registration_allowed(self):
        """核对期间其他批仍可补报：登记只读核对阶段不持有写锁。"""
        import threading
        import time
        from unittest import mock

        from app import web as web_module

        feed, under, over, spare = self._setup_screening_batches()
        start = threading.Event()
        release = threading.Event()
        real = web_module.screening_compatible
        calls = {"n": 0, "guard": threading.Lock()}

        def slow_screening(*args, **kwargs):
            with calls["guard"]:
                calls["n"] += 1
                n = calls["n"]
            if n == 1:  # 只拖慢登记前的只读核对
                start.set()
                release.wait(timeout=5.0)
            return real(*args, **kwargs)

        result = {}

        def do_register():
            with self.app.test_client() as client:
                result["body"] = client.post(
                    "/screening",
                    data={
                        "feed_batch": str(feed),
                        "undersize_batch": str(under),
                        "oversize_batch": str(over),
                        "aperture": "100",
                    },
                    follow_redirects=True,
                ).get_data(as_text=True)

        with mock.patch.object(web_module, "screening_compatible", slow_screening):
            t = threading.Thread(target=do_register)
            t.start()
            self.assertTrue(start.wait(timeout=5.0), "登记未进入只读核对")
            # 登记仍在核对时，另一个批正常补报（相容的细分表），应成功写入
            body = self._upload(
                "S-SPARE", "0 4 5\n5 9 15\n10 14 10\n15 19 20\n20 29 50\n"
            ).get_data(as_text=True)
            self.assertIn("补报成功", body)
            release.set()
            t.join(timeout=10)
        self.assertIn("登记成功", result.get("body", ""))
        self.assertEqual(self._table_count("S-SPARE"), 2)

    def test_register_screening_rejects_preexisting_member(self):
        feed, under, over, _ = self._setup_screening_batches()
        self._sheet(feed, under, over)
        # 换一个批当筛下批但原批已在另一张单上
        spare = self._batch_id("S-SPARE")
        body = self._sheet(feed, spare, over).get_data(as_text=True)
        self.assertIn("已登记", body)
        with connect(self.tmp.name) as conn:
            n = conn.execute("SELECT COUNT(*) FROM screening_sheets").fetchone()[0]
        self.assertEqual(n, 1)

    # ------------------------------------------------------------------
    # 合并核算拦截
    # ------------------------------------------------------------------
    def test_merge_guard_blocks_feed_with_product(self):
        feed, under, over, _ = self._setup_screening_batches()
        self._sheet(feed, under, over)
        for partner in (under, over):
            body = self.client.post(
                "/compute", data={"batch_ids": [feed, partner]}, follow_redirects=True
            ).get_data(as_text=True)
            self.assertIn("合并核算被拦下", body)
            self.assertIn("原批", body)
            self.assertIn("不能与任一产物同时", body)
            self.assertNotIn("合并 P90 核算结果", body)

    def test_merge_guard_allows_both_products(self):
        feed, under, over, _ = self._setup_screening_batches()
        self._sheet(feed, under, over)
        rv = self.client.post(
            "/compute", data={"batch_ids": [under, over]}, follow_redirects=True
        )
        body = rv.get_data(as_text=True)
        self.assertIn("合并 P90 核算结果", body)
        self.assertIn("S-UNDER", body)
        self.assertIn("S-OVER", body)
        self.assertNotIn("合并核算被拦下", body)

    def test_merge_guard_allows_unregistered_and_feed_alone(self):
        feed, under, over, spare = self._setup_screening_batches()
        self._sheet(feed, under, over)
        for ids in ([feed], [under], [feed, spare], [under, spare]):
            body = self.client.post(
                "/compute", data={"batch_ids": ids}, follow_redirects=True
            ).get_data(as_text=True)
            self.assertIn("合并 P90 核算结果", body, f"ids={ids}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
