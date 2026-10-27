
"""
Footprint Sync Worker
=====================

Đồng bộ giao dịch:
    SQLite: data/footprint.db
        -> Supabase: footprint_trades
        -> Supabase: footprint_sync_state

Sử dụng SupabaseStore.sync_sqlite_trades() để tái sử dụng
logic đồng bộ, chuẩn hóa dữ liệu và checkpoint hiện có.

Chạy từ thư mục gốc dự án:
    python -m scripts.footprint.sync_worker

Chạy một lần:
    python -m scripts.footprint.sync_worker --once

Chạy thử không ghi dữ liệu:
    python -m scripts.footprint.sync_worker --dry-run --once

Giới hạn mỗi chu kỳ tối đa 2 batch:
    python -m scripts.footprint.sync_worker --max-batches-per-cycle 2

Dừng worker:
    Ctrl + C

Lưu ý:
    - Không đặt Supabase secret key trong frontend.
    - Không tắt RLS để xử lý lỗi phân quyền.
    - File này không tự tạo hoặc sửa schema Supabase.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional


# ============================================================
# 1. ĐƯỜNG DẪN DỰ ÁN
# ============================================================

# File:
# E:\LHP-FI\lhp-ai-server\scripts\footprint\sync_worker.py
#
# parents[0] = scripts/footprint
# parents[1] = scripts
# parents[2] = thư mục gốc lhp-ai-server

PROJECT_ROOT = Path(__file__).resolve().parents[2]

SQLITE_PATH = PROJECT_ROOT / "data" / "footprint.db"

LOG_DIR = PROJECT_ROOT / "logs"

LOG_FILE = LOG_DIR / "footprint_sync_worker.log"

ENV_FILE = PROJECT_ROOT / ".env"


# ============================================================
# 2. NẠP BIẾN MÔI TRƯỜNG
# ============================================================

try:
    from dotenv import load_dotenv

    if ENV_FILE.is_file():
        # Ưu tiên cấu hình trong .env của dự án.
        # Không in giá trị các khóa bí mật ra log.
        load_dotenv(ENV_FILE, override=True)

except ImportError:
    print(
        "CẢNH BÁO: Chưa cài python-dotenv. "
        "Hãy chạy: pip install python-dotenv"
    )


# Import sau khi nạp .env.
from scripts.footprint.supabase_store import SupabaseStore


# ============================================================
# 3. LOGGING
# ============================================================

def setup_logging() -> logging.Logger:
    """
    Ghi log ra console và file.

    File log giới hạn kích thước để tránh tăng vô hạn.
    """

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("footprint.sync_worker")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    # Tránh thêm handler nhiều lần nếu hàm được gọi lại.
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        filename=str(LOG_FILE),
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    return logger


logger = setup_logging()


# ============================================================
# 4. DỪNG WORKER AN TOÀN
# ============================================================

_STOP_REQUESTED = False


def request_stop(signum: int, frame: Any) -> None:
    """
    Nhận tín hiệu dừng và để vòng lặp kết thúc an toàn.
    """

    global _STOP_REQUESTED

    _STOP_REQUESTED = True

    logger.info(
        "Nhận tín hiệu dừng %s. Worker sẽ dừng sau chu kỳ hiện tại.",
        signum,
    )


def register_signal_handlers() -> None:
    """
    Đăng ký tín hiệu dừng phù hợp với Windows và Linux.
    """

    signal.signal(signal.SIGINT, request_stop)

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)


# ============================================================
# 5. KIỂM TRA CẤU HÌNH
# ============================================================

def validate_configuration(
    store: SupabaseStore,
    dry_run: bool = False,
) -> bool:
    """
    Kiểm tra cấu hình trước khi bắt đầu.

    dry_run=True:
        Không yêu cầu kết nối Supabase vì chế độ này không ghi dữ liệu.

    dry_run=False:
        Yêu cầu cấu hình và health_check thành công.
    """

    if not SQLITE_PATH.is_file():
        logger.error(
            "Không tìm thấy SQLite database: %s",
            SQLITE_PATH,
        )
        return False

    status = store.get_status()

    logger.info(
        "SQLite database: %s",
        SQLITE_PATH,
    )

    logger.info(
        "Supabase enabled=%s, configured=%s, source=%s, batch_size=%s",
        status.get("enabled"),
        status.get("configured"),
        status.get("source"),
        status.get("batch_size"),
    )

    if dry_run:
        logger.warning(
            "ĐANG CHẠY DRY-RUN: không ghi dữ liệu lên Supabase."
        )
        return True

    if not status.get("enabled"):
        logger.error(
            "Đồng bộ đang tắt. "
            "Kiểm tra SUPABASE_SYNC_ENABLED=true trong .env."
        )
        return False

    if not status.get("configured"):
        logger.error(
            "Supabase chưa được cấu hình đầy đủ. "
            "Kiểm tra SUPABASE_URL và khóa backend."
        )
        return False

    health = store.health_check()

    if not health.get("ok"):
        logger.error(
            "Supabase health_check thất bại: %s",
            health.get("message", "Không có thông tin lỗi."),
        )
        logger.error(
            "Worker chưa bắt đầu đồng bộ. "
            "Kiểm tra khóa backend và quyền truy cập."
        )
        return False

    logger.info(
        "Supabase health_check thành công: %s",
        health.get("message"),
    )

    return True


# ============================================================
# 6. CHẠY MỘT CHU KỲ ĐỒNG BỘ
# ============================================================

def sync_once(
    store: SupabaseStore,
    *,
    max_batches: Optional[int] = 5,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Chạy một lần đồng bộ SQLite -> Supabase.

    max_batches:
        5    = tối đa 5 batch mỗi chu kỳ.
        1    = tối đa 1 batch mỗi chu kỳ.
        None = xử lý đến khi hết dữ liệu cần đồng bộ.

    Việc xác định giao dịch mới và cập nhật checkpoint
    do SupabaseStore.sync_sqlite_trades() đảm nhiệm.
    """

    started_at = time.monotonic()

    logger.info(
        "Bắt đầu đồng bộ. max_batches=%s, dry_run=%s",
        max_batches,
        dry_run,
    )

    result = store.sync_sqlite_trades(
        sqlite_path=str(SQLITE_PATH),
        max_batches=max_batches,
        dry_run=dry_run,
    )

    elapsed = time.monotonic() - started_at

    logger.info(
        (
            "Kết quả đồng bộ: ok=%s, read=%s, upserted=%s, "
            "failed=%s, last_local_id=%s, elapsed=%.2fs"
        ),
        result.get("ok"),
        result.get("read", 0),
        result.get("upserted", 0),
        result.get("failed", 0),
        result.get("last_local_id"),
        elapsed,
    )

    errors = result.get("errors") or []

    for item in errors[:10]:
        # Chỉ ghi thông tin lỗi, tuyệt đối không ghi khóa bí mật.
        logger.error("Chi tiết lỗi đồng bộ: %s", item)

    if not result.get("ok"):
        logger.warning(
            "Chu kỳ đồng bộ chưa hoàn tất thành công. "
            "Kiểm tra lỗi trước khi tiếp tục."
        )

    elif result.get("read", 0) == 0:
        logger.info(
            "Không có giao dịch mới cần đồng bộ ở checkpoint hiện tại."
        )

    else:
        logger.info(
            "Chu kỳ đồng bộ kết thúc."
        )

    return result


