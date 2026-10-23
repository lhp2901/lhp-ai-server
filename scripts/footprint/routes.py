"""
DNSE Footprint Flask Routes
===========================

Blueprint API cho hệ thống DNSE Footprint.

URL prefix:
    /api/footprint

Endpoints:
    GET  /api/footprint/health
    GET  /api/footprint/symbols
    GET  /api/footprint/stream/status
    POST /api/footprint/stream/start
    POST /api/footprint/stream/stop
    GET  /api/footprint/<symbol>
    GET  /api/footprint/<symbol>/trades
    GET  /api/footprint/<symbol>/candles
    POST /api/footprint/<symbol>/test-tick

Lưu ý:
- Dữ liệu hiện được giữ trong RAM bởi FootprintEngine.
- Việc có dữ liệu live phụ thuộc vào dnse_stream.py và SDK DNSE.
- Không công khai endpoint điều khiển stream hoặc test-tick
  nếu chưa có cơ chế xác thực phù hợp.
"""

from __future__ import annotations

import logging
import os
import time

from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable

from flask import (
    Blueprint,
    current_app,
    jsonify,
    request,
)

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

logger = logging.getLogger("lhp_ai_server.footprint.routes")


# ============================================================
# 2. BLUEPRINT
# ============================================================

footprint_bp = Blueprint(
    "footprint",
    __name__,
    url_prefix="/api/footprint",
)


# ============================================================
# 3. STREAM MANAGER
# ============================================================

# Giữ tham chiếu stream dùng chung.
# Import lazy để không làm hỏng app.py nếu SDK DNSE chưa cài
# hoặc chưa cấu hình xong.
#
# dnse_stream.py cần cung cấp đối tượng stream có:
#   start()
#   stop()
#   get_status()
#
# Nếu file dnse_stream.py hiện tại dùng tên khác, cần đồng bộ
# tên đối tượng ở hàm _get_stream() bên dưới.

_stream_instance = None
_stream_import_error = None


def _get_stream():
    """
    Lấy stream singleton từ dnse_stream.py.

    Không kết nối DNSE tại thời điểm import routes.py.
    """
    global _stream_instance, _stream_import_error

    if _stream_instance is not None:
        return _stream_instance

    try:
        from scripts.footprint.dnse_stream import stream

        _stream_instance = stream
        _stream_import_error = None

        return _stream_instance

    except Exception as exc:
        _stream_import_error = str(exc)

        logger.exception(
            "Không import được DNSE footprint stream."
        )

        return None


# ============================================================
# 4. RESPONSE HELPERS
# ============================================================

def _utc_now() -> str:
    """Thời gian UTC hiện tại theo ISO 8601."""
    return datetime.now(timezone.utc).isoformat()


def _success(
    data: Any = None,
    *,
    message: str = "OK",
    status_code: int = 200,
):
    """Tạo JSON response thành công."""
    payload = {
        "success": True,
        "message": message,
        "timestamp": _utc_now(),
    }

    if data is not None:
        payload["data"] = data

    return jsonify(payload), status_code


def _error(
    message: str,
    *,
    status_code: int = 400,
    details: Any = None,
):
    """Tạo JSON response lỗi."""
    payload = {
        "success": False,
        "error": message,
        "timestamp": _utc_now(),
    }

    if details is not None:
        payload["details"] = details

    return jsonify(payload), status_code


def _safe_int(
    value: Any,
    default: int,
    *,
    minimum: int = 1,
    maximum: int = 1000,
) -> int:
    """Chuyển query parameter thành int có giới hạn."""
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default

    return max(minimum, min(result, maximum))


def _is_production() -> bool:
    """Xác định môi trường production."""
    return (
        os.getenv("FLASK_ENV", "").lower() == "production"
        or os.getenv("APP_ENV", "").lower() == "production"
        or os.getenv("ENVIRONMENT", "").lower() == "production"
    )


def _json_body() -> dict[str, Any]:
    """Đọc JSON body an toàn."""
    body = request.get_json(silent=True)

    if isinstance(body, dict):
        return body

    return {}


# ============================================================
# 5. KIỂM TRA QUYỀN ĐIỀU KHIỂN STREAM
# ============================================================

def _control_authorized() -> bool:
    """
    Kiểm tra quyền điều khiển stream.

    Nếu FOOTPRINT_CONTROL_TOKEN được cấu hình:
        Client phải gửi header:
            X-Footprint-Token: <token>

    Nếu chưa cấu hình token:
        - Cho phép điều khiển trong môi trường local.
        - Từ chối điều khiển trong production.

    Đây chỉ là lớp bảo vệ đơn giản cho API nội bộ.
    Với website công khai, nên xác thực qua backend/session
    và kiểm tra quyền người dùng.
    """
    expected_token = os.getenv(
        "FOOTPRINT_CONTROL_TOKEN",
        "",
    ).strip()

    if expected_token:
        supplied_token = request.headers.get(
            "X-Footprint-Token",
            "",
        ).strip()

        return supplied_token == expected_token

    if _is_production():
        return False

    return True


