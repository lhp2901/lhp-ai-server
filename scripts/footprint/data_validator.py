
"""
Footprint Data Validator
========================

Module kiểm tra chất lượng dữ liệu cho hệ thống LHP-FI Footprint.

Hỗ trợ:
- Kiểm tra từng giao dịch DNSE.
- Kiểm tra dữ liệu chuẩn hóa trước khi ghi Supabase.
- Kiểm tra SQLite footprint_trades.
- Phát hiện giao dịch trùng theo trade_id hoặc local ID.
- Kiểm tra symbol, price, volume, side và timestamp.
- Kiểm tra dữ liệu dùng cho Order Flow và Volume Profile.
- Tạo báo cáo JSON.
- Ghi log các lỗi chất lượng dữ liệu.

Nguyên tắc:
- Không sửa dữ liệu gốc.
- Không tự xóa giao dịch.
- Không tự thay đổi checkpoint.
- Không tự suy đoán BUY/SELL nếu thiếu thông tin.
- Không ghi thông tin xác thực vào log.

Chạy từ thư mục gốc dự án:

    python -m scripts.footprint.data_validator

Kiểm tra file SQLite:

    python -m scripts.footprint.data_validator \
        --sqlite-path data/footprint.db

Xuất JSON:

    python -m scripts.footprint.data_validator --json

Lưu ý:
- Các ngưỡng kiểm tra là cấu hình vận hành, không phải quy tắc
  giao dịch chính thức của DNSE.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sqlite3
import sys

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from dotenv import load_dotenv


# ============================================================
# 1. PATHS AND CONFIGURATION
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]

ENV_PATH = PROJECT_ROOT / ".env"

DEFAULT_SQLITE_PATH = PROJECT_ROOT / "data" / "footprint.db"

LOG_DIR = PROJECT_ROOT / "logs"

LOG_FILE = LOG_DIR / "footprint_data_validator.log"

load_dotenv(ENV_PATH, override=True)


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


# Danh sách symbol tùy chọn.
# Để trống thì không giới hạn theo danh sách mã.
_DEFAULT_SYMBOLS = os.getenv(
    "FOOTPRINT_SYMBOLS",
    "DXG,HPG,FPT,SSI,VND",
)

ALLOWED_SYMBOLS = {
    item.strip().upper()
    for item in _DEFAULT_SYMBOLS.split(",")
    if item.strip()
}

# Giới hạn thời gian giao dịch được phép lệch khỏi thời điểm hiện tại.
# Không dùng giới hạn này để xóa dữ liệu lịch sử.
MAX_FUTURE_TIMESTAMP_SECONDS = env_int(
    "FOOTPRINT_VALIDATOR_MAX_FUTURE_SECONDS",
    300,
)

# Mặc định không bắt buộc trade_id từ nguồn.
# SQLite local ID vẫn được dùng để định danh bản ghi lưu trữ.
REQUIRE_PROVIDER_TRADE_ID = env_bool(
    "FOOTPRINT_VALIDATOR_REQUIRE_TRADE_ID",
    False,
)

# Theo schema Supabase hiện tại, volume là BIGINT.
REQUIRE_INTEGER_VOLUME = env_bool(
    "FOOTPRINT_VALIDATOR_REQUIRE_INTEGER_VOLUME",
    True,
)

# Chỉ cảnh báo nếu giá trị khối lượng bằng 0.
ZERO_VOLUME_IS_ERROR = env_bool(
    "FOOTPRINT_VALIDATOR_ZERO_VOLUME_IS_ERROR",
    False,
)

# Kiểm tra độ dài symbol theo cấu hình vận hành.
MAX_SYMBOL_LENGTH = env_int(
    "FOOTPRINT_VALIDATOR_MAX_SYMBOL_LENGTH",
    20,
)

# Bật kiểm tra symbol thuộc ALLOWED_SYMBOLS.
# Mặc định tắt để tránh từ chối mã mới chưa thêm vào cấu hình.
STRICT_SYMBOL_LIST = env_bool(
    "FOOTPRINT_VALIDATOR_STRICT_SYMBOL_LIST",
    False,
)


# ============================================================
# 2. LOGGING
# ============================================================

LOG_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("footprint.data_validator")
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
# 3. RESULT HELPERS
# ============================================================

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso() -> str:
    return utc_now().isoformat()


def _issue(
    code: str,
    message: str,
    severity: str = "ERROR",
    field: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity.upper(),
        "field": field,
        "message": message,
    }


def _result(
    valid: bool,
    errors: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    normalized: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return {
        "valid": valid,
        "errors": errors,
        "warnings": warnings,
        "normalized": normalized,
    }


# ============================================================
# 4. SAFE VALUE CONVERSION
# ============================================================

def _first(
    record: Mapping[str, Any],
    *keys: str,
    default: Any = None,
) -> Any:
    for key in keys:
        if key in record and record[key] is not None:
            return record[key]

    return default


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None

    try:
        number = Decimal(str(value))

        if not number.is_finite():
            return None

        return number

    except (InvalidOperation, TypeError, ValueError):
        return None


def _to_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None

    try:
        number = Decimal(str(value))

        if not number.is_finite():
            return None

        if number != number.to_integral_value():
            return None

        return int(number)

    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return None


def _normalize_timestamp_ms(value: Any) -> Optional[int]:
    """
    Chuẩn hóa timestamp thành epoch milliseconds.

    Hỗ trợ:
    - Epoch seconds.
    - Epoch milliseconds.
    - datetime.
    - ISO datetime.

    Không tự suy đoán đơn vị nếu dữ liệu không hợp lệ.
    """

    if value is None or value == "":
        return None

    if isinstance(value, datetime):
        parsed = value

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return int(parsed.timestamp() * 1000)

    try:
        number = float(value)

        if not math.isfinite(number) or number <= 0:
            return None

        if number < 100_000_000_000:
            number *= 1000

        return int(number)

    except (TypeError, ValueError, OverflowError):
        pass

    try:
        text = str(value).strip()

        if text.endswith("Z"):
            text = text[:-1] + "+00:00"

        parsed = datetime.fromisoformat(text)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return int(parsed.timestamp() * 1000)

    except (TypeError, ValueError, OverflowError):
        return None


def _normalize_side(value: Any) -> str:
    text = str(value or "").strip().upper()

    aliases = {
        "B": "BUY",
        "BID": "BUY",
        "BUYER": "BUY",
        "UP": "BUY",
        "S": "SELL",
        "ASK": "SELL",
        "SELLER": "SELL",
        "DOWN": "SELL",
        "": "UNKNOWN",
        "NA": "UNKNOWN",
        "N/A": "UNKNOWN",
        "NONE": "UNKNOWN",
        "NULL": "UNKNOWN",
    }

    text = aliases.get(text, text)

    if text not in {"BUY", "SELL", "UNKNOWN"}:
        return "INVALID"

    return text


def _normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


# ============================================================
# 5. VALIDATOR CLASS
# ============================================================

class FootprintDataValidator:
    """
    Bộ kiểm tra dữ liệu Footprint.

    Có thể sử dụng cho:
    - Dữ liệu WebSocket DNSE.
    - Dữ liệu lấy từ SQLite.
    - Dữ liệu trước khi gọi SupabaseStore.save_trade().
    """

    def __init__(
        self,
        allowed_symbols: Optional[Iterable[str]] = None,
        strict_symbol_list: Optional[bool] = None,
        require_trade_id: Optional[bool] = None,
        require_integer_volume: Optional[bool] = None,
        zero_volume_is_error: Optional[bool] = None,
        max_future_seconds: Optional[int] = None,
    ) -> None:
        self.allowed_symbols = {
            str(symbol).strip().upper()
            for symbol in (
                allowed_symbols
                if allowed_symbols is not None
                else ALLOWED_SYMBOLS
            )
            if str(symbol).strip()
        }

        self.strict_symbol_list = (
            STRICT_SYMBOL_LIST
            if strict_symbol_list is None
            else bool(strict_symbol_list)
        )

        self.require_trade_id = (
            REQUIRE_PROVIDER_TRADE_ID
            if require_trade_id is None
            else bool(require_trade_id)
        )

        self.require_integer_volume = (
            REQUIRE_INTEGER_VOLUME
            if require_integer_volume is None
            else bool(require_integer_volume)
        )

        self.zero_volume_is_error = (
            ZERO_VOLUME_IS_ERROR
            if zero_volume_is_error is None
            else bool(zero_volume_is_error)
        )

        self.max_future_seconds = max(
            0,
            (
                MAX_FUTURE_TIMESTAMP_SECONDS
                if max_future_seconds is None
                else int(max_future_seconds)
            ),
        )

    # --------------------------------------------------------
    # 5.1 Validate a single trade
    # --------------------------------------------------------

    def validate_trade(
        self,
        trade: Mapping[str, Any],
        *,
        require_local_id: bool = False,
    ) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []

        if not isinstance(trade, Mapping):
            errors.append(
                _issue(
                    "INVALID_RECORD",
                    "Bản ghi giao dịch phải là mapping/dictionary.",
                )
            )

            return _result(False, errors, warnings)

        symbol = _normalize_symbol(
            _first(
                trade,
                "symbol",
                "ticker",
                "stock_code",
                "code",
            )
        )

        local_id = _to_int(
            _first(
                trade,
                "source_local_id",
                "local_id",
                "sqlite_id",
                "row_id",
                "id",
            )
        )

        trade_id_value = _first(
            trade,
            "trade_id",
            "provider_trade_id",
            "match_id",
            "transaction_id",
        )

        trade_id = (
            str(trade_id_value).strip()
            if trade_id_value is not None
            and str(trade_id_value).strip()
            else None
        )

        price = _to_decimal(
            _first(
                trade,
                "price",
                "match_price",
                "matched_price",
                "last_price",
            )
        )

        volume = _to_decimal(
            _first(
                trade,
                "volume",
                "quantity",
                "qty",
                "match_volume",
                "matched_volume",
                "vol",
                "kl",
            )
        )

        timestamp_ms = _normalize_timestamp_ms(
            _first(
                trade,
                "timestamp_ms",
                "timestamp",
                "ts_ms",
                "time",
                "trade_time",
                "match_time",
                "datetime",
            )
        )

        side = _normalize_side(
            _first(
                trade,
                "side",
                "aggressor_side",
                "match_side",
                "buy_sell",
                "direction",
            )
        )

        # Symbol
        if not symbol:
            errors.append(
                _issue(
                    "MISSING_SYMBOL",
                    "Giao dịch thiếu symbol.",
                    field="symbol",
                )
            )

        elif len(symbol) > MAX_SYMBOL_LENGTH:
            errors.append(
                _issue(
                    "SYMBOL_TOO_LONG",
                    "Symbol vượt quá độ dài cho phép.",
                    field="symbol",
                )
            )

        elif (
            self.strict_symbol_list
            and self.allowed_symbols
            and symbol not in self.allowed_symbols
        ):
            errors.append(
                _issue(
                    "UNKNOWN_SYMBOL",
                    f"Symbol {symbol!r} không nằm trong danh sách cho phép.",
                    field="symbol",
                )
            )

        elif (
            self.allowed_symbols
            and symbol not in self.allowed_symbols
        ):
            warnings.append(
                _issue(
                    "SYMBOL_NOT_CONFIGURED",
                    f"Symbol {symbol!r} chưa có trong danh sách theo dõi.",
                    severity="WARNING",
                    field="symbol",
                )
            )

        # Local ID
        if require_local_id and local_id is None:
            errors.append(
                _issue(
                    "MISSING_LOCAL_ID",
                    "Bản ghi thiếu ID SQLite hợp lệ.",
                    field="id",
                )
            )

        elif local_id is not None and local_id <= 0:
            errors.append(
                _issue(
                    "INVALID_LOCAL_ID",
                    "ID SQLite phải là số nguyên dương.",
                    field="id",
                )
            )

        # Provider trade ID
        if self.require_trade_id and not trade_id:
            errors.append(
                _issue(
                    "MISSING_TRADE_ID",
                    "Nguồn dữ liệu yêu cầu trade_id nhưng bản ghi không có.",
                    field="trade_id",
                )
            )

        elif not trade_id:
            warnings.append(
                _issue(
                    "MISSING_PROVIDER_TRADE_ID",
                    "Không có trade_id từ nguồn. Cần dựa vào ID SQLite "
                    "để định danh bản ghi lưu trữ.",
                    severity="WARNING",
                    field="trade_id",
                )
            )

        # Price
        if price is None:
            errors.append(
                _issue(
                    "INVALID_PRICE",
                    "Price thiếu hoặc không phải số hữu hạn.",
                    field="price",
                )
            )

        elif price <= 0:
            errors.append(
                _issue(
                    "NON_POSITIVE_PRICE",
                    "Price phải lớn hơn 0.",
                    field="price",
                )
            )

        # Volume
        if volume is None:
            errors.append(
                _issue(
                    "INVALID_VOLUME",
                    "Volume thiếu hoặc không phải số hữu hạn.",
                    field="volume",
                )
            )

        elif volume < 0:
            errors.append(
                _issue(
                    "NEGATIVE_VOLUME",
                    "Volume không được âm.",
                    field="volume",
                )
            )

        elif volume == 0:
            issue = _issue(
                "ZERO_VOLUME",
                "Volume bằng 0; cần kiểm tra quy tắc dữ liệu nguồn.",
                severity=(
                    "ERROR"
                    if self.zero_volume_is_error
                    else "WARNING"
                ),
                field="volume",
            )

            if self.zero_volume_is_error:
                errors.append(issue)
            else:
                warnings.append(issue)

        elif (
            self.require_integer_volume
            and volume != volume.to_integral_value()
        ):
            errors.append(
                _issue(
                    "FRACTIONAL_VOLUME",
                    "Volume có phần thập phân nhưng schema Supabase hiện "
                    "dùng BIGINT. Cần xác minh schema trước khi ghi.",
                    field="volume",
                )
            )

        # Timestamp
        if timestamp_ms is None:
            errors.append(
                _issue(
                    "INVALID_TIMESTAMP",
                    "Timestamp thiếu hoặc không thể chuẩn hóa.",
                    field="timestamp",
                )
            )

        else:
            current_ms = int(utc_now().timestamp() * 1000)

            if timestamp_ms > (
                current_ms + self.max_future_seconds * 1000
            ):
                errors.append(
                    _issue(
                        "FUTURE_TIMESTAMP",
                        "Timestamp nằm quá xa trong tương lai.",
                        field="timestamp",
                    )
                )

            if timestamp_ms <= 0:
                errors.append(
                    _issue(
                        "NON_POSITIVE_TIMESTAMP",
                        "Timestamp phải là thời điểm hợp lệ.",
                        field="timestamp",
                    )
                )

        # Side
        if side == "INVALID":
            errors.append(
                _issue(
                    "INVALID_SIDE",
                    "Side không hợp lệ. Giá trị chuẩn là BUY, SELL hoặc UNKNOWN.",
                    field="side",
                )
            )

        elif side == "UNKNOWN":
            warnings.append(
                _issue(
                    "UNKNOWN_SIDE",
                    "Không xác định được bên chủ động từ dữ liệu hiện tại.",
                    severity="WARNING",
                    field="side",
                )
            )

        normalized = {
            "source_local_id": local_id,
            "symbol": symbol or None,
            "trade_id": trade_id,
            "price": (
                format(price, "f")
                if price is not None
                else None
            ),
            "volume": (
                format(volume, "f")
                if volume is not None
                else None
            ),
            "side": (
                side
                if side != "INVALID"
                else "UNKNOWN"
            ),
            "timestamp_ms": timestamp_ms,
        }

        return _result(
            valid=not errors,
            errors=errors,
            warnings=warnings,
            normalized=normalized,
        )

    # --------------------------------------------------------
    # 5.2 Validate batch
    # --------------------------------------------------------

    def validate_trades(
        self,
        trades: Iterable[Mapping[str, Any]],
        *,
        require_local_id: bool = False,
    ) -> dict[str, Any]:
        """
        Kiểm tra nhiều giao dịch.

        Phát hiện:
        - Local ID trùng trong batch.
        - Trade ID trùng trong cùng symbol trong batch.

        Không kết luận trade ID trùng giữa hai bản ghi luôn là lỗi:
        cần xác minh quy tắc định danh của nguồn DNSE.
        """

        started_at = utc_iso()

        results: list[dict[str, Any]] = []

        local_ids: Counter[int] = Counter()

        provider_ids: Counter[tuple[str, str]] = Counter()

        total = 0
        valid_count = 0
        invalid_count = 0
        warning_count = 0

        for trade in trades:
            total += 1

            result = self.validate_trade(
                trade,
                require_local_id=require_local_id,
            )

            results.append(result)

            normalized = result.get("normalized") or {}

            local_id = normalized.get("source_local_id")

            symbol = normalized.get("symbol")

            trade_id = normalized.get("trade_id")

            if local_id is not None:
                local_ids[local_id] += 1

            if symbol and trade_id:
                provider_ids[(symbol, trade_id)] += 1

            if result["valid"]:
                valid_count += 1
            else:
                invalid_count += 1

            warning_count += len(result.get("warnings", []))

        duplicate_local_ids = [
            local_id
            for local_id, count in local_ids.items()
            if count > 1
        ]

        duplicate_provider_ids = [
            {
                "symbol": symbol,
                "trade_id": trade_id,
                "count": count,
            }
            for (symbol, trade_id), count in provider_ids.items()
            if count > 1
        ]

        return {
            "checked_at": started_at,
            "finished_at": utc_iso(),
            "total": total,
            "valid": valid_count,
            "invalid": invalid_count,
            "warning_count": warning_count,
            "duplicate_local_ids": duplicate_local_ids,
            "duplicate_provider_ids": duplicate_provider_ids,
            "items": results,
            "ok": (
                invalid_count == 0
                and not duplicate_local_ids
            ),
        }


# ============================================================
# 6. SQLITE VALIDATION
# ============================================================

def validate_sqlite(
    sqlite_path: str | Path = DEFAULT_SQLITE_PATH,
    *,
    limit: Optional[int] = None,
    batch_size: int = 1000,
    strict_symbol_list: Optional[bool] = None,
) -> dict[str, Any]:
    """
    Đọc và kiểm tra dữ liệu SQLite footprint_trades.

    Chỉ đọc database, không thay đổi dữ liệu.
    Hỗ trợ giới hạn số dòng để kiểm tra nhanh.
    """

    path = Path(sqlite_path).expanduser()

    if not path.is_absolute():
        path = PROJECT_ROOT / path

    path = path.resolve()

    report: dict[str, Any] = {
        "checked_at": utc_iso(),
        "sqlite_path": str(path),
        "ok": False,
        "table_exists": False,
        "rows_scanned": 0,
        "valid": 0,
        "invalid": 0,
        "warning_count": 0,
        "min_id": None,
        "max_id": None,
        "total_rows": None,
        "issues": [],
        "error": None,
    }

    if not path.is_file():
        report["error"] = (
            f"Không tìm thấy database: {path}"
        )

        return report

    validator = FootprintDataValidator(
        strict_symbol_list=strict_symbol_list
    )

    connection = None

    try:
        uri = path.as_uri() + "?mode=ro"

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

        report["table_exists"] = True

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
                "Thiếu cột: "
                + ", ".join(sorted(missing))
            )

            return report

        summary = connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                MIN(id) AS min_id,
                MAX(id) AS max_id
            FROM footprint_trades
            """
        ).fetchone()

        report["total_rows"] = int(summary["total"] or 0)
        report["min_id"] = summary["min_id"]
        report["max_id"] = summary["max_id"]

        if limit is not None:
            if limit < 1:
                report["error"] = (
                    "limit phải lớn hơn hoặc bằng 1."
                )

                return report

            sql = """
                SELECT
                    id,
                    symbol,
                    trade_id,
                    timestamp,
                    price,
                    volume,
                    side,
                    received_at,
                    raw_json
                FROM footprint_trades
                ORDER BY id ASC
                LIMIT ?
            """

            cursor = connection.execute(
                sql,
                (limit,),
            )

        else:
            cursor = connection.execute(
                """
                SELECT
                    id,
                    symbol,
                    trade_id,
                    timestamp,
                    price,
                    volume,
                    side,
                    received_at,
                    raw_json
                FROM footprint_trades
                ORDER BY id ASC
                """
            )

        seen_local_ids: set[int] = set()

        seen_provider_ids: set[tuple[str, str]] = set()

        batch_size = max(1, int(batch_size))

        while True:
            rows = cursor.fetchmany(batch_size)

            if not rows:
                break

            for row in rows:
                report["rows_scanned"] += 1

                trade = dict(row)

                result = validator.validate_trade(
                    trade,
                    require_local_id=True,
                )

                local_id = trade.get("id")

                if local_id in seen_local_ids:
                    result["errors"].append(
                        _issue(
                            "DUPLICATE_LOCAL_ID",
                            "ID SQLite bị trùng trong kết quả kiểm tra.",
                            field="id",
                        )
                    )

                    result["valid"] = False

                elif local_id is not None:
                    seen_local_ids.add(local_id)

                symbol = str(
                    trade.get("symbol") or ""
                ).strip().upper()

                trade_id = str(
                    trade.get("trade_id") or ""
                ).strip()

                if symbol and trade_id:
                    key = (symbol, trade_id)

                    if key in seen_provider_ids:
                        result["warnings"].append(
                            _issue(
                                "DUPLICATE_PROVIDER_TRADE_ID",
                                "Trùng symbol + trade_id trong SQLite. "
                                "Cần xác minh trade_id của nguồn có duy nhất "
                                "trong phạm vi symbol hay không.",
                                severity="WARNING",
                                field="trade_id",
                            )
                        )

                    seen_provider_ids.add(key)

                if result["valid"]:
                    report["valid"] += 1
                else:
                    report["invalid"] += 1

                report["warning_count"] += len(
                    result["warnings"]
                )

                # Chỉ lưu các lỗi đầu tiên để tránh báo cáo quá lớn.
                if (
                    result["errors"]
                    or result["warnings"]
                ) and len(report["issues"]) < 200:
                    report["issues"].append(
                        {
                            "local_id": local_id,
                            "symbol": symbol,
                            "errors": result["errors"],
                            "warnings": result["warnings"],
                        }
                    )

        report["ok"] = (
            report["error"] is None
            and report["invalid"] == 0
        )

        logger.info(
            "SQLite validation: path=%s scanned=%s valid=%s "
            "invalid=%s warnings=%s",
            path,
            report["rows_scanned"],
            report["valid"],
            report["invalid"],
            report["warning_count"],
        )

    except sqlite3.Error as exc:
        report["error"] = str(exc)

        logger.exception(
            "Không thể kiểm tra SQLite Footprint."
        )

    finally:
        if connection is not None:
            connection.close()

    return report


