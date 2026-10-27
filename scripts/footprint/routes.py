"""
DNSE Footprint Flask Routes
===========================

Blueprint API cho hệ thống DNSE Footprint.

URL prefix:
    /api/footprint

Endpoints:
    GET  /api/footprint/
    GET  /api/footprint/health
    GET  /api/footprint/symbols
    GET  /api/footprint/stream/status

    POST /api/footprint/stream/start
    POST /api/footprint/stream/stop

    GET  /api/footprint/<symbol>
    GET  /api/footprint/<symbol>/trades
    GET  /api/footprint/<symbol>/candles

    POST /api/footprint/<symbol>/test-tick

Nguyên tắc:
    - Không kết nối DNSE khi import module.
    - Sử dụng duy nhất footprint_engine dùng chung.
    - Hạn chế khởi tạo stream nhiều lần.
    - Không mặc định xem API phản hồi thành công là DNSE đang LIVE.
    - Không coi dữ liệu RAM là dữ liệu đã được lưu bền vững.
    - Kiểm tra SQLite nếu local_store có sẵn.
    - Không làm lộ chi tiết lỗi nội bộ trên production.
"""

from __future__ import annotations

import hmac
import json
import logging
import math
import os
import re
import threading
import time

from datetime import datetime, timezone
from pathlib import Path
from functools import wraps
from typing import Any, Callable

from flask import Blueprint, current_app, jsonify, request

from scripts.footprint.engine import footprint_engine

from scripts.footprint.config import (
    FOOTPRINT_SYMBOLS,
    FOOTPRINT_ENABLE_STREAM_CONTROL,
    FOOTPRINT_ENABLE_TEST_TICK,
    DNSE_FOOTPRINT_AUTOSTART,
    validate_config,
)


# ============================================================
# 1. LOGGER
# ============================================================

logger = logging.getLogger(
    "lhp_ai_server.footprint.routes"
)


# ============================================================
# 2. BLUEPRINT
# ============================================================

footprint_bp = Blueprint(
    "footprint",
    __name__,
    url_prefix="/api/footprint",
)


# ============================================================
# 3. CONSTANTS
# ============================================================

API_VERSION = "2.0"

DEFAULT_TRADE_LIMIT = 100
MAX_TRADE_LIMIT = 5000

DEFAULT_CANDLE_LIMIT = 100
MAX_CANDLE_LIMIT = 5000

DEFAULT_SYMBOL_LIMIT = 100

# Chỉ kiểm tra hình dạng mã cổ phiếu.
# Không xác nhận mã đó thực sự được niêm yết.
SYMBOL_PATTERN = re.compile(
    r"^[A-Z0-9._-]{1,20}$"
)


# ============================================================
# 4. SHARED STATE
# ============================================================

_stream_instance = None
_stream_import_error: str | None = None

_stream_lock = threading.RLock()
_autostart_lock = threading.Lock()

_autostart_attempted = False


# ============================================================
# 5. TIME AND RESPONSE HELPERS
# ============================================================

def _utc_now() -> str:
    """Trả về thời gian UTC ISO 8601."""
    return datetime.now(timezone.utc).isoformat()


def _success(
    data: Any = None,
    *,
    message: str = "OK",
    status_code: int = 200,
):
    """Chuẩn hóa response thành công."""
    payload = {
        "success": True,
        "message": message,
        "timestamp": _utc_now(),
        "api_version": API_VERSION,
    }

    if data is not None:
        payload["data"] = data

    response = jsonify(payload)

    response.headers["Cache-Control"] = "no-store"

    return response, status_code


def _error(
    message: str,
    *,
    status_code: int = 400,
    details: Any = None,
):
    """Chuẩn hóa response lỗi."""
    payload = {
        "success": False,
        "error": message,
        "timestamp": _utc_now(),
        "api_version": API_VERSION,
    }

    if details is not None:
        payload["details"] = details

    response = jsonify(payload)

    response.headers["Cache-Control"] = "no-store"

    return response, status_code


def _exception_response(
    message: str,
    exc: Exception,
    *,
    status_code: int = 500,
):
    """
    Ghi lỗi đầy đủ vào log.

    Không trả exception nội bộ cho client trên production.
    """
    logger.exception(
        "%s: %s",
        message,
        exc,
    )

    details = None

    if not _is_production():
        details = str(exc)

    return _error(
        message,
        status_code=status_code,
        details=details,
    )


def _is_production() -> bool:
    """Kiểm tra môi trường production."""
    values = (
        os.getenv("FLASK_ENV", ""),
        os.getenv("APP_ENV", ""),
        os.getenv("ENVIRONMENT", ""),
    )

    return any(
        value.strip().lower() in {
            "production",
            "prod",
        }
        for value in values
    )


def _json_body() -> dict[str, Any]:
    """Đọc JSON object từ request."""
    body = request.get_json(silent=True)

    if isinstance(body, dict):
        return body

    return {}


def _safe_int(
    value: Any,
    default: int,
    *,
    minimum: int = 1,
    maximum: int = 1000,
) -> int:
    """Đọc số nguyên và giới hạn phạm vi."""
    try:
        if isinstance(value, bool):
            return default

        result = int(value)

    except (TypeError, ValueError, OverflowError):
        result = default

    return max(
        minimum,
        min(result, maximum),
    )


