"""
Footprint Sync State Manager
============================

Quản lý trạng thái đồng bộ SQLite -> Supabase.

Tích hợp với:
    scripts/footprint/supabase_store.py
    public.footprint_sync_state
    data/footprint.db

Chức năng:
    - Đọc checkpoint hiện tại.
    - Đánh dấu trạng thái RUNNING.
    - Đánh dấu trạng thái SUCCESS.
    - Đánh dấu trạng thái FAILED.
    - Ghi nhận lỗi đồng bộ.
    - Kiểm tra trạng thái Supabase.
    - Hiển thị trạng thái qua CLI.

Không:
    - Tự tạo bảng Supabase.
    - Tự thay đổi RLS.
    - Tự xóa checkpoint.
    - Tự đồng bộ giao dịch.
    - Ghi hoặc hiển thị khóa bí mật.

Chạy từ thư mục gốc dự án:

    python -m scripts.footprint.sync_state

    python -m scripts.footprint.sync_state --sync-name sqlite_to_supabase

"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ============================================================
# 1. PROJECT PATH
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]

ENV_FILE = PROJECT_ROOT / ".env"

SQLITE_PATH = PROJECT_ROOT / "data" / "footprint.db"


# ============================================================
# 2. ENVIRONMENT
# ============================================================

try:
    from dotenv import load_dotenv

    if ENV_FILE.is_file():
        load_dotenv(ENV_FILE, override=True)

except ImportError:
    pass


# Import sau khi nạp .env.
from scripts.footprint.supabase_store import (
    SupabaseStore,
    get_supabase_store,
)


# ============================================================
# 3. CONSTANTS
# ============================================================

DEFAULT_SYNC_NAME = "sqlite_to_supabase"

STATUS_RUNNING = "RUNNING"
STATUS_SUCCESS = "SUCCESS"
STATUS_FAILED = "FAILED"
STATUS_RETRYING = "RETRYING"


# ============================================================
# 4. LOGGING
# ============================================================

logger = logging.getLogger("footprint.sync_state")

if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)

    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


# ============================================================
# 5. HELPERS
# ============================================================

def utc_now_iso() -> str:
    """Trả về thời gian UTC theo định dạng ISO 8601."""

    return datetime.now(timezone.utc).isoformat()


def normalize_sync_name(value: Optional[str]) -> str:
    """Chuẩn hóa tên tiến trình đồng bộ."""

    name = str(value or DEFAULT_SYNC_NAME).strip()

    if not name:
        return DEFAULT_SYNC_NAME

    return name


def normalize_checkpoint(value: Any) -> int:
    """
    Chuyển checkpoint thành số nguyên không âm.

    Nếu trạng thái chưa có checkpoint, trả về 0.
    """

    try:
        checkpoint = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0

    return max(0, checkpoint)


# ============================================================
# 6. SYNC STATE MANAGER
# ============================================================

class SyncStateManager:
    """
    Quản lý trạng thái đồng bộ bằng SupabaseStore.

    Không tạo client Supabase riêng.
    Không tạo bảng mới.
    Không thay đổi cấu trúc dữ liệu.

    SupabaseStore vẫn là nơi thực hiện truy vấn database.
    """

    def __init__(
        self,
        store: Optional[SupabaseStore] = None,
        sync_name: str = DEFAULT_SYNC_NAME,
    ) -> None:

        self.store = store or get_supabase_store()

        self.sync_name = normalize_sync_name(sync_name)

    # --------------------------------------------------------
    # GET STATE
    # --------------------------------------------------------

    def get_state(self) -> Optional[dict[str, Any]]:
        """
        Đọc trạng thái đồng bộ từ footprint_sync_state.

        Trả về:
            dict nếu đã tồn tại.
            None nếu chưa có bản ghi.
        """

        return self.store.get_sync_state(
            sync_name=self.sync_name,
        )

    # --------------------------------------------------------
    # GET CHECKPOINT
    # --------------------------------------------------------

    def get_trade_checkpoint(self) -> int:
        """
        Đọc ID SQLite cuối cùng đã đồng bộ thành công.

        Giá trị này được dùng để tiếp tục đồng bộ từ giao dịch mới.

        Không tự sửa checkpoint nếu dữ liệu chưa được xác minh.
        """

        state = self.get_state()

        if not state:
            return 0

        return normalize_checkpoint(
            state.get("last_local_trade_id")
        )

    # --------------------------------------------------------
    # MARK RUNNING
    # --------------------------------------------------------

    def mark_running(
        self,
        *,
        checkpoint: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        Đánh dấu tiến trình đang chạy.

        Nếu truyền checkpoint, cập nhật checkpoint hiện có
        cùng với trạng thái.

        Không tự tăng checkpoint.
        """

        fields: dict[str, Any] = {
            "status": STATUS_RUNNING,
            "last_attempt_at": utc_now_iso(),
            "error_message": None,
        }

        if checkpoint is not None:
            fields["last_local_trade_id"] = normalize_checkpoint(
                checkpoint
            )

        result = self.store.update_sync_state(
            self.sync_name,
            **fields,
        )

        logger.info(
            "Sync state RUNNING: sync_name=%s, checkpoint=%s",
            self.sync_name,
            checkpoint if checkpoint is not None else "unchanged",
        )

        return result

    # --------------------------------------------------------
    # UPDATE CHECKPOINT
    # --------------------------------------------------------

    def save_trade_checkpoint(
        self,
        local_trade_id: int,
    ) -> dict[str, Any]:
        """
        Lưu checkpoint giao dịch.

        Chỉ gọi sau khi giao dịch tương ứng đã được ghi
        thành công lên Supabase.

        Hàm này không tự xác minh giao dịch đã tồn tại.
        Trách nhiệm xác minh thuộc về tầng đồng bộ.
        """

        checkpoint = normalize_checkpoint(local_trade_id)

        current_checkpoint = self.get_trade_checkpoint()

        if checkpoint < current_checkpoint:
            raise ValueError(
                "Checkpoint mới nhỏ hơn checkpoint hiện tại: "
                f"{checkpoint} < {current_checkpoint}. "
                "Từ chối ghi lùi checkpoint."
            )

        result = self.store.update_sync_state(
            self.sync_name,
            last_local_trade_id=checkpoint,
            last_attempt_at=utc_now_iso(),
            status=STATUS_RUNNING,
            error_message=None,
        )

        logger.info(
            "Đã cập nhật checkpoint: sync_name=%s, local_trade_id=%s",
            self.sync_name,
            checkpoint,
        )

        return result

    # --------------------------------------------------------
    # MARK SUCCESS
    # --------------------------------------------------------

    def mark_success(
        self,
        *,
        checkpoint: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        Đánh dấu đồng bộ thành công.

        Nếu checkpoint không được truyền, giữ nguyên
        checkpoint hiện có trong database.
        """

        fields: dict[str, Any] = {
            "status": STATUS_SUCCESS,
            "last_attempt_at": utc_now_iso(),
            "last_success_at": utc_now_iso(),
            "error_message": None,
        }

        if checkpoint is not None:
            checkpoint = normalize_checkpoint(checkpoint)

            current_checkpoint = self.get_trade_checkpoint()

            if checkpoint < current_checkpoint:
                raise ValueError(
                    "Không thể đánh dấu SUCCESS với checkpoint "
                    "nhỏ hơn checkpoint hiện tại."
                )

            fields["last_local_trade_id"] = checkpoint

        result = self.store.update_sync_state(
            self.sync_name,
            **fields,
        )

        logger.info(
            "Đồng bộ thành công: sync_name=%s, checkpoint=%s",
            self.sync_name,
            checkpoint if checkpoint is not None else "unchanged",
        )

        return result

    # --------------------------------------------------------
    # MARK FAILED
    # --------------------------------------------------------

    def mark_failed(
        self,
        error: str,
    ) -> dict[str, Any]:
        """
        Đánh dấu đồng bộ thất bại.

        Không thay đổi checkpoint đã lưu trước đó.
        """

        message = str(error or "Lỗi không xác định")[:4000]

        result = self.store.update_sync_state(
            self.sync_name,
            status=STATUS_FAILED,
            last_attempt_at=utc_now_iso(),
            error_message=message,
        )

        logger.error(
            "Đồng bộ thất bại: sync_name=%s, error=%s",
            self.sync_name,
            message,
        )

        return result

    # --------------------------------------------------------
    # MARK RETRYING
    # --------------------------------------------------------

    def mark_retrying(
        self,
        error: str,
    ) -> dict[str, Any]:
        """
        Đánh dấu đang chờ thử lại.

        Giữ nguyên checkpoint hiện có.
        """

        message = str(error or "Đang chờ thử lại")[:4000]

        result = self.store.update_sync_state(
            self.sync_name,
            status=STATUS_RETRYING,
            last_attempt_at=utc_now_iso(),
            error_message=message,
        )

        logger.warning(
            "Đồng bộ đang chờ thử lại: sync_name=%s",
            self.sync_name,
        )

        return result

    # --------------------------------------------------------
    # HEALTH CHECK
    # --------------------------------------------------------

    def health_check(self) -> dict[str, Any]:
        """
        Kiểm tra kết nối Supabase.

        Không thực hiện ghi dữ liệu.
        """

        return self.store.health_check()

    # --------------------------------------------------------
    # STATUS
    # --------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        """
        Tổng hợp thông tin trạng thái hiện tại.

        Không trả về khóa Supabase.
        """

        state = self.get_state()

        if state is None:
            state = {}

        return {
            "sync_name": self.sync_name,
            "sqlite_path": str(SQLITE_PATH),
            "sqlite_exists": SQLITE_PATH.is_file(),
            "supabase_enabled": self.store.enabled,
            "supabase_configured": self.store.configured,
            "status": state.get("status"),
            "last_local_trade_id": normalize_checkpoint(
                state.get("last_local_trade_id")
            ),
            "last_local_candle_id": normalize_checkpoint(
                state.get("last_local_candle_id")
            ),
            "last_attempt_at": state.get("last_attempt_at"),
            "last_success_at": state.get("last_success_at"),
            "updated_at": state.get("updated_at"),
            "error_message": state.get("error_message"),
            "state_exists": bool(state),
        }


# ============================================================
# 7. PUBLIC HELPERS
# ============================================================

def get_sync_state(
    sync_name: str = DEFAULT_SYNC_NAME,
) -> Optional[dict[str, Any]]:
    """Đọc trạng thái đồng bộ."""

    manager = SyncStateManager(sync_name=sync_name)

    return manager.get_state()


def get_trade_checkpoint(
    sync_name: str = DEFAULT_SYNC_NAME,
) -> int:
    """Lấy checkpoint giao dịch."""

    manager = SyncStateManager(sync_name=sync_name)

    return manager.get_trade_checkpoint()


def mark_sync_running(
    sync_name: str = DEFAULT_SYNC_NAME,
    *,
    checkpoint: Optional[int] = None,
) -> dict[str, Any]:
    """Đánh dấu tiến trình đồng bộ đang chạy."""

    manager = SyncStateManager(sync_name=sync_name)

    return manager.mark_running(
        checkpoint=checkpoint,
    )


def mark_sync_success(
    sync_name: str = DEFAULT_SYNC_NAME,
    *,
    checkpoint: Optional[int] = None,
) -> dict[str, Any]:
    """Đánh dấu đồng bộ thành công."""

    manager = SyncStateManager(sync_name=sync_name)

    return manager.mark_success(
        checkpoint=checkpoint,
    )


def mark_sync_failed(
    error: str,
    sync_name: str = DEFAULT_SYNC_NAME,
) -> dict[str, Any]:
    """Đánh dấu đồng bộ thất bại."""

    manager = SyncStateManager(sync_name=sync_name)

    return manager.mark_failed(error)


# ============================================================
# 8. CLI
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Kiểm tra trạng thái đồng bộ Footprint."
    )

    parser.add_argument(
        "--sync-name",
        default=DEFAULT_SYNC_NAME,
        help="Tên tiến trình đồng bộ.",
    )

    args = parser.parse_args()

    try:
        manager = SyncStateManager(
            sync_name=args.sync_name,
        )

        status = manager.get_status()

        print("\n========== FOOTPRINT SYNC STATE ==========")

        for key, value in status.items():
            print(f"{key}: {value}")

        print("\n========== SUPABASE HEALTH ==========")

        health = manager.health_check()

        for key, value in health.items():
            print(f"{key}: {value}")

        if not health.get("ok"):
            return 1

        return 0

    except Exception as exc:
        logger.exception(
            "Không thể đọc trạng thái đồng bộ: %s",
            exc,
        )

        return 1


if __name__ == "__main__":
    raise SystemExit(main())