# ============================================================
# 7. VÒNG LẶP WORKER
# ============================================================

def run_worker(
    *,
    interval_seconds: int = 10,
    max_batches: Optional[int] = 5,
    once: bool = False,
    dry_run: bool = False,
) -> int:
    """
    Điều phối các chu kỳ đồng bộ.

    once=False:
        Chạy liên tục đến khi nhận tín hiệu dừng.

    once=True:
        Chạy một chu kỳ rồi thoát.
    """

    if interval_seconds < 1:
        logger.error("interval_seconds phải lớn hơn hoặc bằng 1.")
        return 2

    store = SupabaseStore()

    if not validate_configuration(store, dry_run=dry_run):
        return 2

    logger.info("Footprint Sync Worker khởi động.")
    logger.info("Project root: %s", PROJECT_ROOT)
    logger.info("SQLite path: %s", SQLITE_PATH)
    logger.info("Log file: %s", LOG_FILE)
    logger.info("Interval: %s giây", interval_seconds)
    logger.info("Max batches mỗi chu kỳ: %s", max_batches)
    logger.info("Once mode: %s", once)
    logger.info("Dry-run: %s", dry_run)

    cycle = 0
    had_error = False

    try:
        while not _STOP_REQUESTED:
            cycle += 1

            logger.info(
                "========== SYNC CYCLE %s ==========",
                cycle,
            )

            try:
                result = sync_once(
                    store,
                    max_batches=max_batches,
                    dry_run=dry_run,
                )

                if not result.get("ok"):
                    had_error = True

            except KeyboardInterrupt:
                logger.info("Người dùng yêu cầu dừng worker.")
                break

            except Exception:
                had_error = True

                logger.exception(
                    "Lỗi không mong đợi trong chu kỳ đồng bộ %s.",
                    cycle,
                )

            if once:
                break

            if _STOP_REQUESTED:
                break

            logger.info(
                "Chờ %s giây trước chu kỳ tiếp theo.",
                interval_seconds,
            )

            # Ngủ theo từng giây để có thể dừng sớm.
            for _ in range(interval_seconds):
                if _STOP_REQUESTED:
                    break
                time.sleep(1)

    finally:
        logger.info("Footprint Sync Worker đã dừng.")

    return 1 if had_error else 0


# ============================================================
# 8. CLI
# ============================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Tự động đồng bộ SQLite Footprint lên Supabase."
    )

    parser.add_argument(
        "--interval",
        type=int,
        default=10,
        help="Số giây chờ giữa các chu kỳ. Mặc định: 10.",
    )

    parser.add_argument(
        "--max-batches-per-cycle",
        type=int,
        default=5,
        help=(
            "Số batch tối đa mỗi chu kỳ. "
            "Mặc định: 5. Dùng 0 để xử lý đến hết dữ liệu."
        ),
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help="Chạy đúng một chu kỳ rồi thoát.",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Chạy thử, không ghi dữ liệu lên Supabase.",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.interval < 1:
        print("--interval phải lớn hơn hoặc bằng 1.")
        return 2

    if args.max_batches_per_cycle < 0:
        print("--max-batches-per-cycle không được âm.")
        return 2

    max_batches: Optional[int]

    if args.max_batches_per_cycle == 0:
        max_batches = None
    else:
        max_batches = args.max_batches_per_cycle

    register_signal_handlers()

    return run_worker(
        interval_seconds=args.interval,
        max_batches=max_batches,
        once=args.once,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())