def _parse_bool(
    value: Any,
    *,
    default: bool = False,
) -> bool:
    """Chuyển chuỗi query thành boolean."""
    if value is None:
        return default

    if isinstance(value, bool):
        return value

    normalized = str(value).strip().lower()

    if normalized in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return True

    if normalized in {
        "0",
        "false",
        "no",
        "off",
    }:
        return False

    return default


def _normalize_symbol(
    value: Any,
) -> str | None:
    """
    Chuẩn hóa symbol trước khi truyền vào Engine.

    Không cho phép chuỗi rỗng hoặc ký tự bất thường.
    """
    if value is None:
        return None

    symbol = str(value).strip().upper()

    if not symbol:
        return None

    if not SYMBOL_PATTERN.fullmatch(symbol):
        return None

    return symbol


def _registered_symbols() -> list[str]:
    """Đăng ký các mã được cấu hình vào Engine."""
    configured = []

    for item in FOOTPRINT_SYMBOLS:
        symbol = _normalize_symbol(item)

        if symbol:
            configured.append(symbol)

    footprint_engine.register_symbols(configured)

    return sorted(set(configured))


# ============================================================
# 6. LOCAL STORAGE
# ============================================================

def _get_local_store():
    """
    Lấy LocalStore theo kiểu lazy import.

    Không làm ứng dụng Flask lỗi chỉ vì local_store chưa được
    tích hợp hoặc SQLite chưa sẵn sàng.

    local_store.py cần cung cấp:
        get_local_store()
    """
    try:
        from scripts.footprint.local_store import (
            get_local_store,
        )

        return get_local_store()

    except Exception as exc:
        logger.warning(
            "LocalStore chưa khả dụng: %s",
            exc,
        )

        return None


def _storage_status() -> dict[str, Any]:
    """
    Lấy trạng thái lưu trữ.

    Không tuyên bố dữ liệu đã đồng bộ chỉ vì SQLite tồn tại.
    """
    store = _get_local_store()

    if store is None:
        return {
            "available": False,
            "backend": "SQLite",
            "status": "UNAVAILABLE",
            "persistent": False,
            "message": (
                "Không khởi tạo được LocalStore. "
                "Kiểm tra local_store.py và cấu hình SQLite."
            ),
        }

    try:
        if not hasattr(store, "get_storage_status"):
            return {
                "available": True,
                "backend": "SQLite",
                "status": "UNKNOWN",
                "persistent": True,
                "message": (
                    "LocalStore không có get_storage_status()."
                ),
            }

        status = store.get_storage_status()

        if not isinstance(status, dict):
            return {
                "available": True,
                "backend": "SQLite",
                "status": "UNKNOWN",
                "persistent": True,
                "message": (
                    "LocalStore trả về trạng thái không hợp lệ."
                ),
            }

        return {
            "available": True,
            "backend": "SQLite",
            "status": "AVAILABLE",
            "persistent": True,
            "details": status,
        }

    except Exception as exc:
        logger.exception(
            "Không đọc được trạng thái SQLite."
        )

        return {
            "available": False,
            "backend": "SQLite",
            "status": "ERROR",
            "persistent": False,
            "error": (
                str(exc)
                if not _is_production()
                else "Không đọc được trạng thái SQLite."
            ),
        }


# ============================================================
# 7. STREAM MANAGER
# ============================================================

def _get_stream():
    """
    Lấy stream singleton.

    Chỉ import module, không chủ động kết nối DNSE.
    """
    global _stream_instance
    global _stream_import_error

    with _stream_lock:
        if _stream_instance is not None:
            return _stream_instance

        try:
            from scripts.footprint.dnse_stream import (
                stream,
            )

            _stream_instance = stream
            _stream_import_error = None

            return _stream_instance

        except Exception as exc:
            _stream_import_error = str(exc)

            logger.exception(
                "Không import được DNSE Footprint Stream."
            )

            return None


def _stream_status_payload() -> dict[str, Any]:
    """Lấy trạng thái stream an toàn."""
    stream_instance = _get_stream()

    if stream_instance is None:
        return {
            "available": False,
            "running": False,
            "connection_state": "UNAVAILABLE",
            "error": (
                _stream_import_error
                if not _is_production()
                else "DNSE stream chưa khả dụng."
            ),
        }

    try:
        if hasattr(stream_instance, "get_status"):
            status = stream_instance.get_status()

            if isinstance(status, dict):
                result = dict(status)

                result["available"] = True

                return result

            return {
                "available": True,
                "running": None,
                "status": str(status),
            }

        if hasattr(stream_instance, "is_running"):
            running = bool(
                stream_instance.is_running()
            )

            return {
                "available": True,
                "running": running,
                "connection_state": (
                    "RUNNING"
                    if running
                    else "STOPPED"
                ),
            }

        return {
            "available": True,
            "running": None,
            "connection_state": "UNKNOWN",
            "message": (
                "Stream chưa cung cấp get_status() "
                "hoặc is_running()."
            ),
        }

    except Exception as exc:
        logger.exception(
            "Không lấy được trạng thái stream."
        )

        return {
            "available": True,
            "running": False,
            "connection_state": "ERROR",
            "error": (
                str(exc)
                if not _is_production()
                else "Không đọc được trạng thái stream."
            ),
        }


