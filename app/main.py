"""FastAPI application: uploads, generation, review and download."""
from __future__ import annotations

import csv
import io
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .compute import GenerationError, generate_generation, reason_label
from .database import get_connection, init_db
from .ingestion import (
    BatchNotFound,
    BatchNotWithdrawable,
    BatchRejected,
    ingest_readings,
    ingest_replacements,
    record_rejected_batch,
    withdraw_reading_batch,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="供水账户用量明细服务", lifespan=lifespan)
STATIC_DIR = Path(__file__).parent / "static"


class Message(BaseModel):
    ok: bool
    message: str
    detail: dict | None = None


def _text_of(file: UploadFile) -> str:
    raw = file.file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传文件为空")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return raw.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400,
                                detail="文件必须为 UTF-8（或 GB18030）编码的 CSV") from exc


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/uploads/readings", response_model=Message)
def upload_readings(file: UploadFile = File(...)):
    text = _text_of(file)
    filename = file.filename or "readings.csv"
    conn = get_connection()
    try:
        try:
            detail = ingest_readings(conn, filename, text)
        except BatchRejected as exc:
            # Single place that records the rejection audit row: the ingestion
            # transaction has already rolled back, so nothing partial survives.
            try:
                record_rejected_batch(conn, "reading", filename, exc.reason)
            except Exception:  # pragma: no cover - audit must never mask the 422
                pass
            raise HTTPException(status_code=422, detail=exc.reason) from exc
        return Message(
            ok=True,
            message=(
                f"读数上传成功：逐条来源 {detail['sources_recorded']} 条，"
                f"新增有效读数 {detail['inserted']} 条，"
                f"同值重传（保留来源）{detail['duplicates_skipped']} 条，"
                f"重新生效 {detail['reactivated']} 条；"
                + ("现役结果已标过期" if detail["effective_changed"]
                   else "有效读数未变化，现役结果保持有效")
            ),
            detail=detail,
        )
    finally:
        conn.close()


@app.post("/api/uploads/replacements", response_model=Message)
def upload_replacements(file: UploadFile = File(...)):
    text = _text_of(file)
    filename = file.filename or "replacements.csv"
    conn = get_connection()
    try:
        try:
            detail = ingest_replacements(conn, filename, text)
        except BatchRejected as exc:
            try:
                record_rejected_batch(conn, "replacement", filename, exc.reason)
            except Exception:  # pragma: no cover
                pass
            raise HTTPException(status_code=422, detail=exc.reason) from exc
        return Message(ok=True,
                       message=f"换表整版上传成功：载入 {detail['inserted']} 户，旧版换表数据已被替换",
                       detail=detail)
    finally:
        conn.close()


@app.post("/api/batches/{batch_id}/withdraw", response_model=Message)
def withdraw_batch(batch_id: int):
    """Withdraw a committed readings batch (per-row sources required)."""
    conn = get_connection()
    try:
        try:
            detail = withdraw_reading_batch(conn, batch_id)
        except BatchNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except BatchNotWithdrawable as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return Message(
            ok=True,
            message=(
                f"批次 {batch_id} 已撤回：停用来源 {detail['sources_withdrawn']} 条，"
                f"失去最后来源的读数 {detail['readings_deactivated']} 条；"
                + ("现役结果已标过期" if detail["effective_changed"]
                   else "有效读数未变化（仍有其他未撤回批次供数），现役结果保持有效")
            ),
            detail=detail,
        )
    finally:
        conn.close()


@app.post("/api/generations", response_model=Message)
def create_generation():
    conn = get_connection()
    try:
        try:
            detail = generate_generation(conn)
        except GenerationError as exc:
            # Business-level failure: keep the previous result, mark it failed/expired.
            _mark_active_failed(conn, str(exc))
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:  # pragma: no cover - defensive
            _mark_active_failed(conn, f"生成失败: {exc}")
            raise HTTPException(status_code=500, detail=f"生成失败: {exc}") from exc
        return Message(
            ok=True,
            message=f"第 {detail['generation_id']} 代结果已发布为现役："
                    f"共 {detail['total_rows']} 行（正常 {detail['ok_rows']}，待核 {detail['review_rows']}）",
            detail=detail,
        )
    finally:
        conn.close()


