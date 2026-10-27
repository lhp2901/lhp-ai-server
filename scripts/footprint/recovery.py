"""
Footprint Recovery Manager
==========================

Phục hồi và chẩn đoán quá trình đồng bộ SQLite -> Supabase.

Tương thích với:
    scripts/footprint/supabase_store.py
    scripts/footprint/sync_manager.py
    scripts/footprint/sync_state.py
    scripts/footprint/health_monitor.py
    scripts/footprint/data_validator.py

Chức năng:
- Kiểm tra SQLite và sync state.
- Phát hiện checkpoint không hợp lệ.
- Phát hiện trạng thái RUNNING có thể đã bị treo.
- Kiểm tra cấu hình Supabase.
- Chạy đồng bộ tiếp từ checkpoint hiện tại.
- Hỗ trợ dry-run.
- Hỗ trợ chọn start ID rõ ràng khi người quản trị yêu cầu.
- Ghi nhật ký phục hồi.
- Không tự xóa dữ liệu.
- Không tự tăng checkpoint qua bản ghi lỗi.
- Không tự đánh dấu SUCCESS nếu chưa có kết quả đồng bộ thành công.

Chạy từ thư mục gốc dự án:

    python -m scripts.footprint.recovery --check

Kiểm tra dữ liệu SQLite:

    python -m scripts.footprint.recovery --validate

Chạy thử phục hồi:

    python -m scripts.footprint.recovery --dry-run

Phục hồi đồng bộ thật:

    python -m scripts.footprint.recovery --run

Chạy lại từ một ID được chỉ định:

    python -m scripts.footprint.recovery --run --start-id 600

LƯU Ý:
--start-id chỉ là điểm bắt đầu đọc SQLite. Nó không tự sửa
checkpoint trong Supabase và không chứng minh dữ liệu trước đó
đã được đồng bộ đầy đủ.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv


# ============================================================
# 1. PROJECT PATHS
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]

ENV_PATH = PROJECT_ROOT / ".env"

DEFAULT_SQLITE_PATH = PROJECT_ROOT / "data" / "footprint.db"

LOG_DIR = PROJECT_ROOT / "logs"

LOG_FILE = LOG_DIR / "footprint_recovery.log"

DEFAULT_SYNC_NAME = "sqlite_to_supabase"

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


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso() -> str:
    return utc_now().isoformat()


DEFAULT_STALE_RUNNING_SECONDS = env_int(
    "FOOTPRINT_RECOVERY_STALE_RUNNING_SECONDS",
    600,
)

DEFAULT_MAX_BATCHES = max(
    1,
    env_int("FOOTPRINT_RECOVERY_MAX_BATCHES", 5),
)


# ============================================================
# 3. LOGGING
# ============================================================

LOG_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("footprint.recovery")

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

class RecoveryError(RuntimeError):
    """Lỗi phục hồi Footprint."""


class RecoveryConfigurationError(RecoveryError):
    """Cấu hình phục hồi không hợp lệ."""


class RecoverySafetyError(RecoveryError):
    """Không đủ điều kiện an toàn để phục hồi."""


# ============================================================
# 5. SAFE HELPERS
# ============================================================

def parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None

    try:
        text = str(value).strip()

        if text.endswith("Z"):
            text = text[:-1] + "+00:00"

        parsed = datetime.fromisoformat(text)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    except (TypeError, ValueError, OverflowError):
        return None


def age_seconds(value: Any) -> Optional[float]:
    parsed = parse_datetime(value)

    if parsed is None:
        return None

    return max(
        0.0,
        (utc_now() - parsed).total_seconds(),
    )


def json_safe(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value

    except (TypeError, ValueError):
        return str(value)


# ============================================================
# 6. SQLITE INSPECTION
# ============================================================

def inspect_sqlite(
    sqlite_path: Path,
) -> dict[str, Any]:
    """
    Kiểm tra SQLite bằng kết nối chỉ đọc.
    Không thay đổi dữ liệu.
    """

    report: dict[str, Any] = {
        "ok": False,
        "path": str(sqlite_path),
        "exists": sqlite_path.is_file(),
        "total_trades": None,
        "min_id": None,
        "max_id": None,
        "integrity_check": None,
        "error": None,
    }

    if not sqlite_path.is_file():
        report["error"] = (
            f"Không tìm thấy database: {sqlite_path}"
        )

        return report

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
            report["error"] = (
                "Không tìm thấy bảng footprint_trades."
            )

            return report

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
            report["error"] = (
                "SQLite thiếu cột: "
                + ", ".join(sorted(missing))
            )

            return report

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

        integrity_value = (
            str(integrity[0])
            if integrity is not None
            else "unknown"
        )

        report.update(
            {
                "total_trades": int(row["total"] or 0),
                "min_id": row["min_id"],
                "max_id": row["max_id"],
                "integrity_check": integrity_value,
            }
        )

        if integrity_value.lower() != "ok":
            report["error"] = (
                f"SQLite integrity check thất bại: "
                f"{integrity_value}"
            )

            return report

        report["ok"] = True

    except sqlite3.Error as exc:
        report["error"] = str(exc)

    finally:
        if connection is not None:
            connection.close()

    return report


# ============================================================
# 7. STORE FACTORY
# ============================================================

def create_store():
    """
    Dùng SupabaseStore hiện có để bảo đảm thống nhất
    cơ chế checkpoint và upsert.
    """

    from scripts.footprint.supabase_store import SupabaseStore

    return SupabaseStore()


# ============================================================
# 8. RECOVERY MANAGER
# ============================================================

class RecoveryManager:
    """
    Điều phối kiểm tra và phục hồi.

    Không tự xóa dữ liệu hoặc tự đặt checkpoint mới.
    """

    def __init__(
        self,
        sqlite_path: Optional[str | Path] = None,
        sync_name: str = DEFAULT_SYNC_NAME,
        stale_running_seconds: int = DEFAULT_STALE_RUNNING_SECONDS,
        max_batches: int = DEFAULT_MAX_BATCHES,
    ) -> None:
        self.sqlite_path = self._resolve_sqlite_path(
            sqlite_path
        )

        self.sync_name = str(sync_name).strip()

        if not self.sync_name:
            raise RecoveryConfigurationError(
                "sync_name không được để trống."
            )

        self.stale_running_seconds = max(
            1,
            int(stale_running_seconds),
        )

        self.max_batches = max(
            1,
            int(max_batches),
        )

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

    # --------------------------------------------------------
    # 8.1 Read state
    # --------------------------------------------------------

    def read_sync_state(self) -> dict[str, Any]:
        try:
            state = self.store.get_sync_state(
                self.sync_name
            )

            if not state:
                return {
                    "ok": True,
                    "exists": False,
                    "sync_name": self.sync_name,
                    "status": "NOT_FOUND",
                    "last_local_trade_id": 0,
                    "last_attempt_at": None,
                    "last_success_at": None,
                    "updated_at": None,
                    "error_message": None,
                }

            checkpoint = state.get(
                "last_local_trade_id"
            )

            try:
                checkpoint = int(checkpoint or 0)

            except (TypeError, ValueError):
                checkpoint = None

            return {
                "ok": True,
                "exists": True,
                "sync_name": self.sync_name,
                "status": str(
                    state.get("status") or "UNKNOWN"
                ).upper(),
                "last_local_trade_id": checkpoint,
                "last_attempt_at": state.get("last_attempt_at"),
                "last_success_at": state.get("last_success_at"),
                "updated_at": state.get("updated_at"),
                "error_message": state.get("error_message"),
            }

        except Exception as exc:
            return {
                "ok": False,
                "exists": False,
                "sync_name": self.sync_name,
                "status": "UNAVAILABLE",
                "error": str(exc),
            }

    # --------------------------------------------------------
    # 8.2 Validate current environment
    # --------------------------------------------------------

    def inspect(self) -> dict[str, Any]:
        sqlite_info = inspect_sqlite(
            self.sqlite_path
        )

        store_status = self.store.get_status()

        if not store_status.get("enabled"):
            supabase_info = {
                "ok": False,
                "enabled": False,
                "message": (
                    "SUPABASE_SYNC_ENABLED chưa bật."
                ),
            }

        elif not store_status.get("configured"):
            supabase_info = {
                "ok": False,
                "enabled": True,
                "message": (
                    "Supabase chưa được cấu hình đầy đủ."
                ),
            }

        else:
            supabase_info = self.store.health_check()

        state_info = self.read_sync_state()

        warnings: list[str] = []
        errors: list[str] = []

        if not sqlite_info.get("ok"):
            errors.append(
                "SQLite không hợp lệ: "
                + str(sqlite_info.get("error"))
            )

        if not supabase_info.get("ok"):
            errors.append(
                "Supabase không sẵn sàng: "
                + str(supabase_info.get("message"))
            )

        if not state_info.get("ok"):
            errors.append(
                "Không đọc được sync state."
            )

        if state_info.get("exists"):
            status = state_info.get("status")

            checkpoint = state_info.get(
                "last_local_trade_id"
            )

            max_id = sqlite_info.get("max_id")

            if checkpoint is None:
                errors.append(
                    "Checkpoint trong sync state không hợp lệ."
                )

            elif max_id is not None and checkpoint > max_id:
                warnings.append(
                    "Checkpoint lớn hơn ID lớn nhất SQLite. "
                    "Cần xác minh đang dùng đúng database."
                )

            elif max_id is not None and checkpoint < max_id:
                warnings.append(
                    "Checkpoint thấp hơn ID lớn nhất SQLite. "
                    "Có thể còn dữ liệu chưa đồng bộ."
                )

            if status in {
                "FAILED",
                "RETRYING",
                "ERROR",
            }:
                warnings.append(
                    f"Trạng thái đồng bộ hiện tại là {status}."
                )

            if status == "RUNNING":
                state_age = age_seconds(
                    state_info.get("updated_at")
                )

                if (
                    state_age is not None
                    and state_age > self.stale_running_seconds
                ):
                    warnings.append(
                        "RUNNING có thể bị treo: "
                        "updated_at đã quá ngưỡng."
                    )

        else:
            warnings.append(
                "Chưa có sync state; chưa thể xác nhận "
                "lịch sử đồng bộ."
            )

        return {
            "checked_at": utc_iso(),
            "ok": (
                not errors
                and sqlite_info.get("ok", False)
                and supabase_info.get("ok", False)
                and state_info.get("ok", False)
            ),
            "sqlite": sqlite_info,
            "supabase": supabase_info,
            "sync_state": state_info,
            "warnings": warnings,
            "errors": errors,
        }

    # --------------------------------------------------------
    # 8.3 Validate source data
    # --------------------------------------------------------

    def validate_source(
        self,
        limit: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        Gọi validator hiện có.

        Không sửa dữ liệu.
        """

        from scripts.footprint.data_validator import validate_sqlite

        return validate_sqlite(
            self.sqlite_path,
            limit=limit,
        )

    # --------------------------------------------------------
    # 8.4 Recover
    # --------------------------------------------------------

    def recover(
        self,
        *,
        dry_run: bool = False,
        start_id: Optional[int] = None,
        max_batches: Optional[int] = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """
        Thực hiện phục hồi đồng bộ.

        Mặc định:
        - Không ghi nếu SQLite hoặc Supabase không khỏe.
        - Tiếp tục từ checkpoint mà SupabaseStore đang quản lý.
        - Không tự sửa checkpoint.
        - Không tự bỏ qua bản ghi lỗi.

        start_id:
            Nếu được chỉ định, đọc SQLite từ ID lớn hơn start_id.
            Đây không phải thao tác đặt checkpoint.

        force:
            Cho phép tiếp tục khi sync state có trạng thái FAILED,
            RETRYING hoặc RUNNING. Không bỏ qua kiểm tra kết nối.
            Chỉ dùng khi đã xác minh không có worker khác đang chạy.
        """

        started = time.monotonic()

        result: dict[str, Any] = {
            "ok": False,
            "dry_run": dry_run,
            "sync_name": self.sync_name,
            "sqlite_path": str(self.sqlite_path),
            "started_at": utc_iso(),
            "finished_at": None,
            "duration_seconds": 0,
            "inspection": None,
            "sync_result": None,
            "error": None,
        }

        try:
            inspection = self.inspect()

            result["inspection"] = inspection

            if not inspection.get("sqlite", {}).get("ok"):
                raise RecoverySafetyError(
                    "SQLite chưa sẵn sàng."
                )

            if not inspection.get("supabase", {}).get("ok"):
                raise RecoverySafetyError(
                    "Supabase chưa sẵn sàng."
                )

            state = inspection.get("sync_state") or {}

            status = str(
                state.get("status") or "UNKNOWN"
            ).upper()

            if (
                not force
                and status in {
                    "RUNNING",
                    "FAILED",
                    "RETRYING",
                    "ERROR",
                }
            ):
                raise RecoverySafetyError(
                    f"Sync state hiện là {status}. "
                    "Hãy kiểm tra worker trước. Nếu đã xác minh "
                    "không có tiến trình khác chạy, có thể dùng --force."
                )

            if start_id is not None and start_id < 0:
                raise RecoveryConfigurationError(
                    "start_id không được âm."
                )

            effective_max_batches = (
                self.max_batches
                if max_batches is None
                else max(1, int(max_batches))
            )

            logger.info(
                "Bắt đầu recovery: sync_name=%s dry_run=%s "
                "start_id=%s max_batches=%s",
                self.sync_name,
                dry_run,
                start_id,
                effective_max_batches,
            )

            sync_result = self.store.sync_sqlite_trades(
                self.sqlite_path,
                start_id=start_id,
                max_batches=effective_max_batches,
                dry_run=dry_run,
                sync_name=self.sync_name,
            )

            result["sync_result"] = sync_result

            if not sync_result.get("ok", False):
                raise RecoveryError(
                    "Đồng bộ có bản ghi lỗi. "
                    "Không đánh dấu phục hồi thành công."
                )

            result["ok"] = True

            logger.info(
                "Recovery hoàn tất: read=%s upserted=%s failed=%s "
                "last_local_id=%s dry_run=%s",
                sync_result.get("read"),
                sync_result.get("upserted"),
                sync_result.get("failed"),
                sync_result.get("last_local_id"),
                dry_run,
            )

        except Exception as exc:
            result["error"] = str(exc)

            logger.exception(
                "Recovery thất bại."
            )

        finally:
            result["finished_at"] = utc_iso()

            result["duration_seconds"] = round(
                time.monotonic() - started,
                3,
            )

        return result


# ============================================================
# 9. REPORTING
# ============================================================

def print_report(
    report: dict[str, Any],
    *,
    as_json: bool = False,
) -> None:
    if as_json:
        print(
            json.dumps(
                report,
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )

        return

    print()
    print("=" * 68)
    print("LHP-FI FOOTPRINT RECOVERY")
    print("=" * 68)

    print(f"Thời gian : {report.get('checked_at', report.get('started_at'))}")
    print(f"Kết quả   : {report.get('ok')}")
    print(f"SQLite    : {report.get('sqlite_path')}")

    sqlite_info = report.get("sqlite") or {}

    if not sqlite_info:
        inspection = report.get("inspection") or {}
        sqlite_info = inspection.get("sqlite") or {}

    print()
    print("--- SQLITE ---")
    print(f"OK        : {sqlite_info.get('ok')}")
    print(f"Tổng trade: {sqlite_info.get('total_trades')}")
    print(f"Min ID    : {sqlite_info.get('min_id')}")
    print(f"Max ID    : {sqlite_info.get('max_id')}")

    inspection = report.get("inspection") or {}

    if inspection:
        supabase_info = inspection.get("supabase") or {}
        state_info = inspection.get("sync_state") or {}

        print()
        print("--- SUPABASE ---")
        print(f"OK        : {supabase_info.get('ok')}")
        print(f"Enabled   : {supabase_info.get('enabled')}")
        print(f"Message   : {supabase_info.get('message')}")

        print()
        print("--- SYNC STATE ---")
        print(f"Status    : {state_info.get('status')}")
        print(
            "Checkpoint: "
            f"{state_info.get('last_local_trade_id')}"
        )
        print(f"Last success: {state_info.get('last_success_at')}")

        for warning in inspection.get("warnings", []):
            print(f"[WARNING] {warning}")

        for error in inspection.get("errors", []):
            print(f"[ERROR] {error}")

    sync_result = report.get("sync_result") or {}

    if sync_result:
        print()
        print("--- RECOVERY RESULT ---")

        for key in (
            "ok",
            "dry_run",
            "read",
            "upserted",
            "failed",
            "last_local_id",
        ):
            print(f"{key:15}: {sync_result.get(key)}")

    if report.get("error"):
        print()
        print(f"Lỗi: {report.get('error')}")

    print()
    print(f"Log file: {LOG_FILE}")
    print("=" * 68)
    print()


# ============================================================
# 10. CLI
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Kiểm tra và phục hồi đồng bộ LHP-FI Footprint."
        )
    )

    parser.add_argument(
        "--sqlite-path",
        default=str(DEFAULT_SQLITE_PATH),
        help="Đường dẫn SQLite.",
    )

    parser.add_argument(
        "--sync-name",
        default=DEFAULT_SYNC_NAME,
        help="Tên sync state.",
    )

    parser.add_argument(
        "--check",
        action="store_true",
        help="Chỉ kiểm tra hệ thống, không đồng bộ.",
    )

    parser.add_argument(
        "--validate",
        action="store_true",
        help="Kiểm tra chất lượng dữ liệu SQLite.",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Giới hạn số dòng khi dùng --validate.",
    )

    parser.add_argument(
        "--run",
        action="store_true",
        help="Thực hiện phục hồi đồng bộ thật.",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Chạy thử, không ghi lên Supabase.",
    )

    parser.add_argument(
        "--start-id",
        type=int,
        default=None,
        help="Điểm bắt đầu đọc SQLite; không tự sửa checkpoint.",
    )

    parser.add_argument(
        "--max-batches",
        type=int,
        default=DEFAULT_MAX_BATCHES,
        help="Số batch tối đa mỗi lượt.",
    )

    parser.add_argument(
        "--stale-running-seconds",
        type=int,
        default=DEFAULT_STALE_RUNNING_SECONDS,
        help="Ngưỡng cảnh báo RUNNING cũ.",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Cho phép phục hồi khi trạng thái đang lỗi/treo.",
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="Xuất JSON.",
    )

    args = parser.parse_args()

    if args.max_batches < 1:
        parser.error("--max-batches phải >= 1.")

    if args.stale_running_seconds < 1:
        parser.error("--stale-running-seconds phải >= 1.")

    if args.start_id is not None and args.start_id < 0:
        parser.error("--start-id không được âm.")

    if args.limit is not None and args.limit < 1:
        parser.error("--limit phải >= 1.")

    selected_actions = sum(
        [
            args.check,
            args.validate,
            args.run,
            args.dry_run,
        ]
    )

    if selected_actions > 1:
        parser.error(
            "Chọn một trong --check, --validate, --run hoặc --dry-run."
        )

    try:
        manager = RecoveryManager(
            sqlite_path=args.sqlite_path,
            sync_name=args.sync_name,
            stale_running_seconds=args.stale_running_seconds,
            max_batches=args.max_batches,
        )

        if args.validate:
            report = manager.validate_source(
                limit=args.limit,
            )

            print_report(
                report,
                as_json=args.json,
            )

            return 0 if report.get("ok") else 1

        if args.run or args.dry_run:
            report = manager.recover(
                dry_run=args.dry_run,
                start_id=args.start_id,
                max_batches=args.max_batches,
                force=args.force,
            )

            print_report(
                report,
                as_json=args.json,
            )

            return 0 if report.get("ok") else 1

        report = manager.inspect()

        print_report(
            report,
            as_json=args.json,
        )

        return 0 if report.get("ok") else 1

    except Exception as exc:
        logger.exception(
            "Không thể khởi chạy RecoveryManager."
        )

        print(
            f"Lỗi RecoveryManager: {exc}",
            file=sys.stderr,
        )

        return 1


if __name__ == "__main__":
    raise SystemExit(main())