def _stream_running() -> bool:
    """Kiểm tra stream có đang chạy hay không."""
    status = _stream_status_payload()

    if "running" in status:
        return status["running"] is True

    state = str(
        status.get("connection_state", "")
    ).upper()

    return state in {
        "LIVE",
        "RUNNING",
        "CONNECTED",
    }


# ============================================================
# 8. STREAM CONTROL AUTHORIZATION
# ============================================================

def _control_authorized() -> bool:
    """
    Kiểm tra quyền điều khiển stream.

    Nếu có FOOTPRINT_CONTROL_TOKEN:
        Yêu cầu header X-Footprint-Token.

    Nếu không có token:
        Chỉ cho phép trong môi trường development.
    """
    expected_token = os.getenv(
        "FOOTPRINT_CONTROL_TOKEN",
        "",
    ).strip()

    supplied_token = request.headers.get(
        "X-Footprint-Token",
        "",
    ).strip()

    if expected_token:
        if not supplied_token:
            return False

        return hmac.compare_digest(
            supplied_token,
            expected_token,
        )

    if _is_production():
        return False

    return True


def _require_control_permission(
    function: Callable,
) -> Callable:
    """Bảo vệ endpoint điều khiển."""
    @wraps(function)
    def wrapper(*args, **kwargs):
        if not _control_authorized():
            return _error(
                "Không có quyền điều khiển Footprint.",
                status_code=403,
                details=(
                    "Cần cấu hình FOOTPRINT_CONTROL_TOKEN "
                    "và gửi header X-Footprint-Token."
                    if _is_production()
                    else None
                ),
            )

        return function(*args, **kwargs)

    return wrapper


# ============================================================
# 9. HEALTH CHECK
# ============================================================

@footprint_bp.get("/health")
def footprint_health():
    """
    GET /api/footprint/health

    Kiểm tra:
        - Blueprint/API.
        - Cấu hình.
        - Engine.
        - Stream.
        - SQLite.

    API hoạt động không đồng nghĩa với DNSE đang LIVE.
    """
    try:
        config_errors = validate_config(
            require_credentials=False,
        )

        engine_status = footprint_engine.get_status()
        stream_status = _stream_status_payload()
        storage_status = _storage_status()

        stream_available = (
            stream_status.get("available") is True
        )

        stream_running = (
            stream_status.get("running") is True
        )

        storage_available = (
            storage_status.get("available") is True
        )

        if stream_running:
            service_status = "LIVE"

        elif not stream_available:
            service_status = "DEGRADED"

        elif not config_errors:
            service_status = "READY"

        else:
            service_status = "CONFIG_ERROR"

        data = {
            "service": "DNSE Footprint",
            "status": service_status,
            "api_available": True,
            "engine_available": True,
            "stream_available": stream_available,
            "stream_running": stream_running,
            "storage_available": storage_available,
            "config_valid": len(config_errors) == 0,
            "config_errors": config_errors,
            "stream": stream_status,
            "engine": engine_status,
            "storage": storage_status,
            "checked_at": _utc_now(),
        }

        return _success(
            data,
            message="Footprint health check completed",
        )

    except Exception as exc:
        return _exception_response(
            "Footprint health check thất bại.",
            exc,
        )


# ============================================================
# 10. SYMBOLS
# ============================================================

@footprint_bp.get("/symbols")
def footprint_symbols():
    """
    GET /api/footprint/symbols

    Trả về:
        - Các mã cấu hình.
        - Các mã đã đăng ký trong Engine.
        - Thống kê riêng từng mã.
    """
    try:
        configured_symbols = _registered_symbols()

        engine_status = footprint_engine.get_status()

        engine_symbols = footprint_engine.get_symbols()

        return _success(
            {
                "configured_symbols": configured_symbols,
                "engine_symbols": engine_symbols,
                "count": len(engine_symbols),
                "symbol_status": engine_status.get(
                    "symbols",
                    {},
                ),
            },
            message="Lấy danh sách mã thành công",
        )

    except Exception as exc:
        return _exception_response(
            "Không lấy được danh sách mã cổ phiếu.",
            exc,
        )


# ============================================================
# 11. STREAM STATUS
# ============================================================

@footprint_bp.get("/stream/status")
def footprint_stream_status():
    """
    GET /api/footprint/stream/status

    Phản hồi riêng trạng thái:
        - DNSE Stream.
        - Footprint Engine.
        - SQLite.
    """
    try:
        stream_status = _stream_status_payload()
        engine_status = footprint_engine.get_status()
        storage_status = _storage_status()

        return _success(
            {
                "stream": stream_status,
                "engine": engine_status,
                "storage": storage_status,
                "checked_at": _utc_now(),
            },
            message="Lấy trạng thái Footprint thành công",
        )

    except Exception as exc:
        return _exception_response(
            "Không lấy được trạng thái stream.",
            exc,
        )


# ============================================================
# 12. START STREAM
# ============================================================

