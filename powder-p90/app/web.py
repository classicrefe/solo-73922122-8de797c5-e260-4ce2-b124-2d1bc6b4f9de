"""Flask 网页：按批号上传/补报档表，登记筛分核对单，勾选批次核算合并 P90。

登记与补报的语义
----------------
* 补报（同一批号再传一张档表）：先与该批全部已入库档表做相容性校验；若该批
  已登记在筛分核对单上，还必须对整张单重做筛分核对（原批 = 筛下批 + 筛上批）。
* 登记核对单：三批须为三个不同批号，筛孔为整数微米；按三批全部已入库档表核对，
  通过才写入，且同一批只能登记在一张单上。
* 核对期间只做只读查询，不持有写锁，其他批仍可补报。
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import asdict

from flask import Flask, flash, redirect, render_template, request, url_for

from .core import (
    ROLE_NAMES,
    BinTableError,
    compatible,
    merge_p90,
    screening_compatible,
)
from .db import (
    connect,
    create_batch,
    get_batch_by_name,
    get_screening_by_batch,
    init_db,
    insert_screening,
    insert_table,
    list_batches,
    list_screening_sheets,
    list_tables,
    load_batch_tables,
    load_table,
    merged_guard,
    parse_and_preview,
)

ROLES = ("feed", "undersize", "oversize")


def _looks_like_uint(text: str) -> bool:
    """仅接受 ASCII 十进制数字串（拒绝小数点、指数、正负号、全角数字）。"""
    return bool(text) and text.isascii() and text.isdigit()


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "powder-p90-demo-secret")
    app.config["DB_PATH"] = db_path or os.environ.get(
        "POWDER_DB", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "powder.db")
    )
    os.makedirs(os.path.dirname(app.config["DB_PATH"]), exist_ok=True)
    init_db(app.config["DB_PATH"])

    def sheet_members(conn, sheet_id: int) -> dict[str, dict]:
        rows = conn.execute(
            """SELECT r.role, b.id AS batch_id, b.name
                 FROM screening_roles r JOIN batches b ON b.id = r.batch_id
                WHERE r.sheet_id = ?""",
            (sheet_id,),
        ).fetchall()
        return {r["role"]: dict(r) for r in rows}

    def role_by_batch(conn) -> dict[int, dict]:
        rows = conn.execute(
            """SELECT r.batch_id, r.sheet_id, r.role, s.aperture
                 FROM screening_roles r
                 JOIN screening_sheets s ON s.id = r.sheet_id"""
        ).fetchall()
        return {int(r["batch_id"]): dict(r) for r in rows}

    @app.errorhandler(sqlite3.OperationalError)
    def db_busy(exc):
        # 两工作进程共用 SQLite；极端并发下写锁等待超时也不应抛 500。
        app.logger.warning("SQLite operational error: %s", exc)
        if request.method == "POST":
            flash(
                f"数据库正忙，本次操作未完成（{exc}）。请刷新后重试；已入库数据不受影响。",
                "error",
            )
            return redirect(url_for("index"))
        return (
            "数据库正忙，请稍后刷新重试。",
            503,
            {"Content-Type": "text/plain; charset=utf-8"},
        )

    @app.route("/")
    def index():
        with connect(app.config["DB_PATH"]) as conn:
            batches = [dict(r) for r in list_batches(conn)]
            roles = role_by_batch(conn)
            sheets = [
                {
                    "id": s["id"],
                    "aperture": s["aperture"],
                    "created_at": s["created_at"],
                    **{role: s["roles"].get(role) for role in ROLES},
                }
                for s in list_screening_sheets(conn)
            ]
        return render_template(
            "index.html",
            batches=batches,
            role_by_batch=roles,
            sheets=sheets,
            role_names=ROLE_NAMES,
        )

    # ------------------------------------------------------------------
    # 上传 / 补报
    # ------------------------------------------------------------------
    @app.route("/upload", methods=["POST"])
    def upload():
        name = (request.form.get("batch_name") or "").strip()
        text = request.form.get("table_text") or ""
        note = (request.form.get("note") or "").strip()
        if not name:
            flash("批号不能为空", "error")
            return redirect(url_for("index"))
        try:
            table = parse_and_preview(text)
        except BinTableError as exc:
            flash(f"档表解析失败，未入库：{exc}", "error")
            return redirect(url_for("index"))

        with connect(app.config["DB_PATH"]) as conn:
            # 补报要“读全部已入库档表 → 相容性/整张单复核 → 写入”。用 IMMEDIATE
            # 事务把三步串行化，避免两张并发补报各自基于旧档表通过检查后同时写入。
            # 事务只覆盖毫秒级的按档核对；登记单的长核对阶段不加写锁（见下方）。
            conn.execute("BEGIN IMMEDIATE")
            row = get_batch_by_name(conn, name)
            if row is None:
                batch_id = create_batch(conn, name)
                table_id = insert_table(conn, batch_id, table, note)
                flash(
                    f"新批 {name} 已创建，档表 #{table_id} 入库：{len(table.bins)} 档、"
                    f"{table.total} 粒",
                    "ok",
                )
                return redirect(url_for("index"))

            batch_id = int(row["id"])
            existing = load_batch_tables(conn, batch_id)
            ok, reason = compatible(existing + [table])
            if not ok:
                # 补报失败：不写入，后续核算/登记仍只按此前已入库档表
                conn.rollback()
                flash(
                    f"补报被拒绝（与批 {name} 已入库的 {len(existing)} 张档表不相容）："
                    f"{reason}。本次上传未入库，核算仍按原档表进行。",
                    "error",
                )
                return redirect(url_for("index"))

            # 该批若已登记核对单，补报后必须对整张单重做筛分核对，失败则整次不写。
            membership = get_screening_by_batch(conn, batch_id)
            if membership is not None:
                sheet_id = int(membership["id"])
                members = sheet_members(conn, sheet_id)
                role_tables = {}
                for role in ROLES:
                    member = members.get(role)
                    role_tables[role] = (
                        load_batch_tables(conn, int(member["batch_id"])) if member else []
                    )
                role_tables[str(membership["role"])] = existing + [table]
                ok, reason = screening_compatible(
                    role_tables["feed"],
                    role_tables["undersize"],
                    role_tables["oversize"],
                    int(membership["aperture"]),
                )
                if not ok:
                    conn.rollback()
                    flash(
                        f"补报被拒绝（批 {name} 在核对单 #{sheet_id} 上，补报后整张单核对不通过）："
                        f"{reason}。本次上传未入库，核对单与三批档表均保持原样。",
                        "error",
                    )
                    return redirect(url_for("index"))
                table_id = insert_table(conn, batch_id, table, note)
                flash(
                    f"批 {name} 补报成功：档表 #{table_id} 与该批已入库档表相容，"
                    f"且核对单 #{sheet_id}（筛孔 {membership['aperture']} 微米）整张单复核通过；"
                    f"现共 {len(existing) + 1} 张档表",
                    "ok",
                )
                return redirect(url_for("index"))

            table_id = insert_table(conn, batch_id, table, note)
            flash(
                f"批 {name} 补报成功：档表 #{table_id} 与已有 {len(existing)} 张档表相容，"
                f"现共 {len(existing) + 1} 张档表、{table.total} 粒",
                "ok",
            )
        return redirect(url_for("index"))

    # ------------------------------------------------------------------
    # 筛分核对单登记
    # ------------------------------------------------------------------
    @app.route("/screening", methods=["POST"])
    def register_screening():
        try:
            ids = {
                role: int((request.form.get(f"{role}_batch") or "").strip())
                for role in ROLES
            }
        except ValueError:
            flash("登记失败（未写入）：须为原批、筛下批、筛上批各选一个已入库批次", "error")
            return redirect(url_for("index"))

        raw_aperture = (request.form.get("aperture") or "").strip()
        if not _looks_like_uint(raw_aperture):
            flash(
                "登记失败（未写入）：筛孔尺寸须用整数微米填写（不含小数点、单位或正负号）",
                "error",
            )
            return redirect(url_for("index"))
        aperture = int(raw_aperture)
        if aperture <= 0:
            flash("登记失败（未写入）：筛孔尺寸须为正整数微米（大于 0）", "error")
            return redirect(url_for("index"))

        if len(set(ids.values())) != 3:
            flash("登记失败（未写入）：原批、筛下批、筛上批必须是三个不同的批号", "error")
            return redirect(url_for("index"))

        # 只读核对阶段：不持有写锁，其他批的补报不被阻塞。
        with connect(app.config["DB_PATH"]) as conn:
            names, missing, tables = _load_role_tables(conn, ids)
            if missing:
                flash(f"登记失败（未写入）：批次不存在或没有已入库档表：{missing}", "error")
                return redirect(url_for("index"))
            already = [
                f"{names[bid]}（{ROLE_NAMES[role]}）"
                for role, bid in ids.items()
                if get_screening_by_batch(conn, bid) is not None
            ]
            if already:
                flash(
                    "登记失败（未写入）：%s 已登记在核对单上，同一批只能登记在一张单上"
                    % "、".join(already),
                    "error",
                )
                return redirect(url_for("index"))
            ok, reason = screening_compatible(
                tables["feed"], tables["undersize"], tables["oversize"], aperture
            )
            if not ok:
                flash(
                    f"核对未通过，本次登记不写入：{reason}。"
                    "核对口径：原批中粒径小于筛孔的粒子全部归筛下批，其余全部归筛上批；"
                    "三批的全部档表必须能同时由一次筛分得到。",
                    "error",
                )
                return redirect(url_for("index"))

            # 写事务内用最新档表复核一次：若核对期间有人补报了这三批中的任何一批，
            # 则以补报后的整张单为准；复核仍须通过才写入，避免基于过期档表登记。
            conn.execute("BEGIN IMMEDIATE")
            names2, missing2, tables2 = _load_role_tables(conn, ids)
            # 写事务内重新检查是否已被并发登记（BEGIN IMMEDIATE 前可能被抢先）。
            for role, bid in ids.items():
                if get_screening_by_batch(conn, bid) is not None:
                    conn.rollback()
                    flash(
                        "登记失败（未写入）：所选批次已被其他核对单登记，请刷新后重试",
                        "error",
                    )
                    return redirect(url_for("index"))
            if missing2:
                conn.rollback()
                flash(f"登记失败（未写入）：{missing2}", "error")
                return redirect(url_for("index"))
            ok2, reason2 = screening_compatible(
                tables2["feed"], tables2["undersize"], tables2["oversize"], aperture
            )
            if not ok2:
                conn.rollback()
                flash(
                    f"登记失败（未写入）：核对期间档表已更新，按最新档表复核不通过：{reason2}",
                    "error",
                )
                return redirect(url_for("index"))
            try:
                sheet_id = insert_screening(
                    conn, aperture, ids["feed"], ids["undersize"], ids["oversize"]
                )
            except sqlite3.IntegrityError:
                # 并发下另一请求刚登记了同一批：UNIQUE 约束兜底，本次不写入。
                conn.rollback()
                flash("登记失败（未写入）：所选批次已被其他核对单登记，请刷新后重试", "error")
                return redirect(url_for("index"))
            names = names2

        flash(
            f"核对通过，登记成功：核对单 #{sheet_id}，筛孔 {aperture} 微米；"
            f"原批 {names[ids['feed']]}，筛下批 {names[ids['undersize']]}，"
            f"筛上批 {names[ids['oversize']]}",
            "ok",
        )
        return redirect(url_for("index"))

    def _load_role_tables(conn, ids):
        names, tables, missing = {}, {}, []
        for role, bid in ids.items():
            row = conn.execute("SELECT name FROM batches WHERE id = ?", (bid,)).fetchone()
            if row is None:
                missing.append(f"#{bid}")
                continue
            names[bid] = row["name"]
            ts = load_batch_tables(conn, bid)
            if not ts:
                missing.append(f"{row['name']}（无档表）")
                continue
            tables[role] = ts
        return names, missing, tables

    # ------------------------------------------------------------------
    # 批次详情
    # ------------------------------------------------------------------
    @app.route("/batch/<int:batch_id>")
    def batch_detail(batch_id: int):
        with connect(app.config["DB_PATH"]) as conn:
            trows = list_tables(conn, batch_id)
            tables = [
                {
                    "meta": dict(r),
                    "bins": [asdict(b) for b in load_table(conn, int(r["id"])).bins],
                }
                for r in trows
            ]
            batches = [dict(r) for r in list_batches(conn)]
            name = next((b["name"] for b in batches if b["id"] == batch_id), str(batch_id))
            membership = get_screening_by_batch(conn, batch_id)
            sheet = None
            if membership is not None:
                sheet = {
                    "id": int(membership["id"]),
                    "aperture": int(membership["aperture"]),
                    "role": membership["role"],
                    **sheet_members(conn, int(membership["id"])),
                }
        return render_template(
            "batch.html",
            name=name,
            batch_id=batch_id,
            tables=tables,
            sheet=sheet,
            role_names=ROLE_NAMES,
        )

    # ------------------------------------------------------------------
    # 合并核算
    # ------------------------------------------------------------------
    @app.route("/compute", methods=["POST"])
    def compute():
        try:
            ids = [int(x) for x in request.form.getlist("batch_ids")]
        except ValueError:
            flash("勾选的批次非法，请重新选择", "error")
            return redirect(url_for("index"))
        if not ids:
            flash("请至少勾选一个批次", "error")
            return redirect(url_for("index"))
        if len(set(ids)) != len(ids):
            flash("勾选的批次存在重复，请重新选择", "error")
            return redirect(url_for("index"))

        with connect(app.config["DB_PATH"]) as conn:
            all_batches = {int(r["id"]): dict(r) for r in list_batches(conn)}
            missing = [i for i in ids if i not in all_batches]
            if missing:
                flash(f"批次不存在：{missing}", "error")
                return redirect(url_for("index"))
            ok, reason = merged_guard(conn, ids)
            if not ok:
                flash(f"合并核算被拦下：{reason}", "error")
                return redirect(url_for("index"))
            batch_tables = [(bid, load_batch_tables(conn, bid)) for bid in ids]
            if any(not ts for _, ts in batch_tables):
                flash("所选批次中存在没有任何档表的批，无法核算", "error")
                return redirect(url_for("index"))
            try:
                result = merge_p90(batch_tables)
            except BinTableError as exc:
                flash(f"核算失败：{exc}", "error")
                return redirect(url_for("index"))

            # 逐批见证频数整理为有序列表，并附每批名称
            def witness_rows(witness):
                out = []
                for bid in ids:
                    freq = witness[bid]
                    rows = [
                        {"size": s, "count": freq[s]} for s in sorted(freq) if freq[s]
                    ]
                    out.append(
                        {
                            "batch_id": bid,
                            "batch_name": all_batches[bid]["name"],
                            "total": sum(freq.values()),
                            "rows": rows,
                        }
                    )
                return out

            ctx = {
                "names": {bid: all_batches[bid]["name"] for bid in ids},
                "ids": ids,
                "total": result.total,
                "k": result.k,
                "p90_low": result.p90_low,
                "p90_high": result.p90_high,
                "witness_low": witness_rows(result.witness_low),
                "witness_high": witness_rows(result.witness_high),
            }
        return render_template("result.html", **ctx)

    return app
