"""
Footprint Health Monitor
========================

Giám sát sức khỏe hệ thống Footprint và đồng bộ SQLite -> Supabase.

Chức năng:
- Kiểm tra file SQLite và khả năng truy vấn database.
- Đếm dữ liệu footprint_trades trong SQLite.
- Xác định ID giao dịch nhỏ nhất/lớn nhất tại SQLite.
- Kiểm tra cấu hình và kết nối Supabase.
- Đọc trạng thái đồng bộ sqlite_to_supabase.
- So sánh checkpoint với dữ liệu SQLite.
- Phát hiện trạng thái đồng bộ bất thường.
- Phát hiện đồng bộ bị treo hoặc quá lâu không cập nhật.
- Ghi log ra logs/footprint_health_monitor.log.
- Trả kết quả JSON để phục vụ giám sát tự động.

Chạy từ thư mục gốc dự án:

    python -m scripts.footprint.health_monitor

Kiểm tra nghiêm ngặt, dùng exit code cho tác vụ tự động:

    python -m scripts.footprint.health_monitor --strict

Xuất JSON:

    python -m scripts.footprint.health_monitor --json

Lưu ý:
- File này chỉ giám sát, không tự đồng bộ hoặc sửa dữ liệu.
- Không ghi Supabase secret key vào log.
- Không tự sửa checkpoint nếu phát hiện sai lệch.
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
# 1. ĐƯỜNG DẪN VÀ CẤU HÌNH
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]

ENV_PATH = PROJECT_ROOT / ".env"

DEFAULT_SQLITE_PATH = PROJECT_ROOT / "data" / "footprint.db"

LOG_DIR = PROJECT_ROOT / "logs"

LOG_FILE = LOG_DIR / "footprint_health_monitor.log"

DEFAULT_SYNC_NAME = "sqlite_to_supabase"

# Thời gian tối đa cho phép không có lần đồng bộ thành công.
DEFAULT_MAX_SYNC_AGE_SECONDS = 15 * 60

# Nếu RUNNING quá thời gian này, cảnh báo có thể tiến trình đã bị treo.
DEFAULT_MAX_RUNNING_SECONDS = 10 * 60

# Nếu checkpoint không tiến triển trong khoảng thời gian này,
# cảnh báo có thể worker bị dừng hoặc gặp lỗi.
DEFAULT_MAX_PROGRESS_AGE_SECONDS = 15 * 60


load_dotenv(ENV_PATH, override=True)


def env_int(name: str, default: int) -> int:
    """Đọc biến môi trường kiểu integer an toàn."""
    value = os.getenv(name)

    if value is None or not value.strip():
        return default

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def utc_now() -> datetime:
    """Trả về thời gian UTC hiện tại."""
    return datetime.now(timezone.utc)


def utc_iso() -> str:
    return utc_now().isoformat()


def parse_datetime(value: Any) -> Optional[datetime]:
    """Chuyển ISO datetime thành datetime UTC."""
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


def elapsed_seconds(value: Any) -> Optional[float]:
    """Tính số giây từ thời điểm được cung cấp đến hiện tại."""
    parsed = parse_datetime(value)

    if parsed is None:
        return None

    return max(0.0, (utc_now() - parsed).total_seconds())


# ============================================================
# 2. LOGGING
# ============================================================

LOG_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("footprint.health_monitor")
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
# 3. SUPABASE STORE
# ============================================================

def create_supabase_store():
    """
    Khởi tạo SupabaseStore từ module hiện có.

    Không tự tạo client khác để tránh phân tán cấu hình.
    """
    from scripts.footprint.supabase_store import (
        SupabaseStore,
    )

    return SupabaseStore()


# ============================================================
# 4. KIỂM TRA SQLITE
# ============================================================

def inspect_sqlite(
    sqlite_path: Path,
    table_name: str = "footprint_trades",
) -> dict[str, Any]:
    """
    Kiểm tra SQLite và thống kê dữ liệu giao dịch.

    Hàm chỉ đọc dữ liệu, không sửa database.
    """
    result: dict[str, Any] = {
        "ok": False,
        "path": str(sqlite_path),
        "exists": sqlite_path.is_file(),
        "readable": False,
        "table_exists": False,
        "total_trades": None,
        "min_id": None,
        "max_id": None,
        "integrity_check": None,
        "error": None,
    }

    if not sqlite_path.is_file():
        result["error"] = "Không tìm thấy file SQLite."
        return result

    connection = None

    try:
        # mode=ro để bảo đảm monitor không tạo/sửa database.
        uri = sqlite_path.resolve().as_uri() + "?mode=ro"

        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=10,
        )

        connection.row_factory = sqlite3.Row

        result["readable"] = True

        tables = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
            """
        ).fetchall()

        table_names = {
            str(row["name"])
            for row in tables
        }

        if table_name not in table_names:
            result["error"] = (
                f"Không tìm thấy bảng {table_name!r} trong SQLite."
            )
            return result

        result["table_exists"] = True

        columns = connection.execute(
            f'PRAGMA table_info("{table_name}")'
        ).fetchall()

        column_names = {
            str(row["name"])
            for row in columns
        }

        if "id" not in column_names:
            result["error"] = (
                f"Bảng {table_name!r} không có cột id."
            )
            return result

        # Tên bảng được lấy từ cấu hình nội bộ, không từ request.
        row = connection.execute(
            f"""
            SELECT
                COUNT(*) AS total,
                MIN(id) AS min_id,
                MAX(id) AS max_id
            FROM "{table_name}"
            """
        ).fetchone()

        result["total_trades"] = int(row["total"] or 0)
        result["min_id"] = row["min_id"]
        result["max_id"] = row["max_id"]

        # Kiểm tra nhanh khả năng đọc cấu trúc database.
        # Không chạy VACUUM hoặc thao tác ghi.
        integrity_row = connection.execute(
            "PRAGMA quick_check"
        ).fetchone()

        integrity_value = (
            str(integrity_row[0])
            if integrity_row is not None
            else "unknown"
        )

        result["integrity_check"] = integrity_value

        if integrity_value.lower() != "ok":
            result["error"] = (
                f"SQLite quick_check trả về: {integrity_value}"
            )
            return result

        result["ok"] = True

    except sqlite3.Error as exc:
        result["error"] = str(exc)

    except Exception as exc:
        result["error"] = str(exc)

    finally:
        if connection is not None:
            connection.close()

    return result