@footprint_bp.post("/stream/start")
@_require_control_permission
def footprint_stream_start():
    """
    POST /api/footprint/stream/start

    Yêu cầu:
        FOOTPRINT_ENABLE_STREAM_CONTROL=true
        Cấu hình DNSE hợp lệ.
    """
    if not FOOTPRINT_ENABLE_STREAM_CONTROL:
        return _error(
            "Chức năng điều khiển stream đang bị tắt.",
            status_code=403,
        )

    try:
        errors = validate_config(
            require_credentials=True,
        )

        if errors:
            return _error(
                "Cấu hình DNSE chưa hợp lệ.",
                status_code=400,
                details=errors,
            )

        stream_instance = _get_stream()

        if stream_instance is None:
            return _error(
                "Không tải được DNSE stream.",
                status_code=503,
                details=(
                    _stream_import_error
                    if not _is_production()
                    else None
                ),
            )

        if not hasattr(stream_instance, "start"):
            return _error(
                "DNSE stream chưa cung cấp start().",
                status_code=500,
            )

        # start() phải tự chống khởi động nhiều lần.
        # Route chỉ gửi yêu cầu, không tuyên bố LIVE ngay lập tức.
        result = stream_instance.start()

        return _success(
            {
                "start_result": result,
                "stream": _stream_status_payload(),
                "note": (
                    "Yêu cầu khởi động đã được gửi. "
                    "Kiểm tra connection_state để xác nhận kết nối."
                ),
            },
            message="Đã gửi yêu cầu khởi động DNSE stream",
            status_code=202,
        )

    except Exception as exc:
        return _exception_response(
            "Không khởi động được DNSE stream.",
            exc,
        )


# ============================================================
# 13. STOP STREAM
# ============================================================

@footprint_bp.post("/stream/stop")
@_require_control_permission
def footprint_stream_stop():
    """POST /api/footprint/stream/stop."""
    if not FOOTPRINT_ENABLE_STREAM_CONTROL:
        return _error(
            "Chức năng điều khiển stream đang bị tắt.",
            status_code=403,
        )

    try:
        stream_instance = _get_stream()

        if stream_instance is None:
            return _error(
                "Không tải được DNSE stream.",
                status_code=503,
                details=(
                    _stream_import_error
                    if not _is_production()
                    else None
                ),
            )

        if not hasattr(stream_instance, "stop"):
            return _error(
                "DNSE stream chưa cung cấp stop().",
                status_code=500,
            )

        result = stream_instance.stop()

        return _success(
            {
                "stop_result": result,
                "stream": _stream_status_payload(),
            },
            message="Đã gửi yêu cầu dừng DNSE stream",
            status_code=202,
        )

    except Exception as exc:
        return _exception_response(
            "Không dừng được DNSE stream.",
            exc,
        )


# ============================================================
# 14. FOOTPRINT BY SYMBOL
# ============================================================

@footprint_bp.get("/<string:symbol>")
def footprint_by_symbol(symbol: str):
    """
    GET /api/footprint/DXG

    Query:
        sort=price|volume|delta
        descending=true|false
    """
    normalized_symbol = _normalize_symbol(symbol)

    if not normalized_symbol:
        return _error(
            "Mã cổ phiếu không hợp lệ.",
            status_code=400,
        )

    sort = request.args.get(
        "sort",
        "price",
    ).strip().lower()

    if sort not in {
        "price",
        "volume",
        "delta",
    }:
        return _error(
            "Tham số sort không hợp lệ.",
            status_code=400,
            details={
                "allowed_values": [
                    "price",
                    "volume",
                    "delta",
                ],
            },
        )

    descending = _parse_bool(
        request.args.get("descending"),
        default=False,
    )

    try:
        result = footprint_engine.get_footprint(
            normalized_symbol,
            sort=sort,
            descending=descending,
        )

        if not result.get("success"):
            return _error(
                result.get(
                    "error",
                    "Không lấy được Footprint.",
                ),
                status_code=400,
            )

        return _success(
            result,
            message=(
                f"Lấy Footprint {normalized_symbol} thành công"
            ),
        )

    except Exception as exc:
        return _exception_response(
            f"Lỗi lấy Footprint cho {normalized_symbol}.",
            exc,
        )


# ============================================================
# 15. TRADE TAPE
# ============================================================

@footprint_bp.get("/<string:symbol>/trades")
def footprint_trades(symbol: str):
    """
    GET /api/footprint/DXG/trades

    Query:
        limit=100
        newest_first=true
    """
    normalized_symbol = _normalize_symbol(symbol)

    if not normalized_symbol:
        return _error(
            "Mã cổ phiếu không hợp lệ.",
            status_code=400,
        )

    limit = _safe_int(
        request.args.get("limit"),
        DEFAULT_TRADE_LIMIT,
        minimum=1,
        maximum=MAX_TRADE_LIMIT,
    )

    newest_first = _parse_bool(
        request.args.get("newest_first"),
        default=True,
    )

    try:
        result = footprint_engine.get_trades(
            normalized_symbol,
            limit=limit,
            newest_first=newest_first,
        )

        if not result.get("success"):
            return _error(
                result.get(
                    "error",
                    "Không lấy được trade tape.",
                ),
                status_code=400,
            )

        return _success(
            result,
            message=(
                f"Lấy giao dịch {normalized_symbol} thành công"
            ),
        )

    except Exception as exc:
        return _exception_response(
            f"Lỗi lấy trade tape cho {normalized_symbol}.",
            exc,
        )


# ============================================================
# 16. CANDLES
# ============================================================