def _require_control_permission(
    function: Callable,
) -> Callable:
    """Decorator bảo vệ các endpoint điều khiển stream."""
    @wraps(function)
    def wrapper(*args, **kwargs):
        if not _control_authorized():
            return _error(
                "Không có quyền điều khiển Footprint stream.",
                status_code=403,
                details=(
                    "Hãy cấu hình FOOTPRINT_CONTROL_TOKEN "
                    "hoặc tích hợp xác thực backend phù hợp."
                ),
            )

        return function(*args, **kwargs)

    return wrapper


# ============================================================
# 6. HEALTH CHECK
# ============================================================

@footprint_bp.get("/health")
def footprint_health():
    """
    GET /api/footprint/health

    Kiểm tra:
    - Flask Blueprint hoạt động.
    - Engine đã được import.
    - Cấu hình cơ bản có hợp lệ hay không.

    Endpoint này không chứng minh DNSE đang stream live.
    """
    try:
        errors = validate_config(
            require_credentials=False,
        )

        engine_status = footprint_engine.get_status()

        return _success(
            {
                "service": "DNSE Footprint",
                "engine_available": True,
                "config_valid": len(errors) == 0,
                "config_errors": errors,
                "stream": _stream_status_payload(),
                "engine": engine_status,
            },
            message="Footprint health check completed",
        )

    except Exception as exc:
        logger.exception("Footprint health check thất bại.")

        return _error(
            "Footprint health check thất bại.",
            status_code=500,
            details=str(exc),
        )


# ============================================================
# 7. DANH SÁCH SYMBOL
# ============================================================

@footprint_bp.get("/symbols")
def footprint_symbols():
    """
    GET /api/footprint/symbols

    Trả về:
    - Các mã được cấu hình.
    - Các mã đã được engine đăng ký.
    """
    try:
        configured_symbols = list(FOOTPRINT_SYMBOLS)
        engine_symbols = footprint_engine.get_symbols()

        # Đăng ký các mã cấu hình nếu chưa tồn tại.
        footprint_engine.register_symbols(configured_symbols)

        engine_symbols = footprint_engine.get_symbols()

        return _success(
            {
                "configured_symbols": configured_symbols,
                "engine_symbols": engine_symbols,
                "count": len(engine_symbols),
            },
            message="Lấy danh sách mã thành công",
        )

    except Exception as exc:
        logger.exception("Không lấy được danh sách symbol.")

        return _error(
            "Không lấy được danh sách mã cổ phiếu.",
            status_code=500,
            details=str(exc),
        )


# ============================================================
# 8. STREAM STATUS
# ============================================================

def _stream_status_payload() -> dict[str, Any]:
    """
    Lấy trạng thái stream mà không làm endpoint bị lỗi
    nếu dnse_stream.py chưa sẵn sàng.
    """
    stream_instance = _get_stream()

    if stream_instance is None:
        return {
            "available": False,
            "running": False,
            "error": _stream_import_error,
        }

    try:
        if hasattr(stream_instance, "get_status"):
            status = stream_instance.get_status()

            if isinstance(status, dict):
                return {
                    "available": True,
                    **status,
                }

            return {
                "available": True,
                "status": str(status),
            }

        if hasattr(stream_instance, "is_running"):
            running = stream_instance.is_running()

            return {
                "available": True,
                "running": bool(running),
            }

        return {
            "available": True,
            "running": None,
            "message": (
                "Stream không có get_status() hoặc is_running()."
            ),
        }

    except Exception as exc:
        logger.exception("Không lấy được trạng thái DNSE stream.")

        return {
            "available": True,
            "running": False,
            "error": str(exc),
        }


@footprint_bp.get("/stream/status")
def footprint_stream_status():
    """
    GET /api/footprint/stream/status

    Trả về trạng thái kết nối DNSE và thống kê engine.
    """
    try:
        return _success(
            {
                "stream": _stream_status_payload(),
                "engine": footprint_engine.get_status(),
            },
            message="Lấy trạng thái stream thành công",
        )

    except Exception as exc:
        logger.exception("Lỗi đọc trạng thái stream.")

        return _error(
            "Không lấy được trạng thái stream.",
            status_code=500,
            details=str(exc),
        )


# ============================================================
# 9. START STREAM
# ============================================================

