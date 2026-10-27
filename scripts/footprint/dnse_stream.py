"""
DNSE REALTIME FOOTPRINT STREAM
==============================

File:
    scripts/footprint/dnse_stream.py

Data flow:
    DNSE WebSocket
        -> Normalize trade
        -> Validate symbol
        -> Persist to SQLite
        -> Update footprint_engine
        -> Flask API / Next.js

Features:
    - Background asyncio event loop.
    - DNSE WebSocket reconnect with exponential backoff and jitter.
    - Flexible DNSE payload normalization.
    - Timestamp normalization to Unix seconds.
    - SQLite persistence before Engine update.
    - Persistence and Engine metrics.
    - Thread-safe status reporting.
    - Graceful stop.
    - Compatible with the existing Flask routes.

Important:
    - Trade side is UNKNOWN unless reliably identified.
    - SQLite persistence does not automatically restore Engine RAM.
    - Trades without provider trade IDs cannot always be deduplicated safely.
    - Historical recovery requires Engine-compatible replay logic.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import random
import threading
import time

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional


# ============================================================
# 1. LOGGING
# ============================================================

logger = logging.getLogger(__name__)

if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | DNSE-FOOTPRINT | %(levelname)s | %(message)s"
        )
    )
    logger.addHandler(handler)

logger.setLevel(logging.INFO)


# ============================================================
# 2. PROJECT IMPORTS
# ============================================================

from scripts.footprint import config as cfg
from scripts.footprint.engine import footprint_engine


# ============================================================
# 3. CONFIGURATION HELPERS
# ============================================================

def _config_value(name: str, default: Any = None) -> Any:
    return getattr(cfg, name, default)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default

    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return value != 0

    text = str(value).strip().lower()

    if text in {"1", "true", "yes", "y", "on"}:
        return True

    if text in {"0", "false", "no", "n", "off"}:
        return False

    return default


def _as_float(value: Any, default: float) -> float:
    try:
        result = float(value)

        if not math.isfinite(result):
            return default

        return result

    except (TypeError, ValueError, OverflowError):
        return default


def _parse_symbols(value: Any) -> list[str]:
    if isinstance(value, str):
        values = value.split(",")

    elif isinstance(value, (list, tuple, set)):
        values = list(value)

    else:
        values = []

    result: list[str] = []
    seen: set[str] = set()

    for item in values:
        symbol = str(item or "").strip().upper()

        if not symbol or symbol in seen:
            continue

        seen.add(symbol)
        result.append(symbol)

    return result


# ============================================================
# 4. DNSE CONFIGURATION
# ============================================================

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
    ) or ""
).strip().rstrip("/")

DNSE_AUTO_RECONNECT = _as_bool(
    _config_value("DNSE_AUTO_RECONNECT", True),
    True,
)

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

FOOTPRINT_BOARD_ID = str(
    _config_value("FOOTPRINT_BOARD_ID", "G1") or "G1"
).strip()

FOOTPRINT_SYMBOLS = _parse_symbols(
    _config_value(
        "FOOTPRINT_SYMBOLS",
        ["HPG", "FPT", "SSI", "DXG"],
    )
)

FOOTPRINT_LOG_EVERY_TRADE = _as_bool(
    _config_value("FOOTPRINT_LOG_EVERY_TRADE", False),
    False,
)

FOOTPRINT_REQUIRE_PERSISTENCE = _as_bool(
    _config_value("FOOTPRINT_REQUIRE_PERSISTENCE", True),
    True,
)

RECONNECT_JITTER_SECONDS = max(
    0.0,
    _as_float(
        _config_value("DNSE_RECONNECT_JITTER_SECONDS", 1.0),
        1.0,
    ),
)

# Chỉ dùng khi SDK thực sự hỗ trợ thuộc tính này.
DNSE_CLIENT_RUNNING_ATTRIBUTE = "_is_running"


# ============================================================
# 5. SQLITE STORE
# ============================================================

def _get_store() -> Any:
    """
    Resolve LocalStore lazily.

    Expected interface:
        get_local_store()
        store.save_trade(trade)
        store.get_storage_status()
    """
    from scripts.footprint.local_store import get_local_store

    return get_local_store()


def _persist_trade(trade: dict[str, Any]) -> dict[str, Any]:
    """
    Persist a normalized trade.

    Returns a structured result:
        {
            "success": bool,
            "inserted": bool | None,
            "duplicate": bool,
            "reason": str | None,
        }

    Compatibility:
        - True from LocalStore means inserted.
        - False is ambiguous in the existing LocalStore API.
        - None is not treated as success.
        - A dict can explicitly distinguish duplicate and insert.
    """
    store = _get_store()
    result = store.save_trade(trade)

    if isinstance(result, dict):
        duplicate = result.get("duplicate") is True
        inserted = result.get("inserted") is True
        success = (
            result.get("success") is True
            or inserted
            or duplicate
        )

        return {
            "success": success,
            "inserted": inserted,
            "duplicate": duplicate,
            "reason": result.get("reason"),
        }

    if result is True:
        return {
            "success": True,
            "inserted": True,
            "duplicate": False,
            "reason": None,
        }

    if result is False:
        # Existing LocalStore returns False for duplicate/rejected
        # inserts. It does not provide enough information to tell
        # a duplicate from a real storage failure.
        return {
            "success": False,
            "inserted": False,
            "duplicate": False,
            "reason": (
                "LocalStore returned False; duplicate and storage "
                "failure cannot be distinguished by this interface."
            ),
        }

    return {
        "success": False,
        "inserted": False,
        "duplicate": False,
        "reason": (
            f"Unexpected save_trade return type: "
            f"{type(result).__name__}"
        ),
    }


def _store_status() -> dict[str, Any]:
    try:
        store = _get_store()
        method = getattr(store, "get_storage_status", None)

        if callable(method):
            result = method()

            if isinstance(result, dict):
                return result

        return {
            "available": True,
            "healthy": None,
            "message": "SQLite store loaded; health not confirmed.",
        }

    except Exception as exc:
        return {
            "available": False,
            "healthy": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


# ============================================================
# 6. PAYLOAD HELPERS
# ============================================================

def _read_field(data: Any, *names: str) -> Any:
    if data is None:
        return None

    if isinstance(data, dict):
        for name in names:
            value = data.get(name)

            if value is not None:
                return value

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
                    value = _read_field(converted, *names)

                    if value is not None:
                        return value

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


def _first_nonempty(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue

        if isinstance(value, str) and not value.strip():
            continue

        return value

    return None


def _to_decimal(
    value: Any,
    *,
    default: Optional[Decimal] = None,
) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return default

    try:
        result = Decimal(str(value))

        if not result.is_finite():
            return default

        return result

    except (InvalidOperation, ValueError, TypeError):
        return default


# ============================================================
# 7. TIMESTAMP NORMALIZATION
# ============================================================

def _normalize_timestamp(value: Any) -> Optional[float]:
    """
    Normalize timestamp to Unix seconds.

    Supports:
        - datetime
        - Unix seconds
        - Unix milliseconds
        - Unix microseconds
        - ISO-8601 strings

    Invalid supplied timestamps return None.
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)

        result = value.timestamp()

        if 946684800 <= result <= 4102444800:
            return result

        return None

    if isinstance(value, (int, float, Decimal)):
        try:
            number = float(value)

        except (ValueError, TypeError, OverflowError):
            return None

        if not math.isfinite(number) or number <= 0:
            return None

        # Microseconds -> seconds.
        if number >= 100_000_000_000_000:
            number /= 1_000_000

        # Milliseconds -> seconds.
        elif number >= 100_000_000_000:
            number /= 1_000

        # Plausible range: 2000-01-01 to 2100-01-01.
        if not 946684800 <= number <= 4102444800:
            return None

        return number

    if isinstance(value, str):
        text = value.strip()

        if not text:
            return None

        try:
            return _normalize_timestamp(float(text))

        except ValueError:
            pass

        try:
            parsed = datetime.fromisoformat(
                text.replace("Z", "+00:00")
            )

            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)

            return _normalize_timestamp(parsed)

        except (ValueError, OverflowError):
            return None

    return None