@footprint_bp.get("/<string:symbol>/candles")
def footprint_candles(symbol: str):
    """
    GET /api/footprint/DXG/candles

    Query:
        limit=100
        newest_first=false
    """
    normalized_symbol = _normalize_symbol(symbol)

    if not normalized_symbol:
        return _error(
            "Mã cổ phiếu không hợp lệ.",
            status_code=400,
        )

    limit = _safe_int(
        request.args.get("limit"),
        DEFAULT_CANDLE_LIMIT,
        minimum=1,
        maximum=MAX_CANDLE_LIMIT,
    )

    newest_first = _parse_bool(
        request.args.get("newest_first"),
        default=False,
    )

    try:
        result = footprint_engine.get_candles(
            normalized_symbol,
            limit=limit,
            newest_first=newest_first,
        )

        if not result.get("success"):
            return _error(
                result.get(
                    "error",
                    "Không lấy được dữ liệu nến.",
                ),
                status_code=400,
            )

        return _success(
            result,
            message=(
                f"Lấy nến {normalized_symbol} thành công"
            ),
        )

    except Exception as exc:
        return _exception_response(
            f"Lỗi lấy candles cho {normalized_symbol}.",
            exc,
        )


# ============================================================
# 17. TEST TICK
# ============================================================

@footprint_bp.post("/<string:symbol>/test-tick")
@_require_control_permission
def footprint_test_tick(symbol: str):
    """
    POST /api/footprint/DXG/test-tick

    Chỉ dành cho development.

    JSON:
        {
            "price": 15.25,
            "quantity": 1000,
            "side": "BUY",
            "timestamp": 1792718100,
            "trade_id": "local-test-001"
        }
    """
    if not FOOTPRINT_ENABLE_TEST_TICK:
        return _error(
            "Endpoint test-tick đang bị tắt.",
            status_code=403,
        )

    if _is_production():
        return _error(
            "Không cho phép test-tick trên production.",
            status_code=403,
        )

    normalized_symbol = _normalize_symbol(symbol)

    if not normalized_symbol:
        return _error(
            "Mã cổ phiếu không hợp lệ.",
            status_code=400,
        )

    if not request.is_json:
        return _error(
            "Request phải có Content-Type: application/json.",
            status_code=415,
        )

    body = _json_body()

    price = body.get("price")

    quantity = body.get(
        "quantity",
        body.get("volume"),
    )

    if price is None or quantity is None:
        return _error(
            "Thiếu trường price hoặc quantity.",
            status_code=400,
        )

    try:
        price_number = float(price)
        quantity_number = float(quantity)

    except (TypeError, ValueError, OverflowError):
        return _error(
            "price hoặc quantity không phải số hợp lệ.",
            status_code=400,
        )

    if (
        not math.isfinite(price_number)
        or price_number <= 0
        or not math.isfinite(quantity_number)
        or quantity_number <= 0
    ):
        return _error(
            "price và quantity phải là số hữu hạn lớn hơn 0.",
            status_code=400,
        )

    trade = {
        "symbol": normalized_symbol,
        "price": price_number,
        "quantity": quantity_number,
        "side": body.get("side"),
        "timestamp": body.get(
            "timestamp",
            time.time(),
        ),
        "trade_id": body.get("trade_id"),
    }

    try:
        accepted = footprint_engine.add_trade(trade)

        if not accepted:
            return _error(
                "Engine từ chối giao dịch thử nghiệm. "
                "Có thể dữ liệu không hợp lệ hoặc trade_id bị trùng.",
                status_code=400,
            )

        return _success(
            {
                "accepted": True,
                "trade": trade,
                "footprint": footprint_engine.get_footprint(
                    normalized_symbol,
                ),
                "storage": {
                    "persisted": False,
                    "message": (
                        "Giao dịch test được đưa vào Engine RAM. "
                        "Endpoint này không tự ghi SQLite."
                    ),
                },
            },
            message="Đã thêm giao dịch thử nghiệm",
            status_code=201,
        )

    except Exception as exc:
        return _exception_response(
            f"Lỗi test-tick cho {normalized_symbol}.",
            exc,
        )


# ============================================================
# 18. STORAGE STATUS
# ============================================================

@footprint_bp.get("/storage/status")
def footprint_storage_status():
    """
    GET /api/footprint/storage/status

    Kiểm tra trạng thái LocalStore/SQLite.
    """
    try:
        status = _storage_status()

        status_code = (
            200
            if status.get("available")
            else 503
        )

        return _success(
            status,
            message="Đã kiểm tra trạng thái lưu trữ",
            status_code=status_code,
        )

    except Exception as exc:
        return _exception_response(
            "Không kiểm tra được trạng thái lưu trữ.",
            exc,
        )


# ============================================================
# 19. INDEX
# ============================================================

