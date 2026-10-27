"""
Footprint Retention Manager
============================

Quản lý vòng đời dữ liệu Footprint trong SQLite.

Chức năng:
- Kiểm tra số lượng bản ghi và dung lượng SQLite.
- Xác định dữ liệu giao dịch cũ theo thời gian lưu trữ.
- Hỗ trợ dry-run trước khi xóa.
- Chỉ thực hiện xóa khi truyền --execute.
- Sao lưu SQLite trước khi xóa.
- Hỗ trợ lọc theo mã cổ phiếu.
- Ghi log quá trình retention.
- Không tự động xóa dữ liệu trên Supabase.

Mặc định:
- SQLite: data/footprint.db
- Thời gian giữ giao dịch: 90 ngày
- Dry-run: bật mặc định
- Không xóa dữ liệu nếu thiếu schema cần thiết

Ví dụ:
    python -m scripts.footprint.retention --check

    python -m scripts.footprint.retention --days 90

    python -m scripts.footprint.retention --days 90 --dry-run

    python -m scripts.footprint.retention --days 90 --execute

    python -m scripts.footprint.retention --days 180 --execute --backup

    python -m scripts.footprint.retention --symbols FPT,HPG,SSI --days 90

    python -m scripts.footprint.retention --json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sqlite3
import sys
import time

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


# ============================================================
# PATHS
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_SQLITE_PATH = PROJECT_ROOT / "data" / "footprint.db"

LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "footprint_retention.log"

BACKUP_DIR = PROJECT_ROOT / "backups" / "footprint"

DEFAULT_RETENTION_DAYS = 90
DEFAULT_BATCH_SIZE = 5000

TRADES_TABLE = "footprint_trades"


# ============================================================
# LOGGING
# ============================================================

def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("footprint_retention")
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    )

    file_handler = logging.FileHandler(
        LOG_FILE,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger


logger = setup_logging()


# ============================================================
# DATA CLASSES
# ============================================================

@dataclass
class TableInfo:
    table_name: str
    exists: bool
    row_count: int = 0
    oldest_timestamp_ms: Optional[int] = None
    newest_timestamp_ms: Optional[int] = None
    oldest_time: Optional[str] = None
    newest_time: Optional[str] = None
    database_size_bytes: int = 0


@dataclass
class RetentionPlan:
    sqlite_path: str
    table_name: str
    retention_days: int
    cutoff_timestamp_ms: int
    cutoff_time: str
    symbols: Optional[list[str]]
    total_rows: int
    rows_to_delete: int
    rows_to_keep: int
    dry_run: bool
    backup_required: bool


@dataclass
class RetentionResult:
    success: bool
    message: str
    sqlite_path: str
    table_name: str
    retention_days: int
    rows_before: int
    rows_to_delete: int
    rows_deleted: int
    rows_after: int
    cutoff_time: str
    backup_path: Optional[str] = None
    elapsed_seconds: float = 0.0
    error: Optional[str] = None


# ============================================================
# GENERAL HELPERS
# ============================================================

def utc_now_ms() -> int:
    return int(time.time() * 1000)


def timestamp_ms_to_iso(value: Optional[int]) -> Optional[str]:
    if value is None:
        return None

    try:
        return datetime.fromtimestamp(
            int(value) / 1000,
            tz=timezone.utc,
        ).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def format_bytes(size: int) -> str:
    value = float(max(0, size))

    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.2f} {unit}"
        value /= 1024

    return f"{value:.2f} TB"


def normalize_symbols(value: Optional[str]) -> Optional[list[str]]:
    if not value:
        return None

    symbols = sorted(
        {
            item.strip().upper()
            for item in value.split(",")
            if item.strip()
        }
    )

    return symbols or None


def quote_identifier(identifier: str) -> str:
    """
    Quote SQL identifiers.

    Only use this helper for table/column names obtained from SQLite
    schema inspection or controlled constants, never raw SQL fragments.
    """
    return '"' + identifier.replace('"', '""') + '"'


# ============================================================
# RETENTION MANAGER
# ============================================================

class FootprintRetentionManager:
    """
    Quản lý retention cho bảng footprint_trades trong SQLite.

    Schema dự kiến:
        id INTEGER PRIMARY KEY
        symbol TEXT
        trade_id TEXT
        timestamp INTEGER
        price REAL
        volume REAL
        side TEXT
        received_at INTEGER
        raw_json TEXT

    timestamp phải là Unix timestamp tính bằng milliseconds.
    """

    def __init__(
        self,
        sqlite_path: Optional[str | Path] = None,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        batch_size: int = DEFAULT_BATCH_SIZE,
        table_name: str = TRADES_TABLE,
        symbols: Optional[list[str]] = None,
        backup_dir: Optional[str | Path] = None,
    ) -> None:
        if retention_days < 1:
            raise ValueError("retention_days phải lớn hơn hoặc bằng 1.")

        if batch_size < 1:
            raise ValueError("batch_size phải lớn hơn hoặc bằng 1.")

        if table_name != TRADES_TABLE:
            raise ValueError(
                "Phiên bản này chỉ hỗ trợ bảng footprint_trades."
            )

        self.sqlite_path = Path(
            sqlite_path or DEFAULT_SQLITE_PATH
        ).expanduser().resolve()

        self.retention_days = int(retention_days)
        self.batch_size = int(batch_size)
        self.table_name = table_name

        self.symbols = (
            sorted({s.strip().upper() for s in symbols if s.strip()})
            if symbols
            else None
        )

        self.backup_dir = Path(
            backup_dir or BACKUP_DIR
        ).expanduser().resolve()

    # --------------------------------------------------------
    # DATABASE CONNECTION
    # --------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if not self.sqlite_path.exists():
            raise FileNotFoundError(
                f"Không tìm thấy SQLite: {self.sqlite_path}"
            )

        connection = sqlite3.connect(
            str(self.sqlite_path),
            timeout=30,
        )

        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")

        return connection

    def _table_exists(
        self,
        connection: sqlite3.Connection,
    ) -> bool:
        row = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name = ?
            """,
            (self.table_name,),
        ).fetchone()

        return row is not None

    def _get_columns(
        self,
        connection: sqlite3.Connection,
    ) -> set[str]:
        rows = connection.execute(
            f"PRAGMA table_info({quote_identifier(self.table_name)})"
        ).fetchall()

        return {str(row["name"]) for row in rows}

    def _validate_schema(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        if not self._table_exists(connection):
            raise RuntimeError(
                f"Không tìm thấy bảng {self.table_name}."
            )

        columns = self._get_columns(connection)

        required = {"id", "symbol", "timestamp"}
        missing = required - columns

        if missing:
            raise RuntimeError(
                "Schema footprint_trades không hợp lệ. "
                f"Thiếu các cột: {', '.join(sorted(missing))}"
            )

    # --------------------------------------------------------
    # INSPECTION
    # --------------------------------------------------------

    def inspect(self) -> TableInfo:
        if not self.sqlite_path.exists():
            return TableInfo(
                table_name=self.table_name,
                exists=False,
                database_size_bytes=0,
            )

        size = self.sqlite_path.stat().st_size

        with self._connect() as connection:
            if not self._table_exists(connection):
                return TableInfo(
                    table_name=self.table_name,
                    exists=False,
                    database_size_bytes=size,
                )

            self._validate_schema(connection)

            row = connection.execute(
                f"""
                SELECT
                    COUNT(*) AS row_count,
                    MIN(timestamp) AS oldest_timestamp_ms,
                    MAX(timestamp) AS newest_timestamp_ms
                FROM {quote_identifier(self.table_name)}
                """
            ).fetchone()

            oldest = row["oldest_timestamp_ms"]
            newest = row["newest_timestamp_ms"]

            return TableInfo(
                table_name=self.table_name,
                exists=True,
                row_count=int(row["row_count"] or 0),
                oldest_timestamp_ms=(
                    int(oldest) if oldest is not None else None
                ),
                newest_timestamp_ms=(
                    int(newest) if newest is not None else None
                ),
                oldest_time=timestamp_ms_to_iso(oldest),
                newest_time=timestamp_ms_to_iso(newest),
                database_size_bytes=size,
            )

    # --------------------------------------------------------
    # RETENTION PLAN
    # --------------------------------------------------------

    def build_plan(self) -> RetentionPlan:
        """
        Tạo kế hoạch dọn dữ liệu mà chưa xóa bất kỳ bản ghi nào.
        """
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=self.retention_days)

        cutoff_timestamp_ms = int(cutoff.timestamp() * 1000)

        with self._connect() as connection:
            self._validate_schema(connection)

            total_rows = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM {quote_identifier(self.table_name)}
                    """
                ).fetchone()[0]
            )

            where_parts = ["timestamp < ?"]
            params: list[Any] = [cutoff_timestamp_ms]

            if self.symbols:
                placeholders = ",".join(
                    "?" for _ in self.symbols
                )

                where_parts.append(
                    f"UPPER(symbol) IN ({placeholders})"
                )
                params.extend(self.symbols)

            where_sql = " AND ".join(where_parts)

            rows_to_delete = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM {quote_identifier(self.table_name)}
                    WHERE {where_sql}
                    """,
                    params,
                ).fetchone()[0]
            )

        return RetentionPlan(
            sqlite_path=str(self.sqlite_path),
            table_name=self.table_name,
            retention_days=self.retention_days,
            cutoff_timestamp_ms=cutoff_timestamp_ms,
            cutoff_time=cutoff.isoformat(),
            symbols=self.symbols,
            total_rows=total_rows,
            rows_to_delete=rows_to_delete,
            rows_to_keep=total_rows - rows_to_delete,
            dry_run=True,
            backup_required=True,
        )

    # --------------------------------------------------------
    # BACKUP
    # --------------------------------------------------------

    def create_backup(self) -> Path:
        """
        Tạo bản sao SQLite nhất quán bằng SQLite backup API.

        Cách này an toàn hơn việc shutil.copy trực tiếp khi DB đang
        được sử dụng, đặc biệt nếu SQLite bật WAL.
        """
        if not self.sqlite_path.exists():
            raise FileNotFoundError(
                f"Không tìm thấy SQLite: {self.sqlite_path}"
            )

        self.backup_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        timestamp = datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ"
        )

        backup_path = self.backup_dir / (
            f"footprint_before_retention_{timestamp}.db"
        )

        source = sqlite3.connect(
            str(self.sqlite_path),
            timeout=30,
        )

        destination = sqlite3.connect(
            str(backup_path),
            timeout=30,
        )

        try:
            source.backup(destination)
            destination.commit()
        except Exception:
            destination.close()
            source.close()

            if backup_path.exists():
                backup_path.unlink()

            raise
        else:
            destination.close()
            source.close()

        logger.info(
            "Đã tạo backup SQLite: %s (%s)",
            backup_path,
            format_bytes(backup_path.stat().st_size),
        )

        return backup_path

    # --------------------------------------------------------
    # DELETE
    # --------------------------------------------------------

    def execute(
        self,
        dry_run: bool = True,
        create_backup: bool = True,
        vacuum: bool = False,
    ) -> RetentionResult:
        """
        Thực thi retention.

        dry_run=True:
            Chỉ báo cáo, không xóa.

        dry_run=False:
            Xóa các bản ghi cũ hơn cutoff.
            Nếu create_backup=True, backup phải thành công trước khi xóa.

        vacuum=True:
            Chạy VACUUM sau khi xóa để cố gắng thu hồi dung lượng file.
            VACUUM có thể tốn thời gian và cần dung lượng đĩa trống.
        """
        started = time.monotonic()

        plan = self.build_plan()

        rows_before = plan.total_rows
        rows_to_delete = plan.rows_to_delete

        if dry_run:
            return RetentionResult(
                success=True,
                message=(
                    "DRY-RUN: không có dữ liệu nào bị xóa."
                ),
                sqlite_path=str(self.sqlite_path),
                table_name=self.table_name,
                retention_days=self.retention_days,
                rows_before=rows_before,
                rows_to_delete=rows_to_delete,
                rows_deleted=0,
                rows_after=rows_before,
                cutoff_time=plan.cutoff_time,
                elapsed_seconds=round(
                    time.monotonic() - started,
                    3,
                ),
            )

        if rows_to_delete == 0:
            return RetentionResult(
                success=True,
                message="Không có bản ghi nào cần xóa.",
                sqlite_path=str(self.sqlite_path),
                table_name=self.table_name,
                retention_days=self.retention_days,
                rows_before=rows_before,
                rows_to_delete=0,
                rows_deleted=0,
                rows_after=rows_before,
                cutoff_time=plan.cutoff_time,
                elapsed_seconds=round(
                    time.monotonic() - started,
                    3,
                ),
            )

        backup_path: Optional[Path] = None

        # Bắt buộc backup thành công trước khi thực hiện DELETE
        # nếu người dùng yêu cầu backup.
        if create_backup:
            backup_path = self.create_backup()

        cutoff_timestamp_ms = plan.cutoff_timestamp_ms

        where_parts = ["timestamp < ?"]
        params: list[Any] = [cutoff_timestamp_ms]

        if self.symbols:
            placeholders = ",".join(
                "?" for _ in self.symbols
            )

            where_parts.append(
                f"UPPER(symbol) IN ({placeholders})"
            )
            params.extend(self.symbols)

        where_sql = " AND ".join(where_parts)

        deleted = 0

        try:
            with self._connect() as connection:
                self._validate_schema(connection)

                # Khóa ghi trong transaction để các batch xóa
                # không bị xen kẽ bởi writer khác.
                connection.execute("BEGIN IMMEDIATE")

                while True:
                    cursor = connection.execute(
                        f"""
                        DELETE FROM {quote_identifier(self.table_name)}
                        WHERE id IN (
                            SELECT id
                            FROM {quote_identifier(self.table_name)}
                            WHERE {where_sql}
                            ORDER BY timestamp ASC, id ASC
                            LIMIT ?
                        )
                        """,
                        [*params, self.batch_size],
                    )

                    batch_deleted = max(cursor.rowcount, 0)
                    deleted += batch_deleted

                    if batch_deleted == 0:
                        break

                    logger.info(
                        "Retention batch: deleted=%s, total_deleted=%s",
                        batch_deleted,
                        deleted,
                    )

                connection.commit()

                rows_after = int(
                    connection.execute(
                        f"""
                        SELECT COUNT(*)
                        FROM {quote_identifier(self.table_name)}
                        """
                    ).fetchone()[0]
                )

                if vacuum:
                    # VACUUM không được chạy bên trong transaction.
                    connection.execute("VACUUM")

        except Exception:
            logger.exception(
                "Retention thất bại. Transaction sẽ được rollback "
                "nếu chưa commit."
            )
            raise

        message = (
            f"Retention hoàn tất: đã xóa {deleted:,} bản ghi; "
            f"còn lại {rows_after:,} bản ghi."
        )

        logger.info(message)

        return RetentionResult(
            success=True,
            message=message,
            sqlite_path=str(self.sqlite_path),
            table_name=self.table_name,
            retention_days=self.retention_days,
            rows_before=rows_before,
            rows_to_delete=rows_to_delete,
            rows_deleted=deleted,
            rows_after=rows_after,
            cutoff_time=plan.cutoff_time,
            backup_path=(
                str(backup_path) if backup_path else None
            ),
            elapsed_seconds=round(
                time.monotonic() - started,
                3,
            ),
        )