# ============================================================
# 8. SIDE NORMALIZATION
# ============================================================

def _configured_side_values(name: str) -> set[str]:
    value = _config_value(name, [])

    if isinstance(value, str):
        value = value.split(",")

    if not isinstance(value, (list, tuple, set)):
        return set()

    return {
        str(item).strip().upper()
        for item in value
        if str(item).strip()
    }


def _normalize_side(raw_side: Any) -> str:
    if raw_side is None:
        return "UNKNOWN"

    text = str(raw_side).strip().upper()

    if not text:
        return "UNKNOWN"

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

    buy_values.update(
        _configured_side_values("FOOTPRINT_BUY_SIDE_VALUES")
    )

    sell_values.update(
        _configured_side_values("FOOTPRINT_SELL_SIDE_VALUES")
    )

    # Conflicting configuration must not produce a guessed side.
    if text in buy_values and text in sell_values:
        return "UNKNOWN"

    if text in buy_values:
        return "BUY"

    if text in sell_values:
        return "SELL"

    return "UNKNOWN"


# ============================================================
# 9. TRADE NORMALIZATION
# ============================================================

def normalize_dnse_trade(data: Any) -> Optional[dict[str, Any]]:
    """
    Convert a DNSE payload into a normalized trade.

    Output fields:
        symbol, price, quantity, volume, side, timestamp,
        trade_id, timestamp_source, received_at
    """
    if data is None:
        return None

    # Handle SDK wrappers.
    nested = _read_field(data, "payload", "data", "trade")

    if nested is not None and nested is not data:
        nested_trade = normalize_dnse_trade(nested)

        if nested_trade is not None:
            return nested_trade

    symbol_value = _first_nonempty(
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

    if symbol_value is None:
        return None

    symbol = str(symbol_value).strip().upper()

    if not symbol:
        return None

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
        return None

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
        return None

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

    if timestamp is None:
        # A missing timestamp may use receive time. A supplied but
        # invalid timestamp is rejected rather than silently replaced.
        if raw_timestamp is not None:
            return None

        timestamp = time.time()
        timestamp_source = "received"

    else:
        timestamp_source = "exchange"

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

    if not trade_id:
        trade_id = None

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

    received_at = time.time()

    return {
        "symbol": symbol,
        "price": float(price),
        "quantity": float(quantity),
        "volume": float(quantity),
        "side": side,
        "timestamp": timestamp,
        "trade_id": trade_id,
        "timestamp_source": timestamp_source,
        "received_at": received_at,
    }


# ============================================================
# 10. STREAM MANAGER
# ============================================================

class DNSEFootprintStream:
    """Manage one DNSE WebSocket connection in a background thread."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client: Any = None

        self._stop_event = threading.Event()

        self._started = False
        self._connected = False
        self._authenticated = False
        self._subscribed = False
        self._sdk_available = False

        self._last_error: Optional[str] = None

        self._last_message_at: Optional[float] = None
        self._last_trade_at: Optional[float] = None
        self._last_persisted_at: Optional[float] = None
        self._last_engine_update_at: Optional[float] = None

        self._started_at: Optional[float] = None
        self._connected_at: Optional[float] = None

        self._connect_count = 0
        self._disconnect_count = 0
        self._reconnect_count = 0

        self._received_count = 0
        self._accepted_count = 0
        self._rejected_count = 0
        self._duplicate_count = 0
        self._persistence_error_count = 0
        self._engine_error_count = 0
        self._unknown_side_count = 0

        self._persisted_count = 0
        self._engine_updated_count = 0

        self._current_symbols = list(FOOTPRINT_SYMBOLS)

    # --------------------------------------------------------
    # Start
    # --------------------------------------------------------

    def start(self) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                if self._started:
                    return True

                self._last_error = (
                    "Stream thread is still shutting down."
                )
                return False

            if not DNSE_API_KEY or not DNSE_API_SECRET:
                self._last_error = (
                    "Thiếu DNSE_API_KEY hoặc DNSE_API_SECRET."
                )
                logger.error(self._last_error)
                return False

            if not DNSE_WS_URL:
                self._last_error = "DNSE_WS_URL chưa được cấu hình."
                logger.error(self._last_error)
                return False

            if not FOOTPRINT_SYMBOLS:
                self._last_error = "FOOTPRINT_SYMBOLS đang rỗng."
                logger.error(self._last_error)
                return False

            self._stop_event.clear()

            self._started = True
            self._connected = False
            self._authenticated = False
            self._subscribed = False

            self._started_at = time.time()
            self._last_error = None
            self._current_symbols = list(FOOTPRINT_SYMBOLS)

            self._thread = threading.Thread(
                target=self._thread_main,
                name="dnse-footprint-stream",
                daemon=True,
            )

            self._thread.start()

            logger.info(
                "Đã khởi động DNSE stream: symbols=%s, board=%s",
                self._current_symbols,
                FOOTPRINT_BOARD_ID,
            )

            return True

    # --------------------------------------------------------
    # Stop
    # --------------------------------------------------------

    def stop(self, timeout: float = 15.0) -> bool:
        self._stop_event.set()

        with self._lock:
            loop = self._loop
            client = self._client
            thread = self._thread

        if (
            loop is not None
            and loop.is_running()
            and client is not None
        ):
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._disconnect_client(client),
                    loop,
                )

                try:
                    future.result(timeout=3.0)
                except Exception:
                    pass

            except Exception:
                logger.debug(
                    "Không gửi được lệnh disconnect tới DNSE client.",
                    exc_info=True,
                )

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
                self._subscribed = False
                return True

            self._last_error = (
                "Stop requested, but stream thread did not exit "
                "within the timeout."
            )

            return False

    # --------------------------------------------------------
    # Status
    # --------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            thread_alive = bool(
                self._thread and self._thread.is_alive()
            )

            result: dict[str, Any] = {
                "running": bool(self._started and thread_alive),
                "started": self._started,
                "connected": self._connected,
                "authenticated": self._authenticated,
                "subscribed": self._subscribed,
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
                "duplicate_count": self._duplicate_count,
                "persistence_error_count": (
                    self._persistence_error_count
                ),
                "engine_error_count": self._engine_error_count,
                "unknown_side_count": self._unknown_side_count,
                "persisted_count": self._persisted_count,
                "engine_updated_count": self._engine_updated_count,
                "last_message_at": self._last_message_at,
                "last_trade_at": self._last_trade_at,
                "last_persisted_at": self._last_persisted_at,
                "last_engine_update_at": self._last_engine_update_at,
                "last_error": self._last_error,
                "started_at": self._started_at,
                "connected_at": self._connected_at,
                "thread_alive": thread_alive,
                "persistence_required": FOOTPRINT_REQUIRE_PERSISTENCE,
            }

        # Query SQLite outside the stream lock.
        result["storage"] = _store_status()

        return result

    # --------------------------------------------------------
    # Thread entry
    # --------------------------------------------------------

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        with self._lock:
            self._loop = loop

        try:
            loop.run_until_complete(self._run_forever())

        except Exception as exc:
            self._record_error(
                f"Stream thread failed: {type(exc).__name__}: {exc}"
            )

            logger.exception("DNSE stream thread failed.")

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
                logger.debug(
                    "Error cleaning pending asyncio tasks.",
                    exc_info=True,
                )

            try:
                loop.close()
            except Exception:
                pass

            with self._lock:
                self._loop = None
                self._client = None
                self._connected = False
                self._authenticated = False
                self._subscribed = False
                self._started = False

            logger.info("DNSE stream thread đã kết thúc.")

    # --------------------------------------------------------
    # Reconnect loop
    # --------------------------------------------------------

    async def _run_forever(self) -> None:
        attempt = 0

        while not self._stop_event.is_set():
            client = None
            was_connected = False

            try:
                attempt += 1

                logger.info(
                    "Đang kết nối DNSE, lần thử #%s.",
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
                    self._subscribed = True
                    self._connected_at = time.time()
                    self._connect_count += 1

                    if attempt > 1:
                        self._reconnect_count += 1

                    self._last_error = None

                was_connected = True

                logger.info(
                    "DNSE subscription đã được thiết lập: %s",
                    self._current_symbols,
                )

                # The SDK callback is expected to deliver trades.
                # Monitor the client until stopped or disconnected.
                while not self._stop_event.is_set():
                    await asyncio.sleep(1)

                    is_running = getattr(
                        client,
                        DNSE_CLIENT_RUNNING_ATTRIBUTE,
                        None,
                    )

                    if is_running is False:
                        raise ConnectionError(
                            "DNSE SDK báo WebSocket đã dừng."
                        )

            except asyncio.CancelledError:
                raise

            except Exception as exc:
                self._record_error(
                    f"{type(exc).__name__}: {exc}"
                )

                logger.exception("DNSE connection/stream error.")

            finally:
                if client is not None:
                    try:
                        await self._disconnect_client(client)

                    except Exception:
                        logger.debug(
                            "Error disconnecting DNSE client.",
                            exc_info=True,
                        )

                with self._lock:
                    if was_connected:
                        self._disconnect_count += 1

                    self._connected = False
                    self._authenticated = False
                    self._subscribed = False

                    if self._client is client:
                        self._client = None

            if self._stop_event.is_set():
                break

            if not DNSE_AUTO_RECONNECT:
                logger.warning(
                    "DNSE_AUTO_RECONNECT=false; dừng vòng lặp."
                )
                break

            exponent = min(max(attempt - 1, 0), 8)

            delay = min(
                DNSE_RECONNECT_BASE_SECONDS * (2 ** exponent),
                DNSE_RECONNECT_MAX_SECONDS,
            )

            if RECONNECT_JITTER_SECONDS:
                delay += random.uniform(
                    0.0,
                    RECONNECT_JITTER_SECONDS,
                )

            logger.warning(
                "Thử kết nối lại sau %.2f giây.",
                delay,
            )

            await self._async_wait_or_stop(delay)

        with self._lock:
            self._connected = False
            self._authenticated = False
            self._subscribed = False

    # --------------------------------------------------------
    # Create and subscribe
    # --------------------------------------------------------

    async def _create_and_subscribe(self) -> Any:
        try:
            from dnse import TradingClient

            self._sdk_available = True

        except ImportError as exc:
            self._sdk_available = False

            raise RuntimeError(
                "Không import được TradingClient từ package dnse."
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
            connect_result = client.connect()

            if inspect.isawaitable(connect_result):
                await asyncio.wait_for(
                    connect_result,
                    timeout=DNSE_CONNECT_TIMEOUT_SECONDS,
                )

            subscribe_method = getattr(
                client,
                "subscribe_trade_extra",
                None,
            )

            if not callable(subscribe_method):
                raise RuntimeError(
                    "TradingClient không có subscribe_trade_extra()."
                )

            subscribe_result = subscribe_method(
                symbols=self._current_symbols,
                on_trade_extra=self._on_trade_extra,
                encoding="json",
                board_id=FOOTPRINT_BOARD_ID,
            )

            if inspect.isawaitable(subscribe_result):
                await asyncio.wait_for(
                    subscribe_result,
                    timeout=DNSE_CONNECT_TIMEOUT_SECONDS,
                )

            return client

        except Exception:
            try:
                await self._disconnect_client(client)
            except Exception:
                pass

            raise

    # --------------------------------------------------------
    # Disconnect
    # --------------------------------------------------------

    async def _disconnect_client(self, client: Any) -> None:
        disconnect_method = getattr(client, "disconnect", None)

        if not callable(disconnect_method):
            return

        result = disconnect_method()

        if inspect.isawaitable(result):
            try:
                await asyncio.wait_for(result, timeout=5.0)

            except asyncio.TimeoutError:
                logger.warning(
                    "Hết thời gian chờ khi đóng DNSE client."
                )

    # --------------------------------------------------------
    # Stop-aware wait
    # --------------------------------------------------------

    async def _async_wait_or_stop(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds

        while not self._stop_event.is_set():
            remaining = deadline - time.monotonic()

            if remaining <= 0:
                return

            await asyncio.sleep(min(0.25, remaining))

    # --------------------------------------------------------
    # Receive callback
    # --------------------------------------------------------

    def _on_trade_extra(self, data: Any) -> None:
        """
        Process one trade callback.

        Ordering:
            1. Count message.
            2. Normalize and validate.
            3. Persist to SQLite.
            4. Update Engine if allowed.
        """
        received_at = time.time()

        with self._lock:
            self._received_count += 1
            self._last_message_at = received_at

        try:
            normalized = normalize_dnse_trade(data)

            if normalized is None:
                with self._lock:
                    self._rejected_count += 1

                logger.debug(
                    "Payload không hợp lệ: %s",
                    _object_to_dict(data),
                )
                return

            symbol = normalized["symbol"]

            with self._lock:
                subscribed_symbols = set(self._current_symbols)

            if symbol not in subscribed_symbols:
                with self._lock:
                    self._rejected_count += 1

                logger.debug(
                    "Bỏ qua mã chưa đăng ký: %s",
                    symbol,
                )
                return

            # ------------------------------------------------
            # Persist before updating Engine.
            # ------------------------------------------------
            try:
                persistence = _persist_trade(normalized)

            except Exception as exc:
                persistence = {
                    "success": False,
                    "inserted": False,
                    "duplicate": False,
                    "reason": f"{type(exc).__name__}: {exc}",
                }

                with self._lock:
                    self._persistence_error_count += 1
                    self._last_error = (
                        "SQLite persistence error: "
                        f"{type(exc).__name__}: {exc}"
                    )

                logger.exception(
                    "Không lưu được giao dịch vào SQLite."
                )

            if persistence["duplicate"]:
                # This branch is available only if LocalStore
                # explicitly reports duplicates.
                with self._lock:
                    self._duplicate_count += 1

                return

            persisted = persistence["success"]

            if not persisted:
                if not persistence.get("reason", "").startswith(
                    "LocalStore returned False"
                ):
                    with self._lock:
                        self._persistence_error_count += 1

                if FOOTPRINT_REQUIRE_PERSISTENCE:
                    with self._lock:
                        self._rejected_count += 1

                    logger.error(
                        "Bỏ qua Engine vì SQLite chưa xác nhận lưu: "
                        "symbol=%s, trade_id=%s, reason=%s",
                        symbol,
                        normalized.get("trade_id"),
                        persistence.get("reason"),
                    )

                    return

                logger.warning(
                    "SQLite chưa xác nhận lưu; vẫn cập nhật Engine "
                    "vì FOOTPRINT_REQUIRE_PERSISTENCE=false."
                )

            else:
                with self._lock:
                    if persistence["inserted"]:
                        self._persisted_count += 1

                    self._last_persisted_at = time.time()

            # ------------------------------------------------
            # Update Engine.
            # ------------------------------------------------
            try:
                result = footprint_engine.add_trade(normalized)

            except Exception:
                with self._lock:
                    self._engine_error_count += 1
                    self._last_error = (
                        "Engine error while processing trade."
                    )

                logger.exception(
                    "Engine không xử lý được trade %s.",
                    normalized.get("trade_id"),
                )
                return

            # Respect explicit rejection/duplicate results.
            if result is False:
                with self._lock:
                    self._rejected_count += 1

                return

            if isinstance(result, dict):
                if result.get("duplicate") is True:
                    with self._lock:
                        self._duplicate_count += 1

                    return

                if result.get("accepted") is False:
                    with self._lock:
                        self._rejected_count += 1

                    return

            # Legacy Engine methods may return None. In that case
            # this means no explicit rejection was reported; it
            # does not prove deduplication or successful insertion.
            with self._lock:
                self._accepted_count += 1
                self._engine_updated_count += 1
                self._last_trade_at = received_at
                self._last_engine_update_at = time.time()

                if normalized["side"] == "UNKNOWN":
                    self._unknown_side_count += 1

            if FOOTPRINT_LOG_EVERY_TRADE:
                logger.info(
                    "TRADE | %s | price=%s | quantity=%s | "
                    "side=%s | id=%s | ts=%s | persisted=%s",
                    normalized["symbol"],
                    normalized["price"],
                    normalized["quantity"],
                    normalized["side"],
                    normalized["trade_id"],
                    normalized["timestamp"],
                    persisted,
                )

        except Exception:
            with self._lock:
                self._rejected_count += 1
                self._last_error = "Unexpected error in trade callback."

            logger.exception("Lỗi xử lý callback trade_extra.")

    # --------------------------------------------------------
    # Error recorder
    # --------------------------------------------------------

    def _record_error(self, message: str) -> None:
        with self._lock:
            self._last_error = str(message)
            self._connected = False
            self._authenticated = False
            self._subscribed = False

        logger.error("DNSE stream error: %s", message)


# ============================================================
# 11. SINGLETON AND SHORTCUTS
# ============================================================

stream = DNSEFootprintStream()


def start_stream() -> bool:
    return stream.start()


def stop_stream() -> bool:
    return stream.stop()


def get_stream_status() -> dict[str, Any]:
    return stream.get_status()