@footprint_bp.get("/")
def footprint_index():
    """
    GET /api/footprint/

    Trả về danh sách endpoint và trạng thái tổng quan.
    """
    return _success(
        {
            "service": "DNSE Footprint API",
            "version": API_VERSION,
            "endpoints": {
                "index": "GET /api/footprint/",
                "health": "GET /api/footprint/health",
                "symbols": "GET /api/footprint/symbols",
                "stream_status": (
                    "GET /api/footprint/stream/status"
                ),
                "stream_start": (
                    "POST /api/footprint/stream/start"
                ),
                "stream_stop": (
                    "POST /api/footprint/stream/stop"
                ),
                "footprint": (
                    "GET /api/footprint/<symbol>"
                ),
                "trades": (
                    "GET /api/footprint/<symbol>/trades"
                ),
                "candles": (
                    "GET /api/footprint/<symbol>/candles"
                ),
                "storage_status": (
                    "GET /api/footprint/storage/status"
                ),
                "test_tick": (
                    "POST /api/footprint/<symbol>/test-tick"
                ),
            },
            "storage": {
                "engine": "RAM",
                "database": "SQLite",
                "database_integration": (
                    "Requires stream persistence and engine recovery."
                ),
            },
        },
        message="DNSE Footprint API is available",
    )


# ============================================================
# 20. AUTOSTART
# ============================================================

def start_stream_if_enabled() -> bool:
    """
    Gọi từ app.py sau khi đăng ký Blueprint.

    Không tự chạy khi import module.

    Lưu ý:
        - Chỉ nên gọi tại tiến trình chính.
        - Flask debug reloader hoặc nhiều worker có thể gọi
          hàm này ở nhiều tiến trình khác nhau.
        - Biến khóa bên dưới chỉ bảo vệ trong cùng một tiến trình.
    """
    global _autostart_attempted

    if not DNSE_FOOTPRINT_AUTOSTART:
        logger.info(
            "DNSE Footprint autostart đang tắt."
        )
        return False

    with _autostart_lock:
        if _autostart_attempted:
            logger.info(
                "Footprint autostart đã được thử trước đó."
            )
            return _stream_running()

        _autostart_attempted = True

    errors = validate_config(
        require_credentials=True,
    )

    if errors:
        logger.error(
            "Không autostart Footprint vì cấu hình lỗi: %s",
            errors,
        )
        return False

    stream_instance = _get_stream()

    if stream_instance is None:
        logger.error(
            "Không autostart được Footprint: %s",
            _stream_import_error,
        )
        return False

    try:
        stream_instance.start()

        logger.info(
            "Đã gửi yêu cầu autostart DNSE Footprint."
        )

        # start() có thể chỉ khởi động thread.
        # Không giả định DNSE đã kết nối thành công.
        return True

    except Exception:
        logger.exception(
            "Autostart DNSE Footprint thất bại."
        )
        return False


# ============================================================
# 21. BLUEPRINT ERROR HANDLERS
# ============================================================

@footprint_bp.errorhandler(404)
def footprint_not_found(error):
    return _error(
        "Không tìm thấy endpoint Footprint.",
        status_code=404,
    )


@footprint_bp.errorhandler(405)
def footprint_method_not_allowed(error):
    return _error(
        "HTTP method không được hỗ trợ.",
        status_code=405,
    )


@footprint_bp.errorhandler(413)
def footprint_request_too_large(error):
    return _error(
        "Request vượt quá dung lượng cho phép.",
        status_code=413,
    )


@footprint_bp.errorhandler(500)
def footprint_internal_error(error):
    logger.error(
        "Footprint Blueprint internal error: %s",
        error,
    )

    return _error(
        "Lỗi nội bộ Footprint API.",
        status_code=500,
    )


    

# ============================================================
# 22. ADMIN API: SYNC, VALIDATION, RETENTION
# ============================================================

_admin_run_history_lock = threading.Lock()


def _admin_run_history_path() -> Path:
    return Path(__file__).resolve().parents[2] / "logs" / "footprint_admin_sync_runs.jsonl"


def _record_admin_sync_run(result: dict[str, Any]) -> None:
    """Append a minimal, secret-free history record for admin-triggered sync runs."""
    sync_result = result.get("sync_result") if isinstance(result.get("sync_result"), dict) else {}
    record = {
        "id": f"admin-{int(time.time() * 1000)}-{threading.get_ident()}",
        "sync_name": str(result.get("sync_name") or "sqlite_to_supabase")[:100],
        "started_at": result.get("started_at"),
        "finished_at": result.get("finished_at") or _utc_now(),
        "status": "SUCCESS" if result.get("ok") else "FAILED",
        "dry_run": bool(result.get("dry_run")),
        "duration_seconds": result.get("duration_seconds"),
        "read": sync_result.get("read", 0),
        "upserted": sync_result.get("upserted", 0),
        "failed": sync_result.get("failed", 0),
        "last_local_id": sync_result.get("last_local_id"),
        # Do not persist raw exception text because third-party errors can contain sensitive details.
        "error_summary": None if result.get("ok") else "Lượt sync thất bại; xem log backend để biết chi tiết.",
        "source": "admin_api_local",
    }
    path = _admin_run_history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _admin_run_history_lock:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        # Bound the local history file to prevent unbounded growth.
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) > 1000:
                path.write_text("\n".join(lines[-500:]) + "\n", encoding="utf-8")
        except OSError:
            logger.warning("Không thể thu gọn local sync history file.")


def _read_admin_sync_runs(limit: int) -> list[dict[str, Any]]:
    path = _admin_run_history_path()
    if not path.is_file():
        return []
    with _admin_run_history_lock:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-max(limit, 1):]
        except OSError:
            logger.exception("Không thể đọc local sync history.")
            return []
    rows = []
    for line in lines:
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
        except json.JSONDecodeError:
            continue
    return sorted(rows, key=lambda row: str(row.get("started_at") or ""), reverse=True)