def _mark_active_failed(conn: sqlite3.Connection, detail: str) -> None:
    """A failed generation publishes nothing new. The previous result stays
    downloadable but is marked expired (with the failure reason recorded),
    so no row claims to be the current live result."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """UPDATE generations
                  SET status='expired',
                      status_detail=?,
                      superseded_at=COALESCE(superseded_at, ?)
                WHERE status='active'""",
            (f"后续生成失败，旧结果保留：{detail}", _now()),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _now() -> str:
    from .ingestion import now_utc_iso
    return now_utc_iso()


@app.get("/api/generations")
def list_generations():
    conn = get_connection()
    try:
        gens = [dict(r) for r in conn.execute(
            "SELECT * FROM generations ORDER BY id DESC")]
        for g in gens:
            g["row_count"] = conn.execute(
                "SELECT COUNT(*) FROM result_rows WHERE generation_id=?",
                (g["id"],)).fetchone()[0]
            g["review_count"] = conn.execute(
                "SELECT COUNT(*) FROM result_rows WHERE generation_id=? AND status='REVIEW'",
                (g["id"],)).fetchone()[0]
        return {"generations": gens}
    finally:
        conn.close()


@app.get("/api/batches")
def list_batches():
    conn = get_connection()
    try:
        batches = [
            dict(r) for r in conn.execute(
                "SELECT * FROM upload_batches ORDER BY id DESC")
        ]
        for b in batches:
            if b["batch_type"] == "reading":
                stats = conn.execute(
                    """SELECT COUNT(*) AS total,
                              SUM(CASE WHEN active=1 THEN 1 ELSE 0 END) AS active_count
                         FROM reading_sources WHERE batch_id=?""",
                    (b["id"],),
                ).fetchone()
                b["source_count"] = stats["total"] or 0
                b["active_source_count"] = stats["active_count"] or 0
            else:
                b["source_count"] = None
                b["active_source_count"] = None
        return {"batches": batches}
    finally:
        conn.close()


@app.get("/api/batches/{batch_id}/sources")
def batch_sources(batch_id: int):
    """Per-row source ledger of one batch (includes withdrawn rows)."""
    conn = get_connection()
    try:
        batch = conn.execute(
            "SELECT * FROM upload_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if batch is None:
            raise HTTPException(status_code=404, detail=f"不存在批次 {batch_id}")
        if batch["batch_type"] != "reading":
            raise HTTPException(
                status_code=409,
                detail=f"批次 {batch_id} 是换表批次，无逐条读数来源记录")
        if not batch["withdrawable"]:
            return {
                "batch_id": batch_id,
                "withdrawable": False,
                "sources": [],
                "message": "旧版数据库批次，缺少逐条来源记录，不可撤回；其读数仍正常供数",
            }
        sources = [
            dict(r) for r in conn.execute(
                """SELECT id, reading_id, row_no, account, meter, read_at,
                          reading_cum, active, created_at
                     FROM reading_sources
                    WHERE batch_id=? ORDER BY row_no, id""",
                (batch_id,),
            )
        ]
        return {"batch_id": batch_id, "withdrawable": True, "sources": sources}
    finally:
        conn.close()


CSV_COLUMNS = [
    "generation_id", "account", "meter",
    "interval_start_utc", "interval_end_utc",
    "start_reading_m3", "end_reading_m3", "consumption_m3",
    "attributed_utc_day", "status", "review_reason", "review_reason_detail",
    "source_reading_ids",
]


def _generation_or_404(conn: sqlite3.Connection, generation_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM generations WHERE id=?",
                       (generation_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"不存在第 {generation_id} 代结果")
    return row


@app.get("/api/generations/latest/download")
def download_latest():
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM generations ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="尚未生成任何结果")
        gen_id = row["id"]
    finally:
        conn.close()
    return download_generation(gen_id)


@app.get("/api/generations/{generation_id}/download")
def download_generation(generation_id: int):
    conn = get_connection()
    try:
        gen = _generation_or_404(conn, generation_id)
        rows = conn.execute(
            "SELECT * FROM result_rows WHERE generation_id=? ORDER BY id",
            (generation_id,)).fetchall()

        def iter_csv():
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(CSV_COLUMNS)
            yield buffer.getvalue()
            for r in rows:
                buffer = io.StringIO()
                writer = csv.writer(buffer)
                writer.writerow([
                    generation_id, r["account"], r["meter"],
                    r["interval_start"] or "", r["interval_end"] or "",
                    "" if r["start_reading"] is None else r["start_reading"],
                    "" if r["end_reading"] is None else r["end_reading"],
                    "" if r["consumption_m3"] is None else r["consumption_m3"],
                    r["attributed_day"] or "",
                    r["status"],
                    r["review_reason"] or "",
                    reason_label(r["review_reason"]) or "",
                    r["source_reading_ids"],
                ])
                yield buffer.getvalue()

        headers = {
            "Content-Disposition":
                f'attachment; filename="usage_generation_{generation_id}.csv"',
            "X-Generation-Status": gen["status"],
        }
        return StreamingResponse(
            iter_csv(), media_type="text/csv; charset=utf-8", headers=headers)
    finally:
        conn.close()


@app.get("/api/generations/{generation_id}/rows")
def generation_rows(generation_id: int, limit: int = 1000):
    conn = get_connection()
    try:
        _generation_or_404(conn, generation_id)
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM result_rows WHERE generation_id=? ORDER BY id LIMIT ?",
            (generation_id, limit))]
        for r in rows:
            r["review_reason_detail"] = reason_label(r["review_reason"])
        return {"generation_id": generation_id, "rows": rows}
    finally:
        conn.close()


@app.get("/healthz")
def healthz():
    return {"ok": True}