# ============================================================
# 5. KIỂM TRA SUPABASE CONNECTION
# ============================================================

def inspect_supabase_connection(store) -> dict[str, Any]:
    """
    Kiểm tra cấu hình và kết nối Supabase.

    Chỉ trả thông tin chẩn đoán an toàn.
    Không bao giờ trả secret key.
    """
    result: dict[str, Any] = {
        "ok": False,
        "enabled": False,
        "configured": False,
        "message": None,
        "jwt_role_claim": None,
    }

    try:
        status = store.get_status()

        result["enabled"] = bool(
            status.get("enabled")
        )

        result["configured"] = bool(
            status.get("configured")
        )

        result["source"] = status.get("source")
        result["batch_size"] = status.get("batch_size")
        result["retries"] = status.get("retries")

        result["jwt_role_claim"] = status.get(
            "jwt_role_claim"
        )

        if not result["enabled"]:
            result["message"] = (
                "SUPABASE_SYNC_ENABLED chưa bật."
            )
            return result

        if not result["configured"]:
            result["message"] = (
                "Supabase chưa được cấu hình đầy đủ "
                "hoặc chưa cài thư viện supabase."
            )
            return result

        health = store.health_check()

        result["ok"] = bool(health.get("ok"))
        result["message"] = health.get("message")

        result["jwt_role_claim"] = health.get(
            "jwt_role_claim",
            result["jwt_role_claim"],
        )

        return result

    except Exception as exc:
        result["message"] = str(exc)
        return result