def _admin_sqlite_path():
    """Resolve SQLite path from the same environment used by backend workers."""
    from pathlib import Path
    configured = os.getenv("FOOTPRINT_SQLITE_PATH", "")
    if configured.strip():
        path = Path(configured).expanduser()
        if not path.is_absolute():
            # Keep relative paths anchored to the backend project root.
            path = Path(__file__).resolve().parents[2] / path
        return path.resolve()
    return Path(__file__).resolve().parents[2] / "data" / "footprint.db"


def _admin_safe_sync_state(state: dict[str, Any] | None) -> dict[str, Any]:
    """Expose operational fields only; never return credentials or arbitrary row data."""
    row = state or {}
    return {
        "sync_name": row.get("sync_name", "sqlite_to_supabase"),
        "status": row.get("status"),
        "last_local_trade_id": row.get("last_local_trade_id", 0),
        "last_local_candle_id": row.get("last_local_candle_id", 0),
        "last_attempt_at": row.get("last_attempt_at"),
        "last_success_at": row.get("last_success_at"),
        "updated_at": row.get("updated_at"),
        "error_message": str(row.get("error_message") or "")[:1000] or None,
        "state_exists": bool(state),
    }


@footprint_bp.get("/sync/status")
@_require_control_permission
def footprint_admin_sync_status():
    """Read-only sync status and safe Supabase configuration diagnostics."""
    try:
        from scripts.footprint.sync_state import SyncStateManager
        from scripts.footprint.supabase_store import get_supabase_store

        sync_name = (request.args.get("sync_name") or "sqlite_to_supabase").strip()[:100]
        store = get_supabase_store()
        state = store.get_sync_state(sync_name)
        status = store.get_status()
        sqlite_path = _admin_sqlite_path()
        sqlite_info = {
            "path": str(sqlite_path),
            "exists": sqlite_path.is_file(),
            "size_bytes": sqlite_path.stat().st_size if sqlite_path.is_file() else 0,
        }
        return _success({
            "sync_name": sync_name,
            "state": _admin_safe_sync_state(state),
            "sqlite": sqlite_info,
            "supabase": {
                "enabled": bool(status.get("enabled")),
                "configured": bool(status.get("configured")),
                "source": status.get("source"),
                "batch_size": status.get("batch_size"),
                "retries": status.get("retries"),
                "url_present": bool(status.get("url_present")),
                "key_present": bool(status.get("key_present")),
                "jwt_role_claim": status.get("jwt_role_claim"),
            },
            "checked_at": _utc_now(),
        })
    except Exception as exc:
        return _exception_response("Không thể đọc trạng thái sync.", exc)


@footprint_bp.get("/sync/runs")
@_require_control_permission
def footprint_admin_sync_runs():
    """Read bounded sync history from Supabase, with local audit fallback."""
    limit = _safe_int(request.args.get("limit"), 50, minimum=1, maximum=200)
    local_rows = _read_admin_sync_runs(limit)
    warning = None
    source = "local"
    rows = []
    try:
        from scripts.footprint.supabase_store import get_supabase_store
        rows = get_supabase_store().list_sync_runs(limit)
        source = "supabase"
    except Exception as exc:
        logger.warning("Không đọc được footprint_sync_runs; dùng local history: %s", exc)
        warning = "Không đọc được footprint_sync_runs; đang hiển thị lịch sử do admin API ghi cục bộ."
        rows = []
    # Local API-triggered records remain useful even if the Supabase table is empty.
    known_ids = {str(row.get("id")) for row in rows if row.get("id") is not None}
    rows.extend(row for row in local_rows if str(row.get("id")) not in known_ids)
    sort_fields = ("started_at", "created_at", "run_started_at", "finished_at", "updated_at")
    def sort_key(row):
        for field in sort_fields:
            value = row.get(field) if isinstance(row, dict) else None
            if value is not None:
                return str(value)
        return ""
    rows = sorted((r for r in rows if isinstance(r, dict)), key=sort_key, reverse=True)[:limit]
    if source == "supabase" and local_rows:
        source = "combined"
    return _success({
        "items": rows,
        "count": len(rows),
        "limit": limit,
        "source": source,
        "warning": warning,
    })


@footprint_bp.post("/sync/run")
@_require_control_permission
def footprint_admin_sync_run():
    """Run a bounded sync pass; dry-run is the default and real sync is explicit."""
    try:
        from scripts.footprint.sync_manager import (
            SyncManager, process_lock, SyncAlreadyRunningError,
        )
        body = _json_body()
        dry_run = _parse_bool(body.get("dry_run"), default=True)
        max_batches = _safe_int(body.get("max_batches"), 2, minimum=1, maximum=20)
        sync_name = str(body.get("sync_name") or "sqlite_to_supabase").strip()[:100]
        manager = SyncManager(
            sqlite_path=_admin_sqlite_path(),
            sync_name=sync_name or "sqlite_to_supabase",
            max_batches=max_batches,
            dry_run=dry_run,
        )
        try:
            with process_lock():
                result = manager.run_once()
        except SyncAlreadyRunningError as exc:
            return _error(str(exc), status_code=409)
        _record_admin_sync_run(result)
        logger.warning(
            "Admin sync requested: dry_run=%s max_batches=%s sync_name=%s",
            dry_run, max_batches, sync_name,
        )
        if not result.get("ok"):
            return _error(
                "Lượt sync chưa thành công.",
                status_code=502,
                details=result,
            )
        return _success(result, message="Dry-run hoàn tất." if dry_run else "Lượt sync đã kết thúc.")
    except Exception as exc:
        return _exception_response("Không thể chạy sync.", exc)


