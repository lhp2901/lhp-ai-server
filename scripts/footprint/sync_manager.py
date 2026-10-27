"""
Footprint Sync Manager
======================

Điều phối đồng bộ dữ liệu:
    SQLite data/footprint.db
        -> Supabase footprint_trades

Chức năng:
- Kiểm tra SQLite và cấu hình Supabase.
- Kiểm tra kết nối trước khi đồng bộ thật.
- Ngăn nhiều tiến trình sync_manager chạy đồng thời.
- Tiếp tục đồng bộ từ checkpoint do SupabaseStore quản lý.
- Chạy một lần hoặc chạy liên tục.
- Retry khi xảy ra lỗi tạm thời.
- Ghi log đầy đủ.
- Hỗ trợ dry-run.
- Không ghi secret key vào log.
- Không tự xóa dữ liệu hoặc tự sửa checkpoint.

Chạy từ thư mục gốc dự án:

    python -m scripts.footprint.sync_manager --once

Chạy thử không ghi Supabase:

    python -m scripts.footprint.sync_manager --dry-run --once

Chạy liên tục:

    python -m scripts.footprint.sync_manager

Giới hạn mỗi lượt:

    python -m scripts.footprint.sync_manager --once --max-batches 5

Chỉ định SQLite:

    python -m scripts.footprint.sync_manager \
        --sqlite-path data/footprint.db --once
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from dotenv import load_dotenv


# ============================================================
# 1. PROJECT PATHS
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]

ENV_PATH = PROJECT_ROOT / ".env"

DEFAULT_SQLITE_PATH = PROJECT_ROOT / "data" / "footprint.db"

LOG_DIR = PROJECT_ROOT / "logs"

LOG_FILE = LOG_DIR / "footprint_sync_manager.log"

LOCK_FILE = LOG_DIR / "footprint_sync_manager.lock"

DEFAULT_SYNC_NAME = "sqlite_to_supabase"


# Nạp .env từ thư mục gốc dự án.
load_dotenv(ENV_PATH, override=True)


# ============================================================
# 2. CONFIGURATION
# ============================================================

def env_int(name: str, default: int) -> int:
    value = os.getenv(name)

    if value is None or not value.strip():
        return default

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)

    if value is None or not value.strip():
        return default

    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)

    if value is None:
        return default

    return value.strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
        "enabled",
    }


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso() -> str:
    return utc_now().isoformat()


DEFAULT_INTERVAL_SECONDS = max(
    1,
    env_int("FOOTPRINT_SYNC_INTERVAL_SECONDS", 10),
)

DEFAULT_MAX_BATCHES = max(
    1,
    env_int("FOOTPRINT_SYNC_MAX_BATCHES", 5),
)

DEFAULT_RETRY_LIMIT = max(
    0,
    env_int("FOOTPRINT_SYNC_RETRY_LIMIT", 3),
)

DEFAULT_RETRY_DELAY_SECONDS = max(
    1.0,
    env_float("FOOTPRINT_SYNC_RETRY_DELAY_SECONDS", 5.0),
)


# ============================================================
# 3. LOGGING
# ============================================================

LOG_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("footprint.sync_manager")

logger.setLevel(logging.INFO)

logger.propagate = False

if not logger.handlers:
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    )

    file_handler = logging.FileHandler(
        LOG_FILE,
        encoding="utf-8",
    )

    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stderr)

    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)

    logger.addHandler(console_handler)


# ============================================================
# 4. EXCEPTIONS
# ============================================================

class SyncManagerError(RuntimeError):
    """Lỗi chung của SyncManager."""


class SyncAlreadyRunningError(SyncManagerError):
    """Một tiến trình SyncManager khác đang giữ khóa."""


class SyncConfigurationError(SyncManagerError):
    """Cấu hình đồng bộ không hợp lệ."""


class SyncExecutionError(SyncManagerError):
    """Đồng bộ thất bại."""


# ============================================================
# 5. SQLITE VALIDATION
# ============================================================

def inspect_sqlite(sqlite_path: Path) -> dict[str, Any]:
    """
    Kiểm tra database trước khi đồng bộ.

    Không tạo bảng, không sửa dữ liệu và không xóa bản ghi.
    """

    result: dict[str, Any] = {
        "ok": False,
        "path": str(sqlite_path),
        "exists": sqlite_path.is_file(),
        "total_trades": 0,
        "min_id": None,
        "max_id": None,
        "error": None,
    }

    if not sqlite_path.is_file():
        result["error"] = (
            f"Không tìm thấy SQLite database: {sqlite_path}"
        )

        return result

    connection = None

    try:
        uri = sqlite_path.resolve().as_uri() + "?mode=ro"

        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=15,
        )

        connection.row_factory = sqlite3.Row

        table = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'footprint_trades'
            """
        ).fetchone()

        if table is None:
            result["error"] = (
                "SQLite không có bảng footprint_trades."
            )

            return result

        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(footprint_trades)"
            ).fetchall()
        }

        required = {
            "id",
            "symbol",
            "trade_id",
            "timestamp",
            "price",
            "volume",
            "side",
            "received_at",
            "raw_json",
        }

        missing = required - columns

        if missing:
            result["error"] = (
                "Bảng footprint_trades thiếu cột: "
                + ", ".join(sorted(missing))
            )

            return result

        row = connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                MIN(id) AS min_id,
                MAX(id) AS max_id
            FROM footprint_trades
            """
        ).fetchone()

        integrity = connection.execute(
            "PRAGMA quick_check"
        ).fetchone()

        integrity_result = (
            str(integrity[0])
            if integrity is not None
            else "unknown"
        )

        result.update(
            {
                "total_trades": int(row["total"] or 0),
                "min_id": row["min_id"],
                "max_id": row["max_id"],
                "integrity_check": integrity_result,
            }
        )

        if integrity_result.lower() != "ok":
            result["error"] = (
                "SQLite quick_check thất bại: "
                + integrity_result
            )

            return result

        result["ok"] = True

    except sqlite3.Error as exc:
        result["error"] = str(exc)

    finally:
        if connection is not None:
            connection.close()

    return result


# ============================================================
# 6. PROCESS LOCK
# ============================================================

def _pid_is_running(pid: int) -> bool:
    """
    Kiểm tra PID có tồn tại hay không.

    Trên Windows, os.kill(pid, 0) có thể trả PermissionError
    nếu PID tồn tại nhưng tiến trình thuộc quyền khác.
    """

    if pid <= 0:
        return False

    try:
        os.kill(pid, 0)
        return True

    except ProcessLookupError:
        return False

    except PermissionError:
        return True

    except OSError:
        return False


class ProcessLock:
    """
    Khóa file nhằm tránh hai sync_manager cùng chạy.

    Không tự xóa khóa nếu chưa xác minh PID cũ đã dừng.
    """

    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self.acquired = False

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        payload = {
            "pid": os.getpid(),
            "started_at": utc_iso(),
        }

        for _ in range(2):
            try:
                fd = os.open(
                    str(self.lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )

                with os.fdopen(fd, "w", encoding="utf-8") as file:
                    json.dump(payload, file)

                self.acquired = True

                logger.info(
                    "Đã lấy process lock. pid=%s",
                    os.getpid(),
                )

                return

            except FileExistsError:
                old_pid = None

                try:
                    content = json.loads(
                        self.lock_path.read_text(
                            encoding="utf-8"
                        )
                    )

                    old_pid = int(content.get("pid") or 0)

                except Exception:
                    old_pid = None

                if old_pid and _pid_is_running(old_pid):
                    raise SyncAlreadyRunningError(
                        "SyncManager khác đang chạy "
                        f"với PID {old_pid}. "
                        "Không khởi chạy thêm tiến trình."
                    )

                # Lock cũ không đọc được hoặc PID không tồn tại.
                # Chỉ xóa lock nếu nội dung file chưa thay đổi
                # so với nội dung đã kiểm tra.
                try:
                    current_content = self.lock_path.read_text(
                        encoding="utf-8"
                    )

                    if old_pid and _pid_is_running(old_pid):
                        raise SyncAlreadyRunningError(
                            "Phát hiện PID lock vẫn đang hoạt động."
                        )

                    self.lock_path.unlink(missing_ok=True)

                    logger.warning(
                        "Đã xóa lock cũ không còn PID hoạt động."
                    )

                except FileNotFoundError:
                    pass

        raise SyncManagerError(
            "Không thể lấy process lock."
        )

    def release(self) -> None:
        if not self.acquired:
            return

        try:
            content = json.loads(
                self.lock_path.read_text(encoding="utf-8")
            )

            owner_pid = int(content.get("pid") or 0)

            # Chỉ xóa lock do chính tiến trình này tạo.
            if owner_pid == os.getpid():
                self.lock_path.unlink(missing_ok=True)

                logger.info("Đã giải phóng process lock.")

        except FileNotFoundError:
            pass

        except Exception:
            logger.exception(
                "Không thể giải phóng process lock."
            )

        finally:
            self.acquired = False


@contextmanager
def process_lock() -> Iterator[ProcessLock]:
    lock = ProcessLock(LOCK_FILE)

    lock.acquire()

    try:
        yield lock

    finally:
        lock.release()


# ============================================================
# 7. SUPABASE STORE
# ============================================================

def create_store():
    """
    Dùng SupabaseStore hiện có để bảo đảm đồng nhất cấu hình,
    retry và quy tắc chuẩn hóa dữ liệu.
    """

    from scripts.footprint.supabase_store import SupabaseStore

    return SupabaseStore()


def validate_supabase(store, dry_run: bool) -> dict[str, Any]:
    status = store.get_status()

    result = {
        "ok": False,
        "enabled": bool(status.get("enabled")),
        "configured": bool(status.get("configured")),
        "message": None,
    }

    if not status.get("configured"):
        result["message"] = (
            "Supabase chưa được cấu hình đầy đủ."
        )

        return result

    if not dry_run and not status.get("enabled"):
        result["message"] = (
            "SUPABASE_SYNC_ENABLED chưa bật."
        )

        return result

    # Dry-run không cần kết nối thật để ghi.
    # Tuy nhiên vẫn kiểm tra cấu hình có hiện diện hay không.
    if dry_run:
        result["ok"] = True
        result["message"] = (
            "Cấu hình hợp lệ; dry-run không ghi dữ liệu."
        )

        return result

    health = store.health_check()

    result["ok"] = bool(health.get("ok"))

    result["message"] = health.get("message")

    return result


# ============================================================
# 8. SYNC MANAGER
# ============================================================

class SyncManager:
    """
    Điều phối một lượt hoặc nhiều lượt đồng bộ.

    Store vẫn là thành phần duy nhất chịu trách nhiệm:
    - đọc checkpoint;
    - lấy dữ liệu theo ID tăng dần;
    - upsert dữ liệu;
    - cập nhật sync state.

    Manager không tự tăng checkpoint, tránh làm mất dữ liệu.
    """

    def __init__(
        self,
        sqlite_path: Optional[str | Path] = None,
        sync_name: str = DEFAULT_SYNC_NAME,
        max_batches: Optional[int] = None,
        dry_run: bool = False,
    ):
        self.sqlite_path = self._resolve_sqlite_path(
            sqlite_path
        )

        self.sync_name = sync_name

        self.max_batches = max_batches

        self.dry_run = dry_run

        self.store = create_store()

    @staticmethod
    def _resolve_sqlite_path(
        sqlite_path: Optional[str | Path],
    ) -> Path:
        if sqlite_path is None:
            configured = os.getenv(
                "FOOTPRINT_SQLITE_PATH",
                str(DEFAULT_SQLITE_PATH),
            )

            path = Path(configured)

        else:
            path = Path(sqlite_path)

        if not path.is_absolute():
            path = PROJECT_ROOT / path

        return path.expanduser().resolve()

    def validate(self) -> dict[str, Any]:
        """
        Kiểm tra SQLite và cấu hình Supabase.
        """

        sqlite_info = inspect_sqlite(
            self.sqlite_path
        )

        supabase_info = validate_supabase(
            self.store,
            dry_run=self.dry_run,
        )

        report = {
            "ok": (
                bool(sqlite_info.get("ok"))
                and bool(supabase_info.get("ok"))
            ),
            "sqlite": sqlite_info,
            "supabase": supabase_info,
        }

        return report

    def run_once(self) -> dict[str, Any]:
        """
        Thực hiện một lượt đồng bộ.

        Khi có lỗi, giữ checkpoint do SupabaseStore quản lý.
        Không tự nhảy checkpoint qua bản ghi lỗi.
        """

        started = time.monotonic()

        result: dict[str, Any] = {
            "ok": False,
            "dry_run": self.dry_run,
            "sync_name": self.sync_name,
            "sqlite_path": str(self.sqlite_path),
            "started_at": utc_iso(),
            "finished_at": None,
            "duration_seconds": 0,
            "validation": None,
            "sync_result": None,
            "error": None,
        }

        logger.info(
            "Bắt đầu lượt sync. sync_name=%s dry_run=%s max_batches=%s",
            self.sync_name,
            self.dry_run,
            self.max_batches,
        )

        try:
            validation = self.validate()

            result["validation"] = validation

            if not validation["ok"]:
                raise SyncConfigurationError(
                    "Kiểm tra cấu hình thất bại: "
                    + json.dumps(
                        validation,
                        ensure_ascii=False,
                        default=str,
                    )
                )

            sqlite_info = validation["sqlite"]

            logger.info(
                "SQLite OK. total=%s min_id=%s max_id=%s",
                sqlite_info.get("total_trades"),
                sqlite_info.get("min_id"),
                sqlite_info.get("max_id"),
            )

            if sqlite_info.get("total_trades", 0) == 0:
                logger.info(
                    "SQLite chưa có giao dịch. Không có dữ liệu để sync."
                )

            sync_result = self.store.sync_sqlite_trades(
                self.sqlite_path,
                max_batches=self.max_batches,
                dry_run=self.dry_run,
                sync_name=self.sync_name,
            )

            result["sync_result"] = sync_result

            if not sync_result.get("ok", False):
                result["error"] = (
                    "SupabaseStore báo có giao dịch lỗi."
                )

                logger.error(
                    "Lượt sync chưa thành công. "
                    "read=%s upserted=%s failed=%s last_local_id=%s",
                    sync_result.get("read"),
                    sync_result.get("upserted"),
                    sync_result.get("failed"),
                    sync_result.get("last_local_id"),
                )

                return result

            result["ok"] = True

            logger.info(
                "Lượt sync hoàn tất. read=%s upserted=%s "
                "failed=%s last_local_id=%s dry_run=%s",
                sync_result.get("read"),
                sync_result.get("upserted"),
                sync_result.get("failed"),
                sync_result.get("last_local_id"),
                self.dry_run,
            )

            return result

        except Exception as exc:
            result["error"] = str(exc)

            logger.exception(
                "Lượt sync gặp lỗi."
            )

            # Cố gắng đánh dấu FAILED nếu đây là sync thật.
            # Không ghi đè RETRYING nếu store đã ghi trạng thái
            # lỗi giao dịch và checkpoint chính xác.
            if not self.dry_run:
                try:
                    state = self.store.get_sync_state(
                        self.sync_name
                    )

                    status = str(
                        (state or {}).get("status") or ""
                    ).upper()

                    if status not in {"RETRYING"}:
                        self.store.update_sync_state(
                            self.sync_name,
                            last_attempt_at=utc_iso(),
                            status="FAILED",
                            error_message=str(exc)[:4000],
                        )

                except Exception:
                    logger.exception(
                        "Không thể cập nhật sync state khi lỗi."
                    )

            return result

        finally:
            result["finished_at"] = utc_iso()

            result["duration_seconds"] = round(
                time.monotonic() - started,
                3,
            )

    def run_forever(
        self,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        retry_limit: int = DEFAULT_RETRY_LIMIT,
        retry_delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS,
    ) -> None:
        """
        Chạy liên tục cho đến khi nhận Ctrl+C.

        Mỗi chu kỳ đều dùng checkpoint hiện có trong Supabase.
        """

        interval_seconds = max(1, int(interval_seconds))

        retry_limit = max(0, int(retry_limit))

        retry_delay_seconds = max(
            1.0,
            float(retry_delay_seconds),
        )

        logger.info(
            "Bắt đầu chế độ chạy liên tục. interval=%ss retry_limit=%s",
            interval_seconds,
            retry_limit,
        )

        try:
            while True:
                succeeded = False

                for attempt in range(retry_limit + 1):
                    result = self.run_once()

                    if result.get("ok"):
                        succeeded = True
                        break

                    if attempt < retry_limit:
                        delay = retry_delay_seconds * (
                            2 ** attempt
                        )

                        logger.warning(
                            "Sync thất bại lần %s/%s. "
                            "Thử lại sau %.1f giây.",
                            attempt + 1,
                            retry_limit + 1,
                            delay,
                        )

                        time.sleep(delay)

                if not succeeded:
                    logger.error(
                        "Đã hết số lần retry trong chu kỳ này."
                    )

                time.sleep(interval_seconds)

        except KeyboardInterrupt:
            logger.info(
                "Nhận Ctrl+C. Dừng SyncManager an toàn."
            )


# ============================================================
# 9. CLI
# ============================================================

def print_result(
    result: dict[str, Any],
    as_json: bool = False,
) -> None:
    if as_json:
        print(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )

        return

    print()
    print("=" * 64)
    print("FOOTPRINT SYNC MANAGER")
    print("=" * 64)

    print(f"Thành công : {result.get('ok')}")
    print(f"Dry-run    : {result.get('dry_run')}")
    print(f"Sync name  : {result.get('sync_name')}")
    print(f"SQLite     : {result.get('sqlite_path')}")
    print(f"Bắt đầu    : {result.get('started_at')}")
    print(f"Kết thúc   : {result.get('finished_at')}")
    print(f"Thời gian  : {result.get('duration_seconds')} giây")

    validation = result.get("validation") or {}

    sqlite_info = validation.get("sqlite") or {}

    supabase_info = validation.get("supabase") or {}

    print()
    print("--- SQLite ---")

    print(f"OK         : {sqlite_info.get('ok')}")
    print(f"Tổng trade : {sqlite_info.get('total_trades')}")
    print(f"Min ID     : {sqlite_info.get('min_id')}")
    print(f"Max ID     : {sqlite_info.get('max_id')}")

    if sqlite_info.get("error"):
        print(f"Lỗi        : {sqlite_info.get('error')}")

    print()
    print("--- Supabase ---")

    print(f"OK         : {supabase_info.get('ok')}")
    print(f"Enabled    : {supabase_info.get('enabled')}")
    print(f"Configured : {supabase_info.get('configured')}")
    print(f"Message    : {supabase_info.get('message')}")

    sync_result = result.get("sync_result") or {}

    print()
    print("--- Sync Result ---")

    for key in (
        "ok",
        "dry_run",
        "read",
        "upserted",
        "failed",
        "last_local_id",
    ):
        print(f"{key:15}: {sync_result.get(key)}")

    errors = sync_result.get("errors") or []

    if errors:
        print()
        print("Lỗi giao dịch:")

        for item in errors[:10]:
            print(
                json.dumps(
                    item,
                    ensure_ascii=False,
                    default=str,
                )
            )

    if result.get("error"):
        print()
        print(f"Lỗi tổng thể: {result.get('error')}")

    print()
    print(f"Log file: {LOG_FILE}")
    print("=" * 64)
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Điều phối đồng bộ Footprint SQLite -> Supabase."
        )
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help="Chỉ thực hiện một lượt đồng bộ.",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Kiểm tra dữ liệu nhưng không ghi lên Supabase.",
    )

    parser.add_argument(
        "--sqlite-path",
        type=str,
        default=None,
        help="Đường dẫn SQLite.",
    )

    parser.add_argument(
        "--sync-name",
        type=str,
        default=DEFAULT_SYNC_NAME,
        help="Tên sync state.",
    )

    parser.add_argument(
        "--max-batches",
        type=int,
        default=DEFAULT_MAX_BATCHES,
        help="Số batch tối đa mỗi lượt.",
    )

    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_SECONDS,
        help="Khoảng cách giữa các chu kỳ, tính bằng giây.",
    )

    parser.add_argument(
        "--retry-limit",
        type=int,
        default=DEFAULT_RETRY_LIMIT,
        help="Số lần retry bổ sung mỗi chu kỳ.",
    )

    parser.add_argument(
        "--retry-delay",
        type=float,
        default=DEFAULT_RETRY_DELAY_SECONDS,
        help="Thời gian retry ban đầu, tính bằng giây.",
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="Xuất kết quả JSON.",
    )

    args = parser.parse_args()

    if args.max_batches < 1:
        parser.error("--max-batches phải lớn hơn hoặc bằng 1.")

    if args.interval < 1:
        parser.error("--interval phải lớn hơn hoặc bằng 1.")

    if args.retry_limit < 0:
        parser.error("--retry-limit không được âm.")

    try:
        manager = SyncManager(
            sqlite_path=args.sqlite_path,
            sync_name=args.sync_name,
            max_batches=args.max_batches,
            dry_run=args.dry_run,
        )

        with process_lock():
            if args.once or args.dry_run:
                result = manager.run_once()

                print_result(
                    result,
                    as_json=args.json,
                )

                return 0 if result.get("ok") else 1

            manager.run_forever(
                interval_seconds=args.interval,
                retry_limit=args.retry_limit,
                retry_delay_seconds=args.retry_delay,
            )

            return 0

    except SyncAlreadyRunningError as exc:
        logger.error("%s", exc)
        print(f"Không thể khởi chạy: {exc}", file=sys.stderr)
        return 2

    except Exception as exc:
        logger.exception("SyncManager không thể khởi chạy.")
        print(f"Lỗi SyncManager: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())