# ============================================================
# 6. ĐỌC TRẠNG THÁI ĐỒNG BỘ TRÊN SUPABASE
# ============================================================

def inspect_sync_state(
    store,
    sync_name: str = DEFAULT_SYNC_NAME,
) -> dict[str, Any]:
    """
    Đọc bản ghi sync state từ Supabase.

    Dùng các hằng số và cơ chế retry của SupabaseStore hiện có.
    Không tự tạo schema hoặc sửa checkpoint.
    """
    result: dict[str, Any] = {
        "ok": False,
        "exists": False,
        "sync_name": sync_name,
        "status": None,
        "last_local_trade_id": 0,
        "last_local_candle_id": 0,
        "last_attempt_at": None,
        "last_success_at": None,
        "updated_at": None,
        "error_message": None,
        "error": None,
    }

    try:
        from scripts.footprint.supabase_store import SYNC_STATE

        response = store._execute(
            "health_monitor_read_sync_state",
            lambda client: (
                client.table(SYNC_STATE)
                .select("*")
                .eq("sync_name", sync_name)
                .limit(1)
                .execute()
            ),
        )

        rows = getattr(response, "data", None)

        if rows is None:
            rows = response

        if not isinstance(rows, list) or not rows:
            result["ok"] = True
            result["exists"] = False
            result["status"] = "NOT_FOUND"
            result["error"] = (
                "Chưa có bản ghi sync state cho "
                f"sync_name={sync_name!r}."
            )
            return result

        state = rows[0]

        result.update({
            "ok": True,
            "exists": True,
            "status": state.get("status"),
            "last_local_trade_id": int(
                state.get("last_local_trade_id") or 0
            ),
            "last_local_candle_id": int(
                state.get("last_local_candle_id") or 0
            ),
            "last_attempt_at": state.get("last_attempt_at"),
            "last_success_at": state.get("last_success_at"),
            "updated_at": state.get("updated_at"),
            "error_message": state.get("error_message"),
        })

        return result

    except Exception as exc:
        result["error"] = str(exc)
        return result


# ============================================================
# 7. PHÂN TÍCH ĐỘ TRỄ VÀ CHECKPOINT
# ============================================================