@footprint_bp.get("/validation/sqlite")
@_require_control_permission
def footprint_admin_validate_sqlite():
    """Read-only validation of SQLite footprint_trades."""
    try:
        from scripts.footprint.data_validator import validate_sqlite
        limit_raw = request.args.get("limit")
        limit = None if limit_raw in (None, "", "all") else _safe_int(limit_raw, 10000, minimum=1, maximum=250000)
        strict = _parse_bool(request.args.get("strict_symbol_list"), default=False)
        report = validate_sqlite(
            sqlite_path=_admin_sqlite_path(),
            limit=limit,
            batch_size=1000,
            strict_symbol_list=strict,
        )
        return _success(report, message="Đã kiểm tra dữ liệu SQLite.")
    except Exception as exc:
        return _exception_response("Không thể chạy data validator.", exc)


@footprint_bp.get("/retention/inspect")
@_require_control_permission
def footprint_admin_retention_inspect():
    """Read-only SQLite retention inspection."""
    try:
        from scripts.footprint.retention import FootprintRetentionManager
        days = _safe_int(request.args.get("days"), 90, minimum=1, maximum=3650)
        symbols_raw = request.args.get("symbols", "").strip()
        symbols = sorted({v.strip().upper() for v in symbols_raw.split(",") if v.strip()}) or None
        manager = FootprintRetentionManager(
            sqlite_path=_admin_sqlite_path(),
            retention_days=days,
            symbols=symbols,
        )
        from dataclasses import asdict
        inspection = manager.inspect()
        plan = asdict(manager.build_plan()) if inspection.exists else None
        return _success({
            "inspection": asdict(inspection),
            "plan": plan,
            "checked_at": _utc_now(),
        })
    except Exception as exc:
        return _exception_response("Không thể kiểm tra retention.", exc)


@footprint_bp.get("/retention/preview")
@_require_control_permission
def footprint_admin_retention_preview():
    """Dry-run only; never deletes records."""
    try:
        from scripts.footprint.retention import FootprintRetentionManager
        from dataclasses import asdict
        days = _safe_int(request.args.get("days"), 90, minimum=1, maximum=3650)
        symbols_raw = request.args.get("symbols", "").strip()
        symbols = sorted({v.strip().upper() for v in symbols_raw.split(",") if v.strip()}) or None
        manager = FootprintRetentionManager(
            sqlite_path=_admin_sqlite_path(),
            retention_days=days,
            symbols=symbols,
        )
        inspection = manager.inspect()
        if not inspection.exists:
            return _success({
                "ok": False,
                "error": "SQLite hoặc bảng footprint_trades chưa tồn tại.",
                "rows_to_delete": 0,
                "rows_to_keep": 0,
                "dry_run": True,
                "deletion_enabled": False,
            }, message="Chưa có database để preview.")
        plan = asdict(manager.build_plan())
        plan["dry_run"] = True
        plan["deletion_enabled"] = False
        return _success(plan, message="Preview retention — không có dữ liệu nào bị xóa.")
    except Exception as exc:
        return _exception_response("Không thể tạo preview retention.", exc)


@footprint_bp.post("/retention/execute")
@_require_control_permission
def footprint_admin_retention_execute():
    """
    Destructive operation requires explicit confirmation. Backup is mandatory.
    No API path can disable the backup through this endpoint.
    """
    body = _json_body()
    if str(body.get("confirm") or "") != "XOA DU LIEU FOOTPRINT":
        return _error(
            "Thiếu xác nhận thao tác.",
            status_code=400,
            details="Gửi confirm='XOA DU LIEU FOOTPRINT' sau khi đã xem preview.",
        )
    try:
        from scripts.footprint.retention import FootprintRetentionManager
        from dataclasses import asdict
        days = _safe_int(body.get("days"), 90, minimum=1, maximum=3650)
        symbols_raw = str(body.get("symbols") or "").strip()
        symbols = sorted({v.strip().upper() for v in symbols_raw.split(",") if v.strip()}) or None
        manager = FootprintRetentionManager(
            sqlite_path=_admin_sqlite_path(),
            retention_days=days,
            symbols=symbols,
        )
        # Re-check the plan immediately before deletion, always create backup,
        # and never expose a "no backup" option in the HTTP API.
        plan = manager.build_plan()
        if plan.rows_to_delete <= 0:
            return _success({"plan": asdict(plan), "deleted": 0}, message="Không có bản ghi cần xóa.")
        result = manager.execute(dry_run=False, create_backup=True, vacuum=False)
        logger.warning(
            "ADMIN RETENTION EXECUTED: days=%s symbols=%s deleted=%s backup=%s",
            days, symbols or "ALL", result.rows_deleted, result.backup_path,
        )
        return _success(asdict(result), message="Retention đã hoàn tất.")
    except Exception as exc:
        return _exception_response("Retention không hoàn tất; kiểm tra backup và log backend.", exc)