# ============================================================
# REPORT
# ============================================================

def print_inspection(info: TableInfo) -> None:
    print("\n========== FOOTPRINT RETENTION STATUS ==========")
    print(f"Database: {DEFAULT_SQLITE_PATH}")
    print(f"Table: {info.table_name}")
    print(f"Exists: {info.exists}")
    print(f"Rows: {info.row_count:,}")
    print(f"Oldest trade: {info.oldest_time or 'N/A'}")
    print(f"Newest trade: {info.newest_time or 'N/A'}")
    print(
        f"Database size: {format_bytes(info.database_size_bytes)}"
    )
    print("================================================\n")


def print_plan(plan: RetentionPlan) -> None:
    print("\n========== RETENTION PLAN ==========")
    print(f"SQLite: {plan.sqlite_path}")
    print(f"Table: {plan.table_name}")
    print(f"Retention: {plan.retention_days} days")
    print(f"Cutoff UTC: {plan.cutoff_time}")
    print(f"Total rows: {plan.total_rows:,}")
    print(f"Rows to delete: {plan.rows_to_delete:,}")
    print(f"Rows to keep: {plan.rows_to_keep:,}")
    print(
        "Symbols: "
        + (
            ", ".join(plan.symbols)
            if plan.symbols
            else "ALL"
        )
    )
    print("Mode: DRY-RUN")
    print("====================================\n")