@footprint_bp.post("/stream/start")
@_require_control_permission
def footprint_stream_start():
    """
    POST /api/footprint/stream/start

    Yêu cầu dnse_stream.py có phương thức start().

    Endpoint này yêu cầu FOOTPRINT_ENABLE_STREAM_CONTROL=true.
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
                details=_stream_import_error,
            )

        if not hasattr(stream_instance, "start"):
            return _error(
                "dnse_stream.py chưa cung cấp phương thức start().",
                status_code=500,
            )

        result = stream_instance.start()

        return _success(
            {
                "start_result": result,
                "stream": _stream_status_payload(),
            },
            message="Đã gửi yêu cầu khởi động DNSE stream",
        )

    except Exception as exc:
        logger.exception("Không khởi động được DNSE stream.")

        return _error(
            "Không khởi động được DNSE stream.",
            status_code=500,
            details=str(exc),
        )


# ============================================================
# 10. STOP STREAM
# ============================================================

@footprint_bp.post("/stream/stop")
@_require_control_permission
def footprint_stream_stop():
    """
    POST /api/footprint/stream/stop

    Yêu cầu dnse_stream.py có phương thức stop().
    """
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
                details=_stream_import_error,
            )

        if not hasattr(stream_instance, "stop"):
            return _error(
                "dnse_stream.py chưa cung cấp phương thức stop().",
                status_code=500,
            )

        result = stream_instance.stop()

        return _success(
            {
                "stop_result": result,
                "stream": _stream_status_payload(),
            },
            message="Đã gửi yêu cầu dừng DNSE stream",
        )

    except Exception as exc:
        logger.exception("Không dừng được DNSE stream.")

        return _error(
            "Không dừng được DNSE stream.",
            status_code=500,
            details=str(exc),
        )


# ============================================================
# 11. FOOTPRINT THEO MÃ
# ============================================================

@footprint_bp.get("/<string:symbol>")
def footprint_by_symbol(symbol: str):
    """
    GET /api/footprint/DXG

    Query parameters:
        sort=price|volume|delta
        descending=true|false

    Ví dụ:
        /api/footprint/DXG?sort=price
        /api/footprint/DXG?sort=volume&descending=true
    """
    try:
        sort = request.args.get(
            "sort",
            "price",
        ).strip().lower()

        if sort not in {"price", "volume", "delta"}:
            return _error(
                "Tham số sort không hợp lệ.",
                status_code=400,
                details={
                    "allowed_values": [
                        "price",
                        "volume",
                        "delta",
                    ]
                },
            )

        descending = (
            request.args.get(
                "descending",
                "false",
            ).strip().lower()
            in {"1", "true", "yes", "on"}
        )

        result = footprint_engine.get_footprint(
            symbol,
            sort=sort,
            descending=descending,
        )

        if not result.get("success"):
            return _error(
                result.get(
                    "error",
                    "Không lấy được dữ liệu Footprint.",
                ),
                status_code=400,
            )

        return _success(
            result,
            message=f"Lấy Footprint {symbol.upper()} thành công",
        )

    except Exception as exc:
        logger.exception(
            "Lỗi lấy Footprint cho %s",
            symbol,
        )

        return _error(
            "Lỗi khi lấy dữ liệu Footprint.",
            status_code=500,
            details=str(exc),
        )


# ============================================================
# 12. TRADE TAPE
# ============================================================

@footprint_bp.get("/<string:symbol>/trades")
def footprint_trades(symbol: str):
    """
    GET /api/footprint/DXG/trades

    Query parameters:
        limit=100
        newest_first=true

    Ví dụ:
        /api/footprint/DXG/trades?limit=50
    """
    try:
        limit = _safe_int(
            request.args.get("limit"),
            default=100,
            minimum=1,
            maximum=5000,
        )

        newest_first = (
            request.args.get(
                "newest_first",
                "true",
            ).strip().lower()
            not in {"0", "false", "no", "off"}
        )

        result = footprint_engine.get_trades(
            symbol,
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
            message=f"Lấy giao dịch {symbol.upper()} thành công",
        )

    except Exception as exc:
        logger.exception(
            "Lỗi lấy trade tape cho %s",
            symbol,
        )

        return _error(
            "Lỗi khi lấy danh sách giao dịch.",
            status_code=500,
            details=str(exc),
        )


# ============================================================
# 13. CANDLES
# ============================================================

@footprint_bp.get("/<string:symbol>/candles")
def footprint_candles(symbol: str):
    """
    GET /api/footprint/DXG/candles

    Query parameters:
        limit=100
        newest_first=false

    Ví dụ:
        /api/footprint/DXG/candles?limit=100
    """
    try:
        limit = _safe_int(
            request.args.get("limit"),
            default=100,
            minimum=1,
            maximum=5000,
        )

        newest_first = (
            request.args.get(
                "newest_first",
                "false",
            ).strip().lower()
            in {"1", "true", "yes", "on"}
        )

        result = footprint_engine.get_candles(
            symbol,
            limit=limit,
            newest_first=newest_first,
        )

        if not result.get("success"):
            return _error(
                result.get(
                    "error",
                    "Không lấy được nến.",
                ),
                status_code=400,
            )

        return _success(
            result,
            message=f"Lấy nến {symbol.upper()} thành công",
        )

    except Exception as exc:
        logger.exception(
            "Lỗi lấy candles cho %s",
            symbol,
        )

        return _error(
            "Lỗi khi lấy dữ liệu nến.",
            status_code=500,
            details=str(exc),
        )


# ============================================================
# 14. TEST TICK — CHỈ DÙNG DEVELOPMENT
# ============================================================

@footprint_bp.post("/<string:symbol>/test-tick")
@_require_control_permission
def footprint_test_tick(symbol: str):
    """
    POST /api/footprint/DXG/test-tick

    Endpoint chỉ dành cho kiểm tra engine.

    Phải bật:
        FOOTPRINT_ENABLE_TEST_TICK=true

    Ví dụ JSON:
        {
            "price": 15.25,
            "quantity": 1000,
            "side": "BUY",
            "timestamp": 1792718100,
            "trade_id": "local-test-001"
        }

    KHÔNG sử dụng endpoint này để đưa dữ liệu giả vào
    hệ thống phân tích live trên production.
    """
    if not FOOTPRINT_ENABLE_TEST_TICK:
        return _error(
            "Endpoint test-tick đang bị tắt.",
            status_code=403,
            details=(
                "Chỉ bật FOOTPRINT_ENABLE_TEST_TICK=true "
                "trong môi trường phát triển."
            ),
        )

    if _is_production():
        return _error(
            "Không cho phép test-tick trên production.",
            status_code=403,
        )

    try:
        body = _json_body()

        price = body.get("price")
        quantity = body.get(
            "quantity",
            body.get("volume"),
        )

        if price is None or quantity is None:
            return _error(
                "Thiếu price hoặc quantity.",
                status_code=400,
            )

        trade = {
            "symbol": symbol.upper(),
            "price": price,
            "quantity": quantity,
            "side": body.get("side"),
            "timestamp": body.get(
                "timestamp",
                time.time(),
            ),
            "trade_id": body.get("trade_id"),
        }

        accepted = footprint_engine.add_trade(trade)

        if not accepted:
            return _error(
                "Engine từ chối giao dịch thử nghiệm. "
                "Kiểm tra dữ liệu hoặc trade_id bị trùng.",
                status_code=400,
            )

        return _success(
            {
                "accepted": True,
                "trade": trade,
                "footprint": footprint_engine.get_footprint(
                    symbol,
                ),
            },
            message="Đã thêm giao dịch thử nghiệm",
            status_code=201,
        )

    except Exception as exc:
        logger.exception(
            "Lỗi test-tick cho %s",
            symbol,
        )

        return _error(
            "Không thêm được giao dịch thử nghiệm.",
            status_code=500,
            details=str(exc),
        )


# ============================================================
# 15. ROUTE TỔNG QUAN
# ============================================================

@footprint_bp.get("/")
def footprint_index():
    """
    GET /api/footprint/

    Trả về thông tin các endpoint hiện có.
    """
    return _success(
        {
            "service": "DNSE Footprint API",
            "version": "1.0",
            "endpoints": {
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
                "test_tick": (
                    "POST /api/footprint/<symbol>/test-tick"
                ),
            },
            "storage": "RAM",
        },
        message="DNSE Footprint API is available",
    )


# ============================================================
# 16. AUTOSTART STREAM — TÙY CHỌN
# ============================================================

def start_stream_if_enabled() -> bool:
    """
    Có thể gọi từ app.py sau khi đăng ký Blueprint.

    Không tự chạy khi import routes.py, nhằm tránh khởi động
    stream nhiều lần do Flask debug reloader hoặc worker.

    Trả về:
        True nếu đã gửi yêu cầu start.
        False nếu autostart bị tắt hoặc không thể start.
    """
    if not DNSE_FOOTPRINT_AUTOSTART:
        logger.info(
            "DNSE Footprint autostart đang tắt."
        )
        return False

    errors = validate_config(
        require_credentials=True,
    )

    if errors:
        logger.error(
            "Không autostart Footprint vì cấu hình chưa hợp lệ: %s",
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

        return True

    except Exception:
        logger.exception(
            "Autostart DNSE Footprint thất bại."
        )
        return False


# ============================================================
# 17. ERROR HANDLERS CỦA BLUEPRINT
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
        "HTTP method không được hỗ trợ cho endpoint này.",
        status_code=405,
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
