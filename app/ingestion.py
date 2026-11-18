"""CSV parsing, validation, source-ledger ingestion and batch withdrawal."""
from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import (
    REASON_CROSS_SWITCH,
    REASON_NO_REPLACEMENT,
    REASON_OUTSIDE_WINDOW,
    REASON_REVERSED,
    REASON_UNPAIRED,
)

READING_HEADERS = ["account", "meter", "read_at", "reading_cum"]
REPLACEMENT_HEADERS = ["account", "old_meter", "new_meter", "switched_at"]


class BatchRejected(Exception):
    """Raised when an upload fails validation; nothing is committed."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class BatchNotFound(Exception):
    """The referenced upload batch does not exist."""


class BatchNotWithdrawable(Exception):
    """The batch cannot be withdrawn (legacy, rejected, wrong type, already withdrawn)."""


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_utc(raw: str) -> datetime:
    """Parse an ISO-8601 timestamp; naive values are treated as UTC."""
    text = raw.strip()
    if not text:
        raise ValueError("empty timestamp")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"unparsable timestamp {raw!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_iso_utc(dt: datetime) -> str:
    if dt.microsecond:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f").rstrip("0").rstrip(".") + "Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class ReadingRow:
    account: str
    meter: str
    read_at: str
    reading_cum: float
    row_no: int


@dataclass(frozen=True)
class ReplacementRow:
    account: str
    old_meter: str
    new_meter: str
    switched_at: str


def _open_csv(text: str):
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader]
    while rows and not any(cell.strip() for cell in rows[-1]):
        rows.pop()
    if not rows:
        raise BatchRejected("CSV 文件为空")
    return rows


def _check_headers(header: list[str], expected: list[str]):
    cleaned = [h.strip() for h in header]
    if cleaned != expected:
        raise BatchRejected(
            f"表头必须为 {','.join(expected)}，实际为 {','.join(cleaned) if cleaned else '(空)'}"
        )


def parse_readings_csv(text: str) -> list[ReadingRow]:
    rows = _open_csv(text)
    _check_headers(rows[0], READING_HEADERS)
    parsed: list[ReadingRow] = []
    seen: dict[tuple[str, str, str], float] = {}
    row_no = 0
    for line_no, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue
        if len(row) != len(READING_HEADERS):
            raise BatchRejected(f"第 {line_no} 行字段数应为 {len(READING_HEADERS)}，实际为 {len(row)}")
        account, meter, raw_ts, raw_val = (c.strip() for c in row)
        if not account or not meter:
            raise BatchRejected(f"第 {line_no} 行 account/meter 不能为空")
        try:
            ts = format_iso_utc(parse_iso_utc(raw_ts))
        except ValueError as exc:
            raise BatchRejected(f"第 {line_no} 行时刻无效: {exc}") from exc
        try:
            value = float(raw_val)
        except ValueError as exc:
            raise BatchRejected(f"第 {line_no} 行累计读数不是数字: {raw_val!r}") from exc
        if value < 0:
            raise BatchRejected(f"第 {line_no} 行累计读数不能为负: {value}")
        row_no += 1
        key = (account, meter, ts)
        if key in seen and seen[key] != value:
            raise BatchRejected(
                f"第 {line_no} 行与批内更早的同表同时刻读数异值（{seen[key]} != {value}），整批拒收"
            )
        # Same-value retransmissions are kept as separate source-ledger rows;
        # only conflicting values reject the whole batch.
        seen.setdefault(key, value)
        parsed.append(ReadingRow(account, meter, ts, value, row_no))
    if not parsed:
        raise BatchRejected("CSV 不含任何有效数据行")
    return parsed


def parse_replacements_csv(text: str) -> list[ReplacementRow]:
    rows = _open_csv(text)
    _check_headers(rows[0], REPLACEMENT_HEADERS)
    parsed: list[ReplacementRow] = []
    seen_accounts: set[str] = set()
    meter_owner: dict[str, str] = {}
    for line_no, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue
        if len(row) != len(REPLACEMENT_HEADERS):
            raise BatchRejected(f"第 {line_no} 行字段数应为 {len(REPLACEMENT_HEADERS)}，实际为 {len(row)}")
        account, old_meter, new_meter, raw_ts = (c.strip() for c in row)
        if not all((account, old_meter, new_meter, raw_ts)):
            raise BatchRejected(f"第 {line_no} 行存在空字段")
        if old_meter == new_meter:
            raise BatchRejected(f"第 {line_no} 行旧表与新表不能相同: {old_meter}")
        if account in seen_accounts:
            raise BatchRejected(f"第 {line_no} 行账户 {account} 在本批中出现多条换表记录（每户一条）")
        try:
            ts = format_iso_utc(parse_iso_utc(raw_ts))
        except ValueError as exc:
            raise BatchRejected(f"第 {line_no} 行切换时刻无效: {exc}") from exc
        for meter in (old_meter, new_meter):
            owner = meter_owner.get(meter)
            if owner is not None and owner != account:
                raise BatchRejected(
                    f"第 {line_no} 行表号 {meter} 已归属账户 {owner}，同一表号不能跨户使用"
                )
            meter_owner[meter] = account
        seen_accounts.add(account)
        parsed.append(ReplacementRow(account, old_meter, new_meter, ts))
    if not parsed:
        raise BatchRejected("CSV 不含任何有效数据行")
    return parsed


def _record_batch(
    conn: sqlite3.Connection,
    batch_type: str,
    filename: str,
    status: str,
    row_count: int,
    reject_reason: str | None,
    withdrawable: int = 1,
) -> int:
    cur = conn.execute(
        """INSERT INTO upload_batches
               (batch_type, filename, status, row_count, reject_reason,
                created_at, withdrawable, withdrawn_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, NULL)""",
        (batch_type, filename, status, row_count, reject_reason,
         now_utc_iso(), withdrawable),
    )
    return int(cur.lastrowid)


def record_rejected_batch(conn: sqlite3.Connection, batch_type: str,
                          filename: str, reason: str) -> None:
    """Persist the rejection itself as an audit record (never partial data)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        _record_batch(conn, batch_type, filename, "rejected", 0, reason)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _expire_active_generation(conn: sqlite3.Connection) -> None:
    """Supersede the currently active generation after effective inputs change."""
    conn.execute(
        """UPDATE generations
              SET status = 'expired', superseded_at = ?
            WHERE status = 'active'""",
        (now_utc_iso(),),
    )