def print_result(result: RetentionResult) -> None:
    print("\n========== RETENTION RESULT ==========")
    print(f"Success: {result.success}")
    print(f"Message: {result.message}")
    print(f"SQLite: {result.sqlite_path}")
    print(f"Retention days: {result.retention_days}")
    print(f"Rows before: {result.rows_before:,}")
    print(f"Rows to delete: {result.rows_to_delete:,}")
    print(f"Rows deleted: {result.rows_deleted:,}")
    print(f"Rows after: {result.rows_after:,}")
    print(f"Cutoff UTC: {result.cutoff_time}")
    print(f"Backup: {result.backup_path or 'N/A'}")
    print(f"Elapsed: {result.elapsed_seconds:.3f}s")
    print("======================================\n")


# ============================================================
# CLI
# ============================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Quản lý thời gian lưu dữ liệu Footprint SQLite."
        )
    )

    parser.add_argument(
        "--sqlite-path",
        type=str,
        default=os.getenv(
            "FOOTPRINT_SQLITE_PATH",
            str(DEFAULT_SQLITE_PATH),
        ),
        help="Đường dẫn file SQLite.",
    )

    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_RETENTION_DAYS,
        help="Số ngày giữ dữ liệu giao dịch. Mặc định: 90.",
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="Số bản ghi xóa tối đa trong mỗi batch.",
    )

    parser.add_argument(
        "--symbols",
        type=str,
        default=None,
        help="Danh sách mã cổ phiếu, phân cách bằng dấu phẩy.",
    )

    parser.add_argument(
        "--check",
        action="store_true",
        help="Chỉ kiểm tra dung lượng và dữ liệu hiện có.",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Chỉ xem trước, không xóa dữ liệu.",
    )

    parser.add_argument(
        "--execute",
        action="store_true",
        help="Cho phép xóa dữ liệu cũ.",
    )

    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Không tạo backup trước khi xóa (không khuyến nghị).",
    )

    parser.add_argument(
        "--vacuum",
        action="store_true",
        help="Chạy VACUUM sau khi xóa.",
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="Xuất kết quả dạng JSON.",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        if args.days < 1:
            raise ValueError("--days phải lớn hơn hoặc bằng 1.")

        if args.batch_size < 1:
            raise ValueError(
                "--batch-size phải lớn hơn hoặc bằng 1."
            )

        manager = FootprintRetentionManager(
            sqlite_path=args.sqlite_path,
            retention_days=args.days,
            batch_size=args.batch_size,
            symbols=normalize_symbols(args.symbols),
        )

        if args.check:
            info = manager.inspect()

            if args.json:
                print(json.dumps(
                    asdict(info),
                    ensure_ascii=False,
                    indent=2,
                ))
            else:
                print_inspection(info)

            return 0 if info.exists else 1

        # Chỉ cho phép xóa nếu người dùng truyền --execute.
        # Nếu không truyền, luôn chạy dry-run.
        dry_run = not args.execute or args.dry_run

        if args.execute and args.dry_run:
            logger.warning(
                "Bạn đã truyền cả --execute và --dry-run. "
                "Dry-run được ưu tiên: không xóa dữ liệu."
            )

        if not dry_run and not args.no_backup:
            logger.info(
                "Backup được bật. SQLite sẽ được sao lưu "
                "trước khi xóa dữ liệu."
            )

        if not dry_run and args.no_backup:
            logger.warning(
                "CẢNH BÁO: bạn đã tắt backup trước khi xóa."
            )

        if dry_run:
            plan = manager.build_plan()

            if args.json:
                print(json.dumps(
                    asdict(plan),
                    ensure_ascii=False,
                    indent=2,
                ))
            else:
                print_plan(plan)

            return 0

        result = manager.execute(
            dry_run=False,
            create_backup=not args.no_backup,
            vacuum=args.vacuum,
        )

        if args.json:
            print(json.dumps(
                asdict(result),
                ensure_ascii=False,
                indent=2,
            ))
        else:
            print_result(result)

        return 0 if result.success else 1

    except KeyboardInterrupt:
        logger.warning("Retention bị người dùng dừng.")
        return 130

    except Exception as exc:
        logger.exception("Retention thất bại: %s", exc)

        if args.json:
            print(json.dumps(
                {
                    "success": False,
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            ))
        else:
            print(f"\nLỖI RETENTION: {exc}", file=sys.stderr)

        return 1


if __name__ == "__main__":
    raise SystemExit(main())