def analyze_sync(
    sqlite_info: dict[str, Any],
    state_info: dict[str, Any],
    max_sync_age_seconds: int,
    max_running_seconds: int,
    max_progress_age_seconds: int,
) -> dict[str, Any]:
    """
    Phân tích tình trạng đồng bộ.

    Checkpoint là ID SQLite đã xử lý đến đâu, không phải số dòng
    chắc chắn đã có trên Supabase.

    Không kết luận dữ liệu đầy đủ chỉ dựa vào checkpoint.
    """
    warnings: list[str] = []
    errors: list[str] = []

    sqlite_max_id = sqlite_info.get("max_id")
    sqlite_total = sqlite_info.get("total_trades")

    checkpoint = int(
        state_info.get("last_local_trade_id") or 0
    )

    status = str(
        state_info.get("status") or "UNKNOWN"
    ).upper()

    last_success_at = state_info.get("last_success_at")
    last_attempt_at = state_info.get("last_attempt_at")
    updated_at = state_info.get("updated_at")

    success_age = elapsed_seconds(last_success_at)
    attempt_age = elapsed_seconds(last_attempt_at)
    updated_age = elapsed_seconds(updated_at)

    result = {
        "status": status,
        "checkpoint": checkpoint,
        "sqlite_max_id": sqlite_max_id,
        "sqlite_total_trades": sqlite_total,
        "checkpoint_reaches_current_max_id": None,
        "last_success_age_seconds": success_age,
        "last_attempt_age_seconds": attempt_age,
        "state_updated_age_seconds": updated_age,
        "warnings": warnings,
        "errors": errors,
    }

    if not sqlite_info.get("ok"):
        errors.append("SQLite không khỏe mạnh.")
        return result

    if not state_info.get("ok"):
        errors.append("Không đọc được trạng thái đồng bộ trên Supabase.")
        return result

    if not state_info.get("exists"):
        warnings.append(
            "Chưa có sync state; chưa thể xác nhận đã đồng bộ."
        )
        return result

    if sqlite_max_id is not None:
        reaches_max = checkpoint >= int(sqlite_max_id)

        result["checkpoint_reaches_current_max_id"] = reaches_max

        if int(sqlite_total or 0) > 0 and checkpoint == 0:
            warnings.append(
                "SQLite có dữ liệu nhưng checkpoint bằng 0."
            )

        elif checkpoint < int(sqlite_max_id):
            warnings.append(
                "Checkpoint thấp hơn ID lớn nhất tại SQLite. "
                "Có thể còn dữ liệu chưa đồng bộ."
            )

        elif checkpoint > int(sqlite_max_id):
            warnings.append(
                "Checkpoint lớn hơn ID lớn nhất tại SQLite. "
                "Hãy kiểm tra database hoặc file SQLite đang dùng."
            )

    if status in {"FAILED", "RETRYING", "ERROR"}:
        errors.append(
            f"Trạng thái đồng bộ là {status}."
        )

    elif status == "RUNNING":
        if updated_age is not None and updated_age > max_running_seconds:
            warnings.append(
                "Trạng thái RUNNING không cập nhật trong thời gian dài. "
                "Kiểm tra worker có bị treo hoặc đã dừng không."
            )

    elif status == "SUCCESS":
        if (
            success_age is not None
            and success_age > max_sync_age_seconds
        ):
            warnings.append(
                "Lần đồng bộ thành công gần nhất đã quá lâu."
            )

    elif status == "NOT_FOUND":
        warnings.append(
            "Chưa tìm thấy bản ghi sync state."
        )

    else:
        warnings.append(
            f"Trạng thái đồng bộ chưa được nhận diện: {status}."
        )

    if state_info.get("error_message"):
        warnings.append(
            "Sync state có error_message: "
            + str(state_info["error_message"])[:500]
        )

    # Chỉ kiểm tra thời gian tiến triển dựa trên updated_at.
    # Không thể xác định chính xác thời điểm checkpoint đổi nếu
    # schema hiện tại không lưu riêng checkpoint_updated_at.
    if (
        status == "RUNNING"
        and checkpoint > 0
        and updated_age is not None
        and updated_age > max_progress_age_seconds
    ):
        warnings.append(
            "Checkpoint có thể không tiến triển. "
            "Kiểm tra log worker để xác minh."
        )

    return result


# ============================================================
# 8. CHẠY HEALTH CHECK TOÀN HỆ THỐNG
# ============================================================