def ingest_readings(conn: sqlite3.Connection, filename: str, text: str) -> dict:
    """Ingest a readings CSV into the source ledger.

    Every CSV data row becomes one ``reading_sources`` row — including
    same-value retransmissions. A reading (``readings``) is effective while at
    least one active source attests it. The active generation is expired only
    when the set of effective readings actually changes.
    """
    rows = parse_readings_csv(text)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = {
            (r["account"], r["meter"], r["read_at"]):
                {"id": r["id"], "value": r["reading_cum"], "active": r["active"]}
            for r in conn.execute(
                "SELECT id, account, meter, read_at, reading_cum, active FROM readings"
            )
        }
        duplicates = 0       # rows attesting an already-effective reading (same value)
        reactivated = 0      # rows bringing an inactive reading back into effect
        new_readings = 0     # readings rows created
        batch_id = _record_batch(conn, "reading", filename, "committed", len(rows), None)

        source_records: list[tuple] = []
        for row in rows:
            key = (row.account, row.meter, row.read_at)
            present = existing.get(key)
            if present is None:
                cur = conn.execute(
                    """INSERT INTO readings
                           (batch_id, account, meter, read_at, reading_cum,
                            created_at, active)
                       VALUES (?, ?, ?, ?, ?, ?, 1)""",
                    (batch_id, row.account, row.meter, row.read_at,
                     row.reading_cum, now_utc_iso()),
                )
                rid = int(cur.lastrowid)
                existing[key] = {"id": rid, "value": row.reading_cum, "active": 1}
                new_readings += 1
            else:
                rid = present["id"]
                if present["active"]:
                    # Effective readings are immutable: a different value here
                    # rejects the whole batch (nothing has been committed yet).
                    if present["value"] != row.reading_cum:
                        raise BatchRejected(
                            f"表 {row.meter} 在 {row.read_at} 已有有效读数 "
                            f"{present['value']}，本批为 {row.reading_cum}，异值整批拒收"
                        )
                    duplicates += 1
                else:
                    # The reading had lost all its sources; any same-key upload
                    # re-attests it. A differing value is still a conflict.
                    if present["value"] != row.reading_cum:
                        raise BatchRejected(
                            f"表 {row.meter} 在 {row.read_at} 存在已撤回来源的历史读数 "
                            f"{present['value']}，本批为 {row.reading_cum}，异值整批拒收"
                        )
                    conn.execute(
                        "UPDATE readings SET active=1, batch_id=?, reading_cum=? WHERE id=?",
                        (batch_id, row.reading_cum, rid),
                    )
                    present["active"] = 1
                    reactivated += 1
            source_records.append(
                (batch_id, rid, row.account, row.meter, row.read_at,
                 row.reading_cum, row.row_no, 1, now_utc_iso())
            )

        conn.executemany(
            """INSERT INTO reading_sources
                   (batch_id, reading_id, account, meter, read_at, reading_cum,
                    row_no, active, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            source_records,
        )

        effective_changed = (new_readings + reactivated) > 0
        if effective_changed:
            _expire_active_generation(conn)
        conn.commit()
    except BatchRejected:
        # Roll back every partial write; the API layer records exactly one
        # rejection audit row using the reason carried by the exception.
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    return {
        "batch_id": batch_id,
        "rows": len(rows),
        "sources_recorded": len(rows),
        "duplicates_skipped": duplicates,
        "reactivated": reactivated,
        "inserted": new_readings,
        "effective_changed": effective_changed,
    }


def ingest_replacements(conn: sqlite3.Connection, filename: str, text: str) -> dict:
    rows = parse_replacements_csv(text)
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Snapshot replacement: the full upload replaces every prior replacement row.
        conn.execute("DELETE FROM replacements")
        batch_id = _record_batch(conn, "replacement", filename, "committed",
                                 len(rows), None, withdrawable=0)
        conn.executemany(
            """INSERT INTO replacements (batch_id, account, old_meter, new_meter, switched_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (batch_id, r.account, r.old_meter, r.new_meter, r.switched_at, now_utc_iso())
                for r in rows
            ],
        )
        _expire_active_generation(conn)
        conn.commit()
    except BatchRejected:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    return {"batch_id": batch_id, "rows": len(rows), "inserted": len(rows)}