# ============================================================
# 7. VALIDATE BEFORE SUPABASE SAVE
# ============================================================

def validate_before_supabase(
    trade: Mapping[str, Any],
    *,
    require_local_id: bool = True,
) -> dict[str, Any]:
    """
    Điểm gọi dùng trước SupabaseStore.save_trade().

    Hàm không ghi dữ liệu.
    Caller phải chủ động quyết định có tiếp tục lưu hay không.
    """

    validator = FootprintDataValidator()

    result = validator.validate_trade(
        trade,
        require_local_id=require_local_id,
    )

    if not result["valid"]:
        logger.warning(
            "Trade không hợp lệ trước Supabase: "
            "code=%s local_id=%s",
            [
                item["code"]
                for item in result["errors"]
            ],
            _first(
                trade,
                "source_local_id",
                "id",
            ),
        )

    return result


# ============================================================
# 8. CLI OUTPUT
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
    print("LHP-FI FOOTPRINT DATA VALIDATOR")
    print("=" * 68)

    print(f"SQLite        : {report.get('sqlite_path')}")
    print(f"Database OK   : {report.get('ok')}")
    print(f"Table exists  : {report.get('table_exists')}")
    print(f"Total rows    : {report.get('total_rows')}")
    print(f"Rows scanned  : {report.get('rows_scanned')}")
    print(f"Valid         : {report.get('valid')}")
    print(f"Invalid       : {report.get('invalid')}")
    print(f"Warnings      : {report.get('warning_count')}")
    print(f"Min ID        : {report.get('min_id')}")
    print(f"Max ID        : {report.get('max_id')}")

    if report.get("error"):
        print(f"Error         : {report.get('error')}")

    issues = report.get("issues") or []

    if issues:
        print()
        print("--- ISSUES (tối đa 200 bản ghi đầu tiên) ---")

        for item in issues:
            print(
                json.dumps(
                    item,
                    ensure_ascii=False,
                    default=str,
                )
            )

    print()
    print(f"Log file: {LOG_FILE}")
    print("=" * 68)
    print()


# ============================================================
# 9. MAIN
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Kiểm tra chất lượng dữ liệu LHP-FI Footprint."
        )
    )

    parser.add_argument(
        "--sqlite-path",
        default=str(DEFAULT_SQLITE_PATH),
        help="Đường dẫn SQLite.",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Chỉ kiểm tra N giao dịch đầu tiên.",
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Số dòng đọc mỗi batch.",
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="Xuất báo cáo JSON.",
    )

    parser.add_argument(
        "--strict-symbol-list",
        action="store_true",
        help="Báo lỗi nếu symbol không nằm trong FOOTPRINT_SYMBOLS.",
    )

    args = parser.parse_args()

    report = validate_sqlite(
        sqlite_path=args.sqlite_path,
        limit=args.limit,
        batch_size=args.batch_size,
        strict_symbol_list=args.strict_symbol_list,
    )

    print_report(
        report,
        as_json=args.json,
    )

    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