def run_health_check(
    sqlite_path: Optional[Path] = None,
    sync_name: str = DEFAULT_SYNC_NAME,
) -> dict[str, Any]:
    """
    Chạy toàn bộ kiểm tra và trả về báo cáo.

    Hàm chỉ đọc dữ liệu, không thực hiện đồng bộ.
    """
    if sqlite_path is None:
        configured_path = os.getenv(
            "FOOTPRINT_SQLITE_PATH",
            str(DEFAULT_SQLITE_PATH),
        )

        candidate = Path(configured_path)

        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate

        sqlite_path = candidate

    max_sync_age = env_int(
        "FOOTPRINT_HEALTH_MAX_SYNC_AGE_SECONDS",
        DEFAULT_MAX_SYNC_AGE_SECONDS,
    )

    max_running_age = env_int(
        "FOOTPRINT_HEALTH_MAX_RUNNING_SECONDS",
        DEFAULT_MAX_RUNNING_SECONDS,
    )

    max_progress_age = env_int(
        "FOOTPRINT_HEALTH_MAX_PROGRESS_AGE_SECONDS",
        DEFAULT_MAX_PROGRESS_AGE_SECONDS,
    )

    report: dict[str, Any] = {
        "checked_at": utc_iso(),
        "project_root": str(PROJECT_ROOT),
        "overall_status": "UNKNOWN",
        "sqlite": {},
        "supabase": {},
        "sync_state": {},
        "sync_analysis": {},
        "warnings": [],
        "errors": [],
    }

    # 1. Kiểm tra SQLite
    sqlite_info = inspect_sqlite(sqlite_path)
    report["sqlite"] = sqlite_info

    if not sqlite_info["ok"]:
        report["errors"].append(
            "SQLite: " + str(sqlite_info.get("error"))
        )

    # 2. Khởi tạo và kiểm tra Supabase
    try:
        store = create_supabase_store()

        supabase_info = inspect_supabase_connection(store)
        report["supabase"] = supabase_info

        if not supabase_info["ok"]:
            report["errors"].append(
                "Supabase: "
                + str(supabase_info.get("message"))
            )

        # 3. Đọc sync state nếu cấu hình và kết nối được.
        if supabase_info["ok"]:
            state_info = inspect_sync_state(
                store,
                sync_name=sync_name,
            )
        else:
            state_info = {
                "ok": False,
                "exists": False,
                "sync_name": sync_name,
                "status": "UNAVAILABLE",
                "last_local_trade_id": 0,
                "error": "Không thể đọc sync state vì Supabase chưa khỏe.",
            }

        report["sync_state"] = state_info

        # 4. Phân tích checkpoint, trạng thái và độ trễ.
        analysis = analyze_sync(
            sqlite_info=sqlite_info,
            state_info=state_info,
            max_sync_age_seconds=max_sync_age,
            max_running_seconds=max_running_age,
            max_progress_age_seconds=max_progress_age,
        )

        report["sync_analysis"] = analysis

        report["warnings"].extend(
            analysis.get("warnings", [])
        )

        report["errors"].extend(
            analysis.get("errors", [])
        )

    except Exception as exc:
        logger.exception("Không thể hoàn thành health check.")

        report["errors"].append(
            "Lỗi khởi tạo/kiểm tra SupabaseStore: "
            + str(exc)
        )

    # 5. Kết luận tổng thể
    if report["errors"]:
        report["overall_status"] = "UNHEALTHY"
    elif report["warnings"]:
        report["overall_status"] = "DEGRADED"
    else:
        report["overall_status"] = "HEALTHY"

    return report


# ============================================================
# 9. HIỂN THỊ BÁO CÁO
# ============================================================

