"""
DNSE REALTIME FOOTPRINT STREAM
==============================

File:
    scripts/footprint/dnse_stream.py

Nhiệm vụ:
    1. Kết nối DNSE WebSocket bằng TradingClient.
    2. Xác thực bằng DNSE API Key và API Secret.
    3. Đăng ký nhận dữ liệu trade_extra.
    4. Chuẩn hóa dữ liệu giao dịch.
    5. Đưa dữ liệu vào footprint_engine.
    6. Tự kết nối lại khi gặp lỗi.
    7. Cung cấp start(), stop(), get_status() cho Flask routes.

Yêu cầu:
    - scripts/footprint/config.py
    - scripts/footprint/engine.py
    - DNSE OpenAPI Python SDK có TradingClient
    - Python 3.10+ được khuyến nghị

Lưu ý:
    Footprint Delta chỉ chính xác khi xác định được bên chủ động mua/bán
    từ dữ liệu đáng tin cậy. Nếu không xác định được, side = UNKNOWN.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from dnse import TradingClient
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional


# ============================================================
# 1. LOGGING
# ============================================================

logger = logging.getLogger(__name__)

if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | DNSE-FOOTPRINT | "
            "%(levelname)s | %(message)s"
        )
    )
    logger.addHandler(_handler)

logger.setLevel(logging.INFO)


# ============================================================
# 2. IMPORT CONFIG VÀ FOOTPRINT ENGINE
# ============================================================

from scripts.footprint import config as cfg

from scripts.footprint.engine import footprint_engine


# ============================================================
# 3. CONFIGURATION HELPERS
# ============================================================

def _config_value(name: str, default: Any = None) -> Any:
    """
    Đọc biến cấu hình bằng getattr để tránh lỗi import
    nếu config.py không định nghĩa một biến tùy chọn.
    """
    return getattr(cfg, name, default)


def _as_bool(value: Any, default: bool = False) -> bool:
    """
    Chuyển đổi giá trị cấu hình thành bool an toàn.
    """
    if value is None:
        return default

    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return value != 0

    normalized = str(value).strip().lower()

    if normalized in {"1", "true", "yes", "y", "on"}:
        return True

    if normalized in {"0", "false", "no", "n", "off"}:
        return False

    return default


def _as_float(value: Any, default: float) -> float:
    try:
        result = float(value)

        if not math.isfinite(result):
            return default

        return result

    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)

    except (TypeError, ValueError):
        return default


DNSE_API_KEY = str(
    _config_value("DNSE_API_KEY", "") or ""
).strip()

DNSE_API_SECRET = str(
    _config_value("DNSE_API_SECRET", "") or ""
).strip()

DNSE_WS_URL = str(
    _config_value(
        "DNSE_WS_URL",
        "wss://ws-openapi.dnse.com.vn",
    )
).strip().rstrip("/")

DNSE_RECONNECT_BASE_SECONDS = max(
    1.0,
    _as_float(
        _config_value("DNSE_RECONNECT_BASE_SECONDS", 3),
        3.0,
    ),
)

DNSE_RECONNECT_MAX_SECONDS = max(
    DNSE_RECONNECT_BASE_SECONDS,
    _as_float(
        _config_value("DNSE_RECONNECT_MAX_SECONDS", 60),
        60.0,
    ),
)

DNSE_CONNECT_TIMEOUT_SECONDS = max(
    5.0,
    _as_float(
        _config_value("DNSE_CONNECT_TIMEOUT_SECONDS", 30),
        30.0,
    ),
)

DNSE_AUTO_RECONNECT = _as_bool(
    _config_value("DNSE_AUTO_RECONNECT", True),
    True,
)

FOOTPRINT_BOARD_ID = str(
    _config_value("FOOTPRINT_BOARD_ID", "G1") or "G1"
).strip()

FOOTPRINT_SYMBOLS = _config_value(
    "FOOTPRINT_SYMBOLS",
    ["HPG", "FPT", "SSI", "DXG"],
)

if isinstance(FOOTPRINT_SYMBOLS, str):
    FOOTPRINT_SYMBOLS = [
        item.strip().upper()
        for item in FOOTPRINT_SYMBOLS.split(",")
        if item.strip()
    ]

else:
    FOOTPRINT_SYMBOLS = [
        str(item).strip().upper()
        for item in FOOTPRINT_SYMBOLS
        if str(item).strip()
    ]

# Loại bỏ mã trùng lặp nhưng vẫn giữ thứ tự ban đầu.
FOOTPRINT_SYMBOLS = list(dict.fromkeys(FOOTPRINT_SYMBOLS))

# Khoảng chờ giữa các lần thử kết nối.
RECONNECT_BASE = DNSE_RECONNECT_BASE_SECONDS
RECONNECT_MAX = DNSE_RECONNECT_MAX_SECONDS

# Chỉ bật nếu config.py có biến tương ứng.
FOOTPRINT_LOG_EVERY_TRADE = _as_bool(
    _config_value("FOOTPRINT_LOG_EVERY_TRADE", False),
    False,
)


# ============================================================
# 4. GENERIC DATA HELPERS
# ============================================================

def _read_field(data: Any, *names: str) -> Any:
    """
    Đọc field từ:
        - dict
        - object thông thường
        - Pydantic model
        - object có model_dump() hoặc dict()

    Trả về giá trị đầu tiên tìm thấy.
    """
    if data is None:
        return None

    if isinstance(data, dict):
        for name in names:
            if name in data and data[name] is not None:
                return data[name]

        # Hỗ trợ tên field không phân biệt chữ hoa/thường.
        lower_map = {
            str(key).lower(): value
            for key, value in data.items()
        }

        for name in names:
            value = lower_map.get(name.lower())

            if value is not None:
                return value

        return None

    for name in names:
        try:
            value = getattr(data, name)

            if value is not None:
                return value

        except (AttributeError, TypeError):
            continue

    for method_name in ("model_dump", "dict"):
        try:
            method = getattr(data, method_name, None)

            if callable(method):
                converted = method()

                if isinstance(converted, dict):
                    return _read_field(converted, *names)

        except Exception:
            continue

    try:
        values = vars(data)

        if isinstance(values, dict):
            return _read_field(values, *names)

    except (TypeError, AttributeError):
        pass

    return None


def _object_to_dict(data: Any) -> dict[str, Any]:
    """
    Chuyển object DNSE thành dict để log hoặc xử lý.

    Không dùng str(object) làm dữ liệu giao dịch vì
    kết quả đó không đảm bảo đúng cấu trúc.
    """
    if isinstance(data, dict):
        return dict(data)

    for method_name in ("model_dump", "dict"):
        try:
            method = getattr(data, method_name, None)

            if callable(method):
                result = method()

                if isinstance(result, dict):
                    return result

        except Exception:
            pass

    try:
        result = vars(data)

        if isinstance(result, dict):
            return dict(result)

    except (TypeError, AttributeError):
        pass

    return {}


def _to_decimal(
    value: Any,
    *,
    default: Optional[Decimal] = None,
) -> Optional[Decimal]:
    """
    Chuyển số thành Decimal.

    Không tự đổi đơn vị giá hoặc khối lượng tại đây.
    Việc chuyển đơn vị phải dựa trên tài liệu dữ liệu DNSE
    và cấu trúc mà engine.py đang sử dụng.
    """
    if value is None or isinstance(value, bool):
        return default

    try:
        result = Decimal(str(value))

        if not result.is_finite():
            return default

        return result

    except (InvalidOperation, ValueError, TypeError):
        return default


def _to_float(value: Any) -> Optional[float]:
    decimal_value = _to_decimal(value)

    if decimal_value is None:
        return None

    return float(decimal_value)


def _first_nonempty(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue

        if isinstance(value, str) and not value.strip():
            continue

        return value

    return None


# ============================================================
# 5. TIMESTAMP NORMALIZATION
# ============================================================

def _normalize_timestamp(value: Any) -> Any:
    """
    Chuẩn hóa timestamp về Unix seconds.

    Hỗ trợ:
        - Unix seconds
        - Unix milliseconds
        - Unix microseconds
        - datetime
        - ISO timestamp string

    Nếu không đọc được timestamp, dùng thời điểm hiện tại.

    Lưu ý:
        Giá trị timestamp từ SDK cần được kiểm tra theo
        tài liệu của phiên bản SDK đang cài đặt.
    """
    if value is None:
        return time.time()

    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)

        return value.timestamp()

    if isinstance(value, (int, float, Decimal)):
        number = float(value)

        if not math.isfinite(number):
            return time.time()

        # Phải kiểm tra microseconds trước milliseconds.
        if number >= 100_000_000_000_000:
            return number / 1_000_000

        if number >= 100_000_000_000:
            return number / 1_000

        return number

    if isinstance(value, str):
        text = value.strip()

        if not text:
            return time.time()

        # Chuỗi số: Unix timestamp.
        try:
            return _normalize_timestamp(float(text))

        except ValueError:
            pass

        # ISO 8601.
        try:
            normalized = text.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)

            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)

            return parsed.timestamp()

        except ValueError:
            pass

    return time.time()


# ============================================================
# 6. SIDE NORMALIZATION
# ============================================================

def _normalize_side(raw_side: Any) -> str:
    """
    Chuẩn hóa side thành BUY / SELL / UNKNOWN.

    Chỉ nhận diện side từ field có ý nghĩa rõ ràng.
    Không suy đoán side từ việc giá tăng/giảm.

    Nếu DNSE không cung cấp side xác nhận, kết quả là UNKNOWN.
    """
    if raw_side is None:
        return "UNKNOWN"

    text = str(raw_side).strip().upper()

    if not text:
        return "UNKNOWN"

    # Một số biểu diễn phổ biến.
    # Chỉ giữ các giá trị cần thiết và không diễn giải
    # các mã chưa xác minh trong payload DNSE.
    buy_values = {
        "BUY",
        "B",
        "BUYER",
        "ACTIVE_BUY",
        "AGGRESSIVE_BUY",
        "BUY_INITIATED",
    }

    sell_values = {
        "SELL",
        "S",
        "SELLER",
        "ACTIVE_SELL",
        "AGGRESSIVE_SELL",
        "SELL_INITIATED",
    }

    # Cho phép cấu hình bổ sung các mã đã được xác minh
    # từ tài liệu DNSE hoặc payload thực tế.
    configured_buy_values = _config_value(
        "FOOTPRINT_BUY_SIDE_VALUES",
        [],
    )

    configured_sell_values = _config_value(
        "FOOTPRINT_SELL_SIDE_VALUES",
        [],
    )

    if isinstance(configured_buy_values, str):
        configured_buy_values = configured_buy_values.split(",")

    if isinstance(configured_sell_values, str):
        configured_sell_values = configured_sell_values.split(",")

    buy_values.update(
        str(value).strip().upper()
        for value in configured_buy_values
        if str(value).strip()
    )

    sell_values.update(
        str(value).strip().upper()
        for value in configured_sell_values
        if str(value).strip()
    )

    if text in buy_values:
        return "BUY"

    if text in sell_values:
        return "SELL"

    return "UNKNOWN"


# ============================================================
# 7. DNSE PAYLOAD NORMALIZATION
# ============================================================

def normalize_dnse_trade(data: Any) -> Optional[dict[str, Any]]:
    """
    Chuẩn hóa một tick DNSE thành cấu trúc mà engine.py hỗ trợ.

    Output:
        {
            "symbol": "HPG",
            "price": 27000,
            "quantity": 100,
            "side": "BUY" | "SELL" | "UNKNOWN",
            "timestamp": 1234567890.0,
            "trade_id": "..."
        }

    Tên field bên dưới bao gồm các alias thường gặp.
    Tuy nhiên, cần xác minh tên field thực tế bằng payload
    từ phiên bản DNSE SDK đang chạy.

    Không coi các số liệu tổng hợp trong trade_extra là
    một giao dịch riêng lẻ nếu chúng không đại diện cho tick.
    """
    if data is None:
        return None

    # --------------------------------------------------------
    # 7.1. Symbol
    # --------------------------------------------------------

    symbol = _first_nonempty(
        _read_field(
            data,
            "symbol",
            "ticker",
            "code",
            "securityCode",
            "security_code",
            "instrument",
        )
    )

    if symbol is None:
        return None

    symbol = str(symbol).strip().upper()

    if not symbol:
        return None

    # --------------------------------------------------------
    # 7.2. Price
    # --------------------------------------------------------

    raw_price = _first_nonempty(
        _read_field(
            data,
            "matchPrice",
            "matchedPrice",
            "tradePrice",
            "trade_price",
            "price",
            "lastPrice",
            "last_price",
        )
    )

    price = _to_decimal(raw_price)

    if price is None or price <= 0:
        logger.debug(
            "Bỏ tick vì không có giá hợp lệ. symbol=%s",
            symbol,
        )
        return None

    # --------------------------------------------------------
    # 7.3. Quantity / volume
    # --------------------------------------------------------

    raw_quantity = _first_nonempty(
        _read_field(
            data,
            "matchedQuantity",
            "matchQuantity",
            "matchQty",
            "tradeQuantity",
            "trade_quantity",
            "quantity",
            "qty",
            "volume",
            "lastQuantity",
        )
    )

    quantity = _to_decimal(raw_quantity)

    if quantity is None or quantity <= 0:
        logger.debug(
            "Bỏ tick vì không có khối lượng hợp lệ. symbol=%s",
            symbol,
        )
        return None

    # --------------------------------------------------------
    # 7.4. Timestamp
    # --------------------------------------------------------

    raw_timestamp = _first_nonempty(
        _read_field(
            data,
            "timestamp",
            "tradeTime",
            "trade_time",
            "matchTime",
            "match_time",
            "transactTime",
            "transact_time",
            "receivedAt",
            "received_at",
            "time",
        )
    )

    timestamp = _normalize_timestamp(raw_timestamp)

    # --------------------------------------------------------
    # 7.5. Trade ID
    # --------------------------------------------------------

    raw_trade_id = _first_nonempty(
        _read_field(
            data,
            "tradeId",
            "trade_id",
            "matchId",
            "match_id",
            "executionId",
            "execution_id",
            "id",
        )
    )

    trade_id = (
        str(raw_trade_id).strip()
        if raw_trade_id is not None
        else None
    )

    # --------------------------------------------------------
    # 7.6. Side
    # --------------------------------------------------------

    # Chỉ đọc các field được đặt tên rõ ràng là side hoặc
    # aggressor/active side. Không sử dụng màu nến hoặc
    # biến động giá để đoán bên mua/bán.
    raw_side = _first_nonempty(
        _read_field(
            data,
            "aggressorSide",
            "aggressor_side",
            "activeSide",
            "active_side",
            "tradeSide",
            "trade_side",
            "side",
            "buySell",
            "buy_sell",
        )
    )

    side = _normalize_side(raw_side)

    # --------------------------------------------------------
    # 7.7. Normalized result
    # --------------------------------------------------------

    return {
        "symbol": symbol,
        "price": float(price),
        "quantity": float(quantity),
        "side": side,
        "timestamp": timestamp,
        "trade_id": trade_id,
    }


# ============================================================
# 8. DNSE STREAM MANAGER
# ============================================================

class DNSEFootprintStream:
    """
    Quản lý vòng đời DNSE WebSocket trong thread riêng.

    API công khai:
        start()
        stop()
        get_status()

    Singleton ở cuối file:
        stream
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()

        self._thread: Optional[threading.Thread] = None

        self._loop: Optional[asyncio.AbstractEventLoop] = None

        self._client: Any = None

        self._stop_event = threading.Event()

        self._started = False
        self._connected = False
        self._authenticated = False

        self._last_error: Optional[str] = None
        self._last_message_at: Optional[float] = None
        self._last_trade_at: Optional[float] = None

        self._connect_count = 0
        self._disconnect_count = 0
        self._reconnect_count = 0

        self._received_count = 0
        self._accepted_count = 0
        self._rejected_count = 0

        self._unknown_side_count = 0

        self._started_at: Optional[float] = None

        self._current_symbols: list[str] = list(
            FOOTPRINT_SYMBOLS
        )

        self._sdk_available = False

    # --------------------------------------------------------
    # 8.1. Public: start
    # --------------------------------------------------------

    def start(self) -> bool:
        """
        Khởi chạy stream.

        Không block Flask request.
        Nếu stream đã chạy thì không tạo thêm kết nối.
        """
        with self._lock:
            if (
                self._thread is not None
                and self._thread.is_alive()
                and self._started
            ):
                logger.info(
                    "DNSE Footprint stream đang chạy."
                )
                return True

            if not DNSE_API_KEY or not DNSE_API_SECRET:
                self._last_error = (
                    "Thiếu DNSE_API_KEY hoặc DNSE_API_SECRET "
                    "trong file môi trường/config."
                )

                logger.error(self._last_error)
                return False

            if not FOOTPRINT_SYMBOLS:
                self._last_error = (
                    "FOOTPRINT_SYMBOLS đang rỗng. "
                    "Hãy cấu hình danh sách mã cổ phiếu."
                )

                logger.error(self._last_error)
                return False

            self._stop_event.clear()

            self._started = True
            self._started_at = time.time()
            self._last_error = None

            self._current_symbols = list(
                FOOTPRINT_SYMBOLS
            )

            self._thread = threading.Thread(
                target=self._thread_main,
                name="dnse-footprint-stream",
                daemon=True,
            )

            self._thread.start()

            logger.info(
                "Đã yêu cầu khởi động DNSE stream. symbols=%s, board=%s",
                self._current_symbols,
                FOOTPRINT_BOARD_ID,
            )

            return True

    # --------------------------------------------------------
    # 8.2. Public: stop
    # --------------------------------------------------------

    def stop(self, timeout: float = 15.0) -> bool:
        """
        Yêu cầu dừng stream và đóng kết nối SDK.

        timeout chỉ giới hạn thời gian chờ thread kết thúc;
        nó không đảm bảo đóng socket ngay lập tức nếu SDK
        đang bị kẹt ở một thao tác mạng.
        """
        logger.info("Đang yêu cầu dừng DNSE stream...")

        self._stop_event.set()

        loop = self._loop
        client = self._client

        if loop is not None and loop.is_running():
            if client is not None:
                try:
                    asyncio.run_coroutine_threadsafe(
                        self._disconnect_client(client),
                        loop,
                    )
                except Exception as exc:
                    logger.warning(
                        "Không thể gửi lệnh disconnect: %s",
                        exc,
                    )

        thread = self._thread

        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=max(0.1, timeout))

        with self._lock:
            if thread is None or not thread.is_alive():
                self._started = False
                self._connected = False
                self._authenticated = False

                logger.info(
                    "DNSE stream đã dừng."
                )

                return True

            logger.warning(
                "DNSE stream chưa kết thúc sau %.1f giây.",
                timeout,
            )

            return False

    # --------------------------------------------------------
    # 8.3. Public: status
    # --------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        """
        Trả về trạng thái cho routes.py.

        Không trả API Key hoặc API Secret.
        """
        with self._lock:
            thread_alive = bool(
                self._thread and self._thread.is_alive()
            )

            return {
                "running": bool(
                    self._started and thread_alive
                ),
                "started": self._started,
                "connected": self._connected,
                "authenticated": self._authenticated,
                "sdk_available": self._sdk_available,
                "websocket_url": DNSE_WS_URL,
                "board_id": FOOTPRINT_BOARD_ID,
                "symbols": list(self._current_symbols),
                "symbol_count": len(self._current_symbols),
                "connect_count": self._connect_count,
                "disconnect_count": self._disconnect_count,
                "reconnect_count": self._reconnect_count,
                "received_count": self._received_count,
                "accepted_count": self._accepted_count,
                "rejected_count": self._rejected_count,
                "unknown_side_count": self._unknown_side_count,
                "last_message_at": self._last_message_at,
                "last_trade_at": self._last_trade_at,
                "last_error": self._last_error,
                "started_at": self._started_at,
                "thread_alive": thread_alive,
            }

    # --------------------------------------------------------
    # 8.4. Thread entry point
    # --------------------------------------------------------

    def _thread_main(self) -> None:
        """
        Tạo event loop riêng trong background thread.

        Flask tiếp tục xử lý API request bình thường.
        """
        loop = asyncio.new_event_loop()

        asyncio.set_event_loop(loop)

        with self._lock:
            self._loop = loop

        try:
            loop.run_until_complete(
                self._run_forever()
            )

        except Exception as exc:
            self._record_error(
                f"Lỗi nghiêm trọng trong stream thread: {exc}"
            )

            logger.exception(
                "DNSE stream thread gặp lỗi."
            )

        finally:
            try:
                pending = asyncio.all_tasks(loop)

                for task in pending:
                    task.cancel()

                if pending:
                    loop.run_until_complete(
                        asyncio.gather(
                            *pending,
                            return_exceptions=True,
                        )
                    )

            except Exception:
                pass

            try:
                loop.close()

            except Exception:
                pass

            with self._lock:
                self._loop = None
                self._client = None
                self._connected = False
                self._authenticated = False
                self._started = False

            logger.info(
                "DNSE stream thread đã kết thúc."
            )

    # --------------------------------------------------------
    # 8.5. Reconnection loop
    # --------------------------------------------------------

    async def _run_forever(self) -> None:
        """
        Vòng lặp kết nối.

        SDK có auto_reconnect riêng; vòng lặp ngoài này bổ sung
        khả năng khởi tạo lại client nếu phiên kết nối kết thúc
        hoặc quá trình connect/subscribe thất bại.
        """
        attempt = 0

        while not self._stop_event.is_set():
            client = None

            try:
                attempt += 1

                logger.info(
                    "Đang kết nối DNSE. Lần thử #%s",
                    attempt,
                )

                client = await self._create_and_subscribe()

                if client is None:
                    raise RuntimeError(
                        "Không tạo được DNSE WebSocket client."
                    )

                with self._lock:
                    self._client = client
                    self._connected = True
                    self._authenticated = True

                    self._connect_count += 1

                    if attempt > 1:
                        self._reconnect_count += 1

                    self._last_error = None

                logger.info(
                    "DNSE WebSocket đã kết nối và đăng ký dữ liệu. "
                    "symbols=%s, board=%s",
                    self._current_symbols,
                    FOOTPRINT_BOARD_ID,
                )

                # Duy trì client cho đến khi nhận yêu cầu dừng
                # hoặc SDK kết thúc kết nối.
                while not self._stop_event.is_set():
                    await asyncio.sleep(1)

                    # Kiểm tra trạng thái nội bộ nếu SDK cung cấp.
                    is_running = getattr(
                        client,
                        "_is_running",
                        None,
                    )

                    if is_running is False:
                        raise ConnectionError(
                            "DNSE SDK báo WebSocket không còn chạy."
                        )

            except asyncio.CancelledError:
                raise

            except Exception as exc:
                message = (
                    f"{type(exc).__name__}: {exc}"
                )

                self._record_error(message)

                logger.exception(
                    "DNSE connect/subscribe loop gặp lỗi."
                )

            finally:
                if client is not None:
                    try:
                        await self._disconnect_client(client)

                    except Exception as exc:
                        logger.warning(
                            "Lỗi khi đóng DNSE client: %s",
                            exc,
                        )

                with self._lock:
                    if self._connected:
                        self._disconnect_count += 1

                    self._connected = False
                    self._authenticated = False

                    if self._client is client:
                        self._client = None

            if self._stop_event.is_set():
                break

            if not DNSE_AUTO_RECONNECT:
                logger.warning(
                    "DNSE_AUTO_RECONNECT=false; không thử lại."
                )
                break

            delay = min(
                RECONNECT_BASE * (2 ** min(attempt - 1, 8)),
                RECONNECT_MAX,
            )

            logger.warning(
                "Thử kết nối lại DNSE sau %.1f giây.",
                delay,
            )

            # Dùng Event để có thể dừng ngay trong thời gian chờ.
            await self._async_wait_or_stop(delay)

        with self._lock:
            self._connected = False
            self._authenticated = False

    # --------------------------------------------------------
    # 8.6. Create client and subscribe
    # --------------------------------------------------------

    async def _create_and_subscribe(self) -> Any:
        """
        Khởi tạo DNSE TradingClient, connect và subscribe.

        Theo SDK DNSE:
            from dnse import TradingClient
            await client.connect()
            await client.subscribe_trade_extra(...)
        """
        try:
            from dnse import TradingClient

            self._sdk_available = True

        except ImportError as exc:
            self._sdk_available = False

            raise RuntimeError(
                "Không import được TradingClient từ package dnse. "
                "Hãy kiểm tra đã cài đúng DNSE OpenAPI SDK hay chưa."
            ) from exc

        client = TradingClient(
            api_key=DNSE_API_KEY,
            api_secret=DNSE_API_SECRET,
            base_url=DNSE_WS_URL,
            encoding="json",
            auto_reconnect=DNSE_AUTO_RECONNECT,
            max_retries=10,
            timeout=DNSE_CONNECT_TIMEOUT_SECONDS,
        )

        try:
            await asyncio.wait_for(
                client.connect(),
                timeout=DNSE_CONNECT_TIMEOUT_SECONDS,
            )

            # SDK connect() hoàn tất sau khi kết nối và xác thực.
            logger.info(
                "DNSE client.connect() hoàn tất."
            )

            subscribe_method = getattr(
                client,
                "subscribe_trade_extra",
                None,
            )

            if not callable(subscribe_method):
                raise RuntimeError(
                    "TradingClient không có subscribe_trade_extra(). "
                    "Kiểm tra phiên bản SDK đang cài đặt."
                )

            await subscribe_method(
                symbols=self._current_symbols,
                on_trade_extra=self._on_trade_extra,
                encoding="json",
                board_id=FOOTPRINT_BOARD_ID,
            )

            logger.info(
                "Đã gửi đăng ký trade_extra: symbols=%s, board=%s",
                self._current_symbols,
                FOOTPRINT_BOARD_ID,
            )

            return client

        except Exception:
            try:
                await self._disconnect_client(client)

            except Exception:
                pass

            raise

    # --------------------------------------------------------
    # 8.7. Disconnect helper
    # --------------------------------------------------------

    async def _disconnect_client(self, client: Any) -> None:
        """
        Đóng kết nối DNSE theo API SDK.

        Hàm disconnect có thể chưa tồn tại ở một phiên bản
        SDK khác, vì vậy kiểm tra trước khi gọi.
        """
        disconnect_method = getattr(
            client,
            "disconnect",
            None,
        )

        if not callable(disconnect_method):
            return

        result = disconnect_method()

        if asyncio.iscoroutine(result):
            try:
                await asyncio.wait_for(
                    result,
                    timeout=5.0,
                )

            except asyncio.TimeoutError:
                logger.warning(
                    "Hết thời gian chờ khi đóng DNSE client."
                )

    # --------------------------------------------------------
    # 8.8. Stop-aware async wait
    # --------------------------------------------------------

    async def _async_wait_or_stop(
        self,
        seconds: float,
    ) -> None:
        """
        Chờ tối đa seconds nhưng có thể thoát sớm khi stop().
        """
        deadline = time.monotonic() + seconds

        while not self._stop_event.is_set():
            remaining = deadline - time.monotonic()

            if remaining <= 0:
                return

            await asyncio.sleep(
                min(0.25, remaining)
            )

    # --------------------------------------------------------
    # 8.9. Receive callback
    # --------------------------------------------------------

    def _on_trade_extra(self, data: Any) -> None:
        """
        Callback nhận một thông điệp trade_extra từ DNSE SDK.

        Callback SDK có thể chạy trong thread event loop.
        Do đó:
            - Không chạy tác vụ nặng ở đây.
            - Không thực hiện request HTTP.
            - Chỉ chuẩn hóa và đưa tick vào engine.
        """
        with self._lock:
            self._received_count += 1
            self._last_message_at = time.time()

        try:
            normalized = normalize_dnse_trade(data)

            if normalized is None:
                with self._lock:
                    self._rejected_count += 1

                logger.debug(
                    "Không thể chuẩn hóa payload DNSE: %s",
                    _object_to_dict(data),
                )

                return

            symbol = normalized["symbol"]

            # Chỉ chấp nhận mã đang đăng ký.
            # Tránh trộn dữ liệu của mã khác vào footprint.
            if symbol not in self._current_symbols:
                logger.debug(
                    "Bỏ qua symbol không đăng ký: %s",
                    symbol,
                )
                return

            # Engine sẽ xử lý deduplication, phân loại side,
            # tổng hợp volume và xây dựng footprint.
            accepted = footprint_engine.add_trade(
                normalized
            )

            if accepted is False:
                with self._lock:
                    self._rejected_count += 1

                return

            with self._lock:
                self._accepted_count += 1
                self._last_trade_at = time.time()

                if normalized["side"] == "UNKNOWN":
                    self._unknown_side_count += 1

            if FOOTPRINT_LOG_EVERY_TRADE:
                logger.info(
                    "TICK | %s | price=%s | qty=%s | side=%s | id=%s",
                    normalized["symbol"],
                    normalized["price"],
                    normalized["quantity"],
                    normalized["side"],
                    normalized["trade_id"],
                )

        except Exception as exc:
            with self._lock:
                self._rejected_count += 1

            logger.exception(
                "Lỗi xử lý DNSE trade_extra callback: %s",
                exc,
            )

    # --------------------------------------------------------
    # 8.10. Error recorder
    # --------------------------------------------------------

    def _record_error(self, message: str) -> None:
        with self._lock:
            self._last_error = str(message)
            self._connected = False
            self._authenticated = False

        logger.error(
            "DNSE stream error: %s",
            message,
        )


# ============================================================
# 9. SINGLETON FOR routes.py
# ============================================================

stream = DNSEFootprintStream()


# ============================================================
# 10. OPTIONAL SHORTCUTS
# ============================================================

def start_stream() -> bool:
    """
    Hàm tiện ích nếu một module khác muốn khởi động stream.
    """
    return stream.start()


def stop_stream() -> bool:
    """
    Hàm tiện ích nếu một module khác muốn dừng stream.
    """
    return stream.stop()


def get_stream_status() -> dict[str, Any]:
    """
    Hàm tiện ích lấy trạng thái stream.
    """
    return stream.get_status()