def withdraw_reading_batch(conn: sqlite3.Connection, batch_id: int) -> dict:
    """Withdraw one committed readings batch atomically.

    The batch's source rows are deactivated. A reading leaves the effective set
    only when this was its last active source; readings still attested by other
    non-withdrawn batches keep participating. All raw readings, source rows and
    previously downloaded generations remain in place for traceability. The
    active generation is expired only when effective readings actually changed.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        batch = conn.execute(
            "SELECT * FROM upload_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if batch is None:
            raise BatchNotFound(f"不存在批次 {batch_id}")
        if batch["batch_type"] != "reading":
            raise BatchNotWithdrawable(
                f"批次 {batch_id} 是换表批次，换表数据通过整版重传更正，不支持撤回"
            )
        if batch["status"] == "rejected":
            raise BatchNotWithdrawable(f"批次 {batch_id} 已被拒收，无需撤回")
        if batch["status"] == "withdrawn":
            raise BatchNotWithdrawable(f"批次 {batch_id} 已经撤回，不能重复撤回")
        if not batch["withdrawable"]:
            raise BatchNotWithdrawable(
                f"批次 {batch_id} 来自旧版数据库，缺少逐条来源记录，注明不可撤回"
            )

        cur = conn.execute(
            "UPDATE reading_sources SET active=0 WHERE batch_id=? AND active=1",
            (batch_id,),
        )
        sources_withdrawn = cur.rowcount

        # A reading drops out only if no other active source remains.
        cur = conn.execute(
            """
            UPDATE readings
               SET active=0
             WHERE active=1
               AND id IN (SELECT reading_id FROM reading_sources
                           WHERE batch_id=?)
               AND NOT EXISTS (
                     SELECT 1 FROM reading_sources s
                      WHERE s.reading_id = readings.id AND s.active=1)
            """,
            (batch_id,),
        )
        readings_deactivated = cur.rowcount

        conn.execute(
            "UPDATE upload_batches SET status='withdrawn', withdrawn_at=? WHERE id=?",
            (now_utc_iso(), batch_id),
        )
        effective_changed = readings_deactivated > 0
        if effective_changed:
            _expire_active_generation(conn)
        conn.commit()
    except (BatchNotFound, BatchNotWithdrawable):
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    return {
        "batch_id": batch_id,
        "sources_withdrawn": sources_withdrawn,
        "readings_deactivated": readings_deactivated,
        "effective_changed": effective_changed,
    }


REASON_LABELS = {
    REASON_UNPAIRED: "无法配对读数（缺少同户同表的前一笔相邻读数）",
    REASON_REVERSED: "累计读数倒退（后笔小于前笔）",
    REASON_CROSS_SWITCH: "相邻读数跨越换表切换时刻，禁止相减",
    REASON_OUTSIDE_WINDOW: "读数超出该表在本户的服务窗口",
    REASON_NO_REPLACEMENT: "该账户无换表记录，表归属无法确认",
}