def print_report(report: dict[str, Any], as_json: bool = False) -> None:
    """In báo cáo dạng JSON hoặc dạng dễ đọc."""
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
    print("=" * 64)
    print("          FOOTPRINT SYSTEM HEALTH MONITOR")
    print("=" * 64)

    print(f"Thời điểm kiểm tra : {report.get('checked_at')}")
    print(f"Trạng thái tổng thể: {report.get('overall_status')}")
    print(f"Thư mục dự án      : {report.get('project_root')}")

    sqlite_info = report.get("sqlite", {})

    print()
    print("-" * 64)
    print("1. SQLITE")
    print("-" * 64)

    print(f"Đường dẫn       : {sqlite_info.get('path')}")
    print(f"Tồn tại         : {sqlite_info.get('exists')}")
    print(f"Đọc được        : {sqlite_info.get('readable')}")
    print(f"Bảng tồn tại    : {sqlite_info.get('table_exists')}")
    print(f"Tổng giao dịch  : {sqlite_info.get('total_trades')}")
    print(f"ID nhỏ nhất     : {sqlite_info.get('min_id')}")
    print(f"ID lớn nhất     : {sqlite_info.get('max_id')}")
    print(f"Integrity check : {sqlite_info.get('integrity_check')}")

    if sqlite_info.get("error"):
        print(f"Lỗi SQLite      : {sqlite_info.get('error')}")

    supabase_info = report.get("supabase", {})

    print()
    print("-" * 64)
    print("2. SUPABASE")
    print("-" * 64)

    print(f"Enabled         : {supabase_info.get('enabled')}")
    print(f"Configured      : {supabase_info.get('configured')}")
    print(f"Kết nối OK      : {supabase_info.get('ok')}")
    print(f"Nguồn dữ liệu   : {supabase_info.get('source')}")
    print(f"Batch size      : {supabase_info.get('batch_size')}")
    print(f"JWT role claim  : {supabase_info.get('jwt_role_claim')}")
    print(f"Thông báo       : {supabase_info.get('message')}")

    state_info = report.get("sync_state", {})
    analysis = report.get("sync_analysis", {})

    print()
    print("-" * 64)
    print("3. SQLITE -> SUPABASE SYNC")
    print("-" * 64)

    print(f"Sync name       : {state_info.get('sync_name')}")
    print(f"Có sync state   : {state_info.get('exists')}")
    print(f"Trạng thái      : {state_info.get('status')}")
    print(
        "Checkpoint ID   : "
        f"{state_info.get('last_local_trade_id')}"
    )
    print(
        "SQLite max ID   : "
        f"{analysis.get('sqlite_max_id')}"
    )
    print(
        "Checkpoint đạt max ID: "
        f"{analysis.get('checkpoint_reaches_current_max_id')}"
    )
    print(f"Lần thử gần nhất: {state_info.get('last_attempt_at')}")
    print(f"Lần thành công  : {state_info.get('last_success_at')}")
    print(f"Cập nhật state  : {state_info.get('updated_at')}")

    if state_info.get("error_message"):
        print(
            "Sync error      : "
            f"{state_info.get('error_message')}"
        )

    warnings = report.get("warnings", [])
    errors = report.get("errors", [])

    print()
    print("-" * 64)
    print("4. CẢNH BÁO")
    print("-" * 64)

    if warnings:
        for index, message in enumerate(warnings, start=1):
            print(f"[WARNING {index}] {message}")
    else:
        print("Không có cảnh báo.")

    if errors:
        for index, message in enumerate(errors, start=1):
            print(f"[ERROR {index}] {message}")
    else:
        print("Không có lỗi nghiêm trọng.")

    print()
    print(f"Log file: {LOG_FILE}")
    print("=" * 64)
    print()


# ============================================================
# 10. CLI ENTRY POINT
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Kiểm tra sức khỏe hệ thống Footprint."
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="Xuất báo cáo JSON.",
    )

    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Trả exit code 1 nếu hệ thống DEGRADED hoặc UNHEALTHY."
        ),
    )

    parser.add_argument(
        "--sqlite-path",
        type=str,
        default=None,
        help="Đường dẫn database SQLite cần kiểm tra.",
    )

    parser.add_argument(
        "--sync-name",
        type=str,
        default=DEFAULT_SYNC_NAME,
        help="Tên sync_name trong footprint_sync_state.",
    )

    args = parser.parse_args()

    sqlite_path = None

    if args.sqlite_path:
        sqlite_path = Path(args.sqlite_path)

        if not sqlite_path.is_absolute():
            sqlite_path = PROJECT_ROOT / sqlite_path

    start_time = time.monotonic()

    report = run_health_check(
        sqlite_path=sqlite_path,
        sync_name=args.sync_name,
    )

    report["duration_seconds"] = round(
        time.monotonic() - start_time,
        3,
    )

    print_report(
        report,
        as_json=args.json,
    )

    logger.info(
        "Health check hoàn tất: status=%s, duration=%.3fs",
        report["overall_status"],
        report["duration_seconds"],
    )

    if report["overall_status"] == "UNHEALTHY":
        return 1

    if args.strict and report["overall_status"] != "HEALTHY":
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())