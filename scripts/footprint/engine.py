
"""
DNSE Footprint Engine
=====================

Kiến trúc:
    DNSE Stream
        -> FootprintEngine.add_trade()
        -> LocalStore (SQLite)
        -> RAM Aggregation
        -> Flask API / Next.js

Quy ước:
    - timestamp nội bộ: Unix epoch tính bằng giây.
    - timestamp lưu trong LocalStore: Unix epoch tính bằng mili giây.
    - volume: khối lượng giao dịch dương.
    - side: BUY / SELL / UNKNOWN.
    - Delta chỉ được xác nhận khi không có UNKNOWN volume.
    - Engine không tự suy luận aggressor side từ biến động giá.

Tương thích:
    - add_trade()
    - add_trades()
    - get_footprint()
    - get_trades()
    - get_candles()
    - get_symbols()
    - get_status()
    - clear_symbol()
    - reset()

Lưu ý:
    - SQLite persistence là tùy chọn.
    - Khi bật persistence, LocalStore phải cung cấp save_trade(),
      load_trades() và get_storage_status().
    - Chỉ một tầng trong pipeline nên chịu trách nhiệm ghi SQLite.
      Mặc định engine_persists_trades=True.
      Nếu dnse_stream.py tự ghi SQLite trước khi gọi engine,
      hãy cấu hình engine_persists_trades=False để tránh ghi lặp.
"""

from __future__ import annotations

import logging
import math
import threading
import time

from collections import OrderedDict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Optional


logger = logging.getLogger("lhp_ai_server.footprint.engine")


# ============================================================
# 1. CONFIG
# ============================================================

try:
    from scripts.footprint.config import (
        FOOTPRINT_MAX_TRADES,
        FOOTPRINT_CANDLE_SECONDS,
        FOOTPRINT_MAX_CANDLES,
        FOOTPRINT_MAX_PRICE_LEVELS,
        FOOTPRINT_KEEP_TRADE_TAPE,
        FOOTPRINT_PRICE_DECIMALS,
        FOOTPRINT_UNKNOWN_SIDE_LABEL,
        FOOTPRINT_REQUIRE_CONFIRMED_SIDE,
        DNSE_SIDE_BUY_VALUE,
        DNSE_SIDE_SELL_VALUE,
        DNSE_SIDE_CASE_INSENSITIVE,
    )

except ImportError:
    FOOTPRINT_MAX_TRADES = 20000
    FOOTPRINT_CANDLE_SECONDS = 60
    FOOTPRINT_MAX_CANDLES = 500
    FOOTPRINT_MAX_PRICE_LEVELS = 1000
    FOOTPRINT_KEEP_TRADE_TAPE = True
    FOOTPRINT_PRICE_DECIMALS = 2
    FOOTPRINT_UNKNOWN_SIDE_LABEL = "UNKNOWN"
    FOOTPRINT_REQUIRE_CONFIRMED_SIDE = True

    DNSE_SIDE_BUY_VALUE = ""
    DNSE_SIDE_SELL_VALUE = ""
    DNSE_SIDE_CASE_INSENSITIVE = True


# ============================================================
# 2. NORMALIZATION HELPERS
# ============================================================

def _to_decimal(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None

    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None

    if not result.is_finite():
        return None

    return result


def _to_float(value: Any) -> Optional[float]:
    value_decimal = _to_decimal(value)

    if value_decimal is None:
        return None

    result = float(value_decimal)

    if not math.isfinite(result):
        return None

    return result


def _to_positive_float(value: Any) -> Optional[float]:
    result = _to_float(value)

    if result is None or result <= 0:
        return None

    return result


def _normalize_symbol(value: Any) -> Optional[str]:
    if value is None:
        return None

    symbol = str(value).strip().upper()

    if not symbol:
        return None

    # Không cho phép mã chứa khoảng trắng.
    if any(char.isspace() for char in symbol):
        return None

    return symbol


def _first_value(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)

        if value is not None and value != "":
            return value

    return None


def _normalize_side(value: Any) -> str:
    unknown = str(FOOTPRINT_UNKNOWN_SIDE_LABEL or "UNKNOWN").upper()

    if value is None:
        return unknown

    raw = str(value).strip()

    if not raw:
        return unknown

    comparison = raw.upper() if DNSE_SIDE_CASE_INSENSITIVE else raw

    configured_buy = str(DNSE_SIDE_BUY_VALUE or "").strip()
    configured_sell = str(DNSE_SIDE_SELL_VALUE or "").strip()

    if configured_buy:
        target = (
            configured_buy.upper()
            if DNSE_SIDE_CASE_INSENSITIVE
            else configured_buy
        )

        if comparison == target:
            return "BUY"

    if configured_sell:
        target = (
            configured_sell.upper()
            if DNSE_SIDE_CASE_INSENSITIVE
            else configured_sell
        )

        if comparison == target:
            return "SELL"

    buy_labels = {
        "BUY",
        "B",
        "BID",
        "BUYER",
        "ACTIVE_BUY",
        "AGGRESSIVE_BUY",
        "BUY_AGGRESSOR",
    }

    sell_labels = {
        "SELL",
        "S",
        "ASK",
        "SELLER",
        "ACTIVE_SELL",
        "AGGRESSIVE_SELL",
        "SELL_AGGRESSOR",
    }

    if comparison in buy_labels:
        return "BUY"

    if comparison in sell_labels:
        return "SELL"

    return unknown


def _parse_timestamp(value: Any) -> Optional[float]:
    """
    Chuẩn hóa timestamp thành Unix epoch tính bằng giây.

    Trả về None nếu timestamp không hợp lệ.
    Không tự thay timestamp lỗi bằng thời gian hiện tại vì điều đó
    có thể đưa giao dịch lịch sử vào sai cây nến.
    """
    if value is None or value == "":
        return None

    if isinstance(value, datetime):
        dt = value

        if dt.tzinfo is None:
            # Giữ hành vi tương thích: datetime không timezone được
            # xem là UTC. Adapter phải xử lý nếu DNSE gửi giờ local.
            dt = dt.replace(tzinfo=timezone.utc)

        try:
            timestamp = dt.timestamp()
        except (ValueError, OverflowError, OSError):
            return None

        return timestamp if timestamp > 0 else None

    if isinstance(value, (int, float, Decimal)):
        number = _to_float(value)

        if number is None or number <= 0:
            return None

        # Microseconds trước, milliseconds sau.
        if number >= 100_000_000_000_000:
            number /= 1_000_000.0
        elif number >= 100_000_000_000:
            number /= 1_000.0

        # Loại bỏ giá trị quá xa hiện tại do dữ liệu sai đơn vị.
        if number < 1:
            return None

        return number

    raw = str(value).strip()

    if not raw:
        return None

    # Thử chuỗi số.
    try:
        number = float(raw)

        if math.isfinite(number) and number > 0:
            if number >= 100_000_000_000_000:
                number /= 1_000_000.0
            elif number >= 100_000_000_000:
                number /= 1_000.0

            return number if number >= 1 else None

    except (ValueError, OverflowError):
        pass

    # ISO 8601.
    try:
        normalized = raw

        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"

        dt = datetime.fromisoformat(normalized)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        timestamp = dt.timestamp()

        return timestamp if timestamp > 0 else None

    except (ValueError, OverflowError, OSError):
        return None


def _timestamp_to_iso(timestamp: Optional[float]) -> Optional[str]:
    if timestamp is None:
        return None

    try:
        return datetime.fromtimestamp(
            float(timestamp),
            tz=timezone.utc,
        ).isoformat()
    except (ValueError, OverflowError, OSError, TypeError):
        return None


def _price_key(price: float) -> str:
    decimals = max(0, min(8, int(FOOTPRINT_PRICE_DECIMALS)))
    quantizer = Decimal("1").scaleb(-decimals)

    value = Decimal(str(price)).quantize(
        quantizer,
        rounding=ROUND_HALF_UP,
    )

    return format(value, f".{decimals}f")


def _safe_trade_id(value: Any) -> Optional[str]:
    if value is None:
        return None

    result = str(value).strip()

    return result or None


def _timestamp_ms(timestamp_seconds: float) -> int:
    return int(round(timestamp_seconds * 1000))


def _copy_dict(value: dict[str, Any]) -> dict[str, Any]:
    # Tránh trả tham chiếu tới dict nội bộ của engine.
    return dict(value)


# ============================================================
# 3. ENGINE
# ============================================================

class FootprintEngine:
    """
    Engine tổng hợp giao dịch thành Footprint.

    persistence:
        Đối tượng LocalStore tương thích hoặc None.

    engine_persists_trades:
        True:
            Engine ghi giao dịch mới vào LocalStore trước khi cập nhật RAM.
        False:
            Tầng upstream chịu trách nhiệm lưu SQLite.

    recover_on_start:
        Tự phục hồi giao dịch gần nhất từ SQLite khi khởi tạo.
    """

    def __init__(
        self,
        max_trades: int = FOOTPRINT_MAX_TRADES,
        candle_seconds: int = FOOTPRINT_CANDLE_SECONDS,
        max_candles: int = FOOTPRINT_MAX_CANDLES,
        max_price_levels: int = FOOTPRINT_MAX_PRICE_LEVELS,
        keep_trade_tape: bool = FOOTPRINT_KEEP_TRADE_TAPE,
        persistence: Any = None,
        engine_persists_trades: bool = True,
        recover_on_start: bool = True,
        recovery_batch_size: int = 10000,
    ) -> None:
        self.max_trades = max(100, int(max_trades))
        self.candle_seconds = max(1, int(candle_seconds))
        self.max_candles = max(10, int(max_candles))
        self.max_price_levels = max(10, int(max_price_levels))
        self.keep_trade_tape = bool(keep_trade_tape)

        self.persistence = persistence
        self.engine_persists_trades = bool(engine_persists_trades)
        self.recovery_batch_size = max(100, int(recovery_batch_size))

        self._lock = threading.RLock()

        # symbol -> price_key -> aggregate.
        self._price_levels: dict[str, dict[str, dict[str, Any]]] = {}

        # symbol -> recent trades.
        self._trades: dict[str, list[dict[str, Any]]] = {}

        # symbol -> bucket -> candle.
        self._candles: dict[
            str,
            OrderedDict[int, dict[str, Any]],
        ] = {}

        # symbol -> recently seen IDs.
        self._seen_trade_ids: dict[str, OrderedDict[str, None]] = {}

        self._stats: dict[str, dict[str, Any]] = {}
        self._cumulative_delta: dict[str, Optional[float]] = {}
        self._delta_complete: dict[str, bool] = {}

        self._started_at = time.time()
        self._last_trade_at: Optional[float] = None

        self._received_count = 0
        self._accepted_count = 0
        self._duplicate_count = 0
        self._rejected_count = 0
        self._persistence_error_count = 0
        self._recovered_count = 0

        self._persistence_enabled = persistence is not None
        self._recovery_completed = False
        self._last_persistence_error: Optional[str] = None

        self._initialize_symbol_locked("DXG")

        if recover_on_start and self.persistence is not None:
            try:
                self.recover_from_store()
            except Exception:
                logger.exception("Không thể phục hồi Footprint từ LocalStore.")

        logger.info(
            "FootprintEngine initialized: max_trades=%s, "
            "candle_seconds=%s, max_candles=%s, persistence=%s",
            self.max_trades,
            self.candle_seconds,
            self.max_candles,
            self._persistence_enabled,
        )

    # ========================================================
    # 4. STATE INITIALIZATION
    # ========================================================

    @staticmethod
    def _empty_stats() -> dict[str, Any]:
        return {
            "trade_count": 0,
            "total_volume": 0.0,
            "buy_volume": 0.0,
            "sell_volume": 0.0,
            "unknown_volume": 0.0,
            "notional": 0.0,
            "last_price": None,
            "last_volume": None,
            "last_trade_at": None,
            "first_trade_at": None,
        }

    def _initialize_symbol_locked(self, symbol: str) -> None:
        if symbol in self._stats:
            return

        self._price_levels[symbol] = {}
        self._trades[symbol] = []
        self._candles[symbol] = OrderedDict()
        self._seen_trade_ids[symbol] = OrderedDict()
        self._stats[symbol] = self._empty_stats()

        self._cumulative_delta[symbol] = 0.0
        self._delta_complete[symbol] = True

    def register_symbol(self, symbol: str) -> bool:
        normalized = _normalize_symbol(symbol)

        if not normalized:
            return False

        with self._lock:
            self._initialize_symbol_locked(normalized)

        return True

    def register_symbols(self, symbols: list[str]) -> list[str]:
        registered: list[str] = []

        for symbol in symbols:
            if self.register_symbol(symbol):
                normalized = _normalize_symbol(symbol)

                if normalized:
                    registered.append(normalized)

        return registered

    # ========================================================
    # 5. TRADE NORMALIZATION
    # ========================================================

    def _normalize_trade(
        self,
        trade: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        if not isinstance(trade, dict):
            return None

        symbol = _normalize_symbol(
            _first_value(
                trade,
                "symbol",
                "ticker",
                "code",
                "stockSymbol",
            )
        )

        price = _to_positive_float(
            _first_value(
                trade,
                "price",
                "matchPrice",
                "matchedPrice",
                "lastPrice",
            )
        )

        volume = _to_positive_float(
            _first_value(
                trade,
                "quantity",
                "volume",
                "qty",
                "matchedQuantity",
                "matchedQty",
            )
        )

        raw_side = _first_value(
            trade,
            "side",
            "aggressorSide",
            "tradeSide",
            "matchSide",
        )

        raw_timestamp = _first_value(
            trade,
            "timestamp",
            "time",
            "tradeTime",
            "matchedAt",
            "transactTime",
        )

        trade_id = _safe_trade_id(
            _first_value(
                trade,
                "trade_id",
                "tradeId",
                "matchId",
                "executionId",
                "id",
            )
        )

        if not symbol or price is None or volume is None:
            return None

        timestamp = _parse_timestamp(raw_timestamp)

        if timestamp is None:
            return None

        side = _normalize_side(raw_side)

        # Chuẩn hóa về một cấu trúc chung.
        normalized = {
            "symbol": symbol,
            "price": price,
            "price_key": _price_key(price),
            "volume": volume,
            "quantity": volume,
            "side": side,
            "timestamp": timestamp,
            "timestamp_ms": _timestamp_ms(timestamp),
            "datetime": _timestamp_to_iso(timestamp),
            "trade_id": trade_id,
            "notional": price * volume,
        }

        return normalized

    # ========================================================
    # 6. PERSISTENCE ADAPTER
    # ========================================================

    def _persist_trade(self, trade: dict[str, Any]) -> bool:
        """
        Ghi giao dịch qua LocalStore.

        Trả về False nếu lưu thất bại. Khi engine_persists_trades=True,
        giao dịch không được đưa vào RAM nếu lưu SQLite thất bại.
        """
        if self.persistence is None or not self.engine_persists_trades:
            return True

        try:
            result = self.persistence.save_trade({
                "symbol": trade["symbol"],
                "trade_id": trade["trade_id"],
                "timestamp": trade["timestamp_ms"],
                "price": trade["price"],
                "volume": trade["volume"],
                "side": trade["side"],
                "raw": {
                    "symbol": trade["symbol"],
                    "price": trade["price"],
                    "quantity": trade["volume"],
                    "side": trade["side"],
                    "timestamp": trade["timestamp"],
                    "trade_id": trade["trade_id"],
                },
            })

            # Hỗ trợ LocalStore trả bool hoặc dict.
            if result is False:
                raise RuntimeError("LocalStore.save_trade() trả về False")

            if isinstance(result, dict):
                if result.get("success") is False:
                    raise RuntimeError(
                        str(result.get("error") or "SQLite save failed")
                    )

                if result.get("duplicate") is True:
                    return False

            self._last_persistence_error = None
            return True

        except Exception as exc:
            self._persistence_error_count += 1
            self._last_persistence_error = str(exc)

            logger.exception(
                "Không thể lưu giao dịch %s/%s vào SQLite",
                trade.get("symbol"),
                trade.get("trade_id"),
            )

            return False

    # ========================================================
    # 7. ADD TRADE
    # ========================================================

    def add_trade(self, trade: dict[str, Any]) -> bool:
        """
        Nhận một giao dịch.

        True:
            Giao dịch mới được chấp nhận vào engine.
        False:
            Dữ liệu không hợp lệ, trùng ID hoặc không ghi được SQLite.
        """
        with self._lock:
            self._received_count += 1

        normalized = self._normalize_trade(trade)

        if normalized is None:
            with self._lock:
                self._rejected_count += 1

            logger.debug("Bỏ qua giao dịch không hợp lệ: %r", trade)
            return False

        symbol = normalized["symbol"]
        trade_id = normalized["trade_id"]

        with self._lock:
            self._initialize_symbol_locked(symbol)

            if trade_id:
                if trade_id in self._seen_trade_ids[symbol]:
                    self._duplicate_count += 1
                    return False

        # Không giữ engine lock trong lúc thao tác SQLite.
        # Việc này hạn chế chặn các request đọc dữ liệu.
        persisted = self._persist_trade(normalized)

        if not persisted:
            with self._lock:
                # Nếu LocalStore báo duplicate, không tăng rejected.
                # Nhưng không được thêm giao dịch vào RAM lần nữa.
                self._duplicate_count += 1

            return False

        with self._lock:
            self._initialize_symbol_locked(symbol)

            # Kiểm tra lại sau khi rời lock để ghi SQLite.
            # Tránh hai thread cùng đưa một ID vào RAM.
            if trade_id and trade_id in self._seen_trade_ids[symbol]:
                self._duplicate_count += 1
                return False

            if trade_id:
                seen = self._seen_trade_ids[symbol]
                seen[trade_id] = None

                while len(seen) > self.max_trades * 2:
                    seen.popitem(last=False)

            self._update_price_level_locked(normalized)
            self._update_stats_locked(normalized)

            if self.keep_trade_tape:
                self._trades[symbol].append(normalized)

                self._trades[symbol].sort(
                    key=lambda item: (
                        item["timestamp"],
                        item.get("trade_id") or "",
                    )
                )

                if len(self._trades[symbol]) > self.max_trades:
                    self._trades[symbol] = self._trades[symbol][
                        -self.max_trades:
                    ]

            self._update_candle_locked(normalized)

            self._accepted_count += 1

            if (
                self._last_trade_at is None
                or normalized["timestamp"] > self._last_trade_at
            ):
                self._last_trade_at = normalized["timestamp"]

        return True

    def add_trades(self, trades: list[dict[str, Any]]) -> dict[str, int]:
        accepted = 0
        rejected = 0

        for trade in trades:
            if self.add_trade(trade):
                accepted += 1
            else:
                rejected += 1

        return {
            "received": len(trades),
            "accepted": accepted,
            "rejected_or_duplicate": rejected,
        }

    # ========================================================
    # 8. PRICE LEVEL AGGREGATION
    # ========================================================

    def _update_price_level_locked(
        self,
        trade: dict[str, Any],
    ) -> None:
        symbol = trade["symbol"]
        price_key = trade["price_key"]
        price = trade["price"]
        volume = trade["volume"]
        side = trade["side"]

        levels = self._price_levels[symbol]

        if price_key not in levels:
            levels[price_key] = {
                "price": price,
                "price_key": price_key,
                "total_volume": 0.0,
                "buy_volume": 0.0,
                "sell_volume": 0.0,
                "unknown_volume": 0.0,
                "trade_count": 0,
                "notional": 0.0,
                "delta": None,
                "delta_percent": None,
            }

        level = levels[price_key]

        level["total_volume"] += volume
        level["trade_count"] += 1
        level["notional"] += trade["notional"]

        if side == "BUY":
            level["buy_volume"] += volume
        elif side == "SELL":
            level["sell_volume"] += volume
        else:
            level["unknown_volume"] += volume

        directional = level["buy_volume"] + level["sell_volume"]

        if level["unknown_volume"] <= 0:
            level["delta"] = (
                level["buy_volume"] - level["sell_volume"]
            )
            level["delta_percent"] = (
                level["delta"] / directional * 100.0
                if directional > 0
                else None
            )
        else:
            level["delta"] = None
            level["delta_percent"] = None

        # Không xóa mức giá khỏi thống kê tổng hợp.
        # Nếu có nhiều mức giá, giữ toàn bộ aggregate trong RAM để POC
        # và tổng volume không bị sai. API có thể giới hạn kết quả trả về.

    # ========================================================
    # 9. CANDLE AGGREGATION
    # ========================================================

    def _update_candle_locked(
        self,
        trade: dict[str, Any],
    ) -> None:
        symbol = trade["symbol"]
        timestamp = trade["timestamp"]
        price = trade["price"]
        volume = trade["volume"]
        side = trade["side"]

        bucket = (
            int(timestamp // self.candle_seconds)
            * self.candle_seconds
        )

        candles = self._candles[symbol]

        if bucket not in candles:
            candles[bucket] = {
                "symbol": symbol,
                "timestamp": bucket,
                "timestamp_ms": bucket * 1000,
                "datetime": _timestamp_to_iso(bucket),
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 0.0,
                "buy_volume": 0.0,
                "sell_volume": 0.0,
                "unknown_volume": 0.0,
                "trade_count": 0,
                "notional": 0.0,
                "delta": None,
                "cumulative_delta": None,
                "_first_trade_timestamp": timestamp,
                "_last_trade_timestamp": timestamp,
                "_first_trade_id": trade.get("trade_id") or "",
                "_last_trade_id": trade.get("trade_id") or "",
            }

        candle = candles[bucket]

        candle["high"] = max(candle["high"], price)
        candle["low"] = min(candle["low"], price)

        # OHLC phải dựa theo timestamp, không dựa theo thứ tự nhận.
        first_key = (
            timestamp,
            trade.get("trade_id") or "",
        )

        open_key = (
            candle["_first_trade_timestamp"],
            candle["_first_trade_id"],
        )

        if first_key < open_key:
            candle["_first_trade_timestamp"] = timestamp
            candle["_first_trade_id"] = trade.get("trade_id") or ""
            candle["open"] = price

        last_key = (
            timestamp,
            trade.get("trade_id") or "",
        )

        close_key = (
            candle["_last_trade_timestamp"],
            candle["_last_trade_id"],
        )

        if last_key >= close_key:
            candle["_last_trade_timestamp"] = timestamp
            candle["_last_trade_id"] = trade.get("trade_id") or ""
            candle["close"] = price

        candle["volume"] += volume
        candle["trade_count"] += 1
        candle["notional"] += trade["notional"]

        if side == "BUY":
            candle["buy_volume"] += volume
        elif side == "SELL":
            candle["sell_volume"] += volume
        else:
            candle["unknown_volume"] += volume

        if candle["unknown_volume"] <= 0:
            candle["delta"] = (
                candle["buy_volume"] - candle["sell_volume"]
            )
        else:
            candle["delta"] = None

        # Giữ bucket theo thứ tự thời gian, kể cả khi nhận giao dịch muộn.
        candles[bucket] = candle
        candles = OrderedDict(sorted(candles.items()))
        self._candles[symbol] = candles

        while len(candles) > self.max_candles:
            candles.popitem(last=False)

        self._recalculate_cumulative_delta_locked(symbol)

    def _recalculate_cumulative_delta_locked(
        self,
        symbol: str,
    ) -> None:
        """
        Cumulative Delta được tính trên tập nến hiện còn trong RAM.

        Nếu nến có UNKNOWN volume, cumulative delta của nến đó và
        các nến sau nó không được xác nhận.
        """
        running_delta = 0.0
        complete_so_far = True

        for candle in self._candles[symbol].values():
            delta = candle["delta"]

            if delta is None:
                candle["cumulative_delta"] = None
                complete_so_far = False
                continue

            if complete_so_far:
                running_delta += delta
                candle["cumulative_delta"] = running_delta
            else:
                candle["cumulative_delta"] = None

        if complete_so_far:
            self._cumulative_delta[symbol] = running_delta
            self._delta_complete[symbol] = True
        else:
            self._cumulative_delta[symbol] = None
            self._delta_complete[symbol] = False

    # ========================================================
    # 10. SYMBOL STATS
    # ========================================================

    def _update_stats_locked(
        self,
        trade: dict[str, Any],
    ) -> None:
        symbol = trade["symbol"]
        stats = self._stats[symbol]

        volume = trade["volume"]
        price = trade["price"]
        side = trade["side"]
        timestamp = trade["timestamp"]

        stats["trade_count"] += 1
        stats["total_volume"] += volume
        stats["notional"] += price * volume

        if side == "BUY":
            stats["buy_volume"] += volume
        elif side == "SELL":
            stats["sell_volume"] += volume
        else:
            stats["unknown_volume"] += volume

        # Last trade dựa trên timestamp lớn nhất.
        if (
            stats["last_trade_at"] is None
            or timestamp >= stats["last_trade_at"]
        ):
            stats["last_price"] = price
            stats["last_volume"] = volume
            stats["last_trade_at"] = timestamp

        if (
            stats["first_trade_at"] is None
            or timestamp < stats["first_trade_at"]
        ):
            stats["first_trade_at"] = timestamp

    # ========================================================
    # 11. FOOTPRINT API
    # ========================================================

    def get_footprint(
        self,
        symbol: str,
        *,
        sort: str = "price",
        descending: bool = False,
    ) -> dict[str, Any]:
        normalized_symbol = _normalize_symbol(symbol)

        if not normalized_symbol:
            return {
                "success": False,
                "error": "Invalid symbol",
                "symbol": symbol,
            }

        with self._lock:
            self._initialize_symbol_locked(normalized_symbol)

            stats = _copy_dict(self._stats[normalized_symbol])

            levels = [
                _copy_dict(level)
                for level in self._price_levels[
                    normalized_symbol
                ].values()
            ]

            cumulative_delta = self._cumulative_delta[normalized_symbol]
            cumulative_delta_complete = self._delta_complete[
                normalized_symbol
            ]

            candle_count = len(self._candles[normalized_symbol])

        if sort == "volume":
            levels.sort(
                key=lambda item: item["total_volume"],
                reverse=descending,
            )
        elif sort == "delta":
            # UNKNOWN delta luôn được xếp cuối, bất kể chiều sắp xếp.
            known = [item for item in levels if item["delta"] is not None]
            unknown = [item for item in levels if item["delta"] is None]

            known.sort(
                key=lambda item: item["delta"],
                reverse=descending,
            )

            levels = known + unknown
        else:
            levels.sort(
                key=lambda item: item["price"],
                reverse=descending,
            )

        poc = (
            max(
                levels,
                key=lambda item: item["total_volume"],
            )
            if levels
            else None
        )

        directional_volume = stats["buy_volume"] + stats["sell_volume"]

        total_delta: Optional[float]

        if stats["unknown_volume"] <= 0:
            total_delta = stats["buy_volume"] - stats["sell_volume"]
        else:
            total_delta = None

        delta_percent = None

        if total_delta is not None and directional_volume > 0:
            delta_percent = total_delta / directional_volume * 100.0

        return {
            "success": True,
            "symbol": normalized_symbol,
            "generated_at": _timestamp_to_iso(time.time()),
            "summary": {
                "trade_count": stats["trade_count"],
                "total_volume": stats["total_volume"],
                "buy_volume": stats["buy_volume"],
                "sell_volume": stats["sell_volume"],
                "unknown_volume": stats["unknown_volume"],
                "notional": stats["notional"],
                "last_price": stats["last_price"],
                "last_volume": stats["last_volume"],
                "first_trade_at": _timestamp_to_iso(
                    stats["first_trade_at"]
                ),
                "last_trade_at": _timestamp_to_iso(
                    stats["last_trade_at"]
                ),
                "delta": total_delta,
                "delta_percent": delta_percent,
                "cumulative_delta": (
                    cumulative_delta
                    if cumulative_delta_complete
                    else None
                ),
                "delta_complete": stats["unknown_volume"] <= 0,
                "cumulative_delta_complete": cumulative_delta_complete,
                "price_level_count": len(levels),
                "candle_count": candle_count,
            },
            "poc": _copy_dict(poc) if poc else None,
            "price_levels": levels,
        }

    # ========================================================
    # 12. TRADE TAPE
    # ========================================================

    def get_trades(
        self,
        symbol: str,
        *,
        limit: int = 100,
        newest_first: bool = True,
    ) -> dict[str, Any]:
        normalized_symbol = _normalize_symbol(symbol)

        if not normalized_symbol:
            return {
                "success": False,
                "error": "Invalid symbol",
                "symbol": symbol,
                "trades": [],
            }

        limit = max(1, min(int(limit), self.max_trades))

        with self._lock:
            self._initialize_symbol_locked(normalized_symbol)

            if not self.keep_trade_tape:
                return {
                    "success": True,
                    "symbol": normalized_symbol,
                    "count": 0,
                    "trades": [],
                    "trade_tape_enabled": False,
                }

            trades = list(self._trades[normalized_symbol])

        if newest_first:
            trades.reverse()

        trades = trades[:limit]

        return {
            "success": True,
            "symbol": normalized_symbol,
            "count": len(trades),
            "trade_tape_enabled": True,
            "trades": trades,
        }

    # ========================================================
    # 13. CANDLES
    # ========================================================

    def get_candles(
        self,
        symbol: str,
        *,
        limit: int = 100,
        newest_first: bool = False,
    ) -> dict[str, Any]:
        normalized_symbol = _normalize_symbol(symbol)

        if not normalized_symbol:
            return {
                "success": False,
                "error": "Invalid symbol",
                "symbol": symbol,
                "candles": [],
            }

        limit = max(1, min(int(limit), self.max_candles))

        with self._lock:
            self._initialize_symbol_locked(normalized_symbol)

            candles = [
                {
                    key: value
                    for key, value in candle.items()
                    if not key.startswith("_")
                }
                for candle in self._candles[normalized_symbol].values()
            ]

        if newest_first:
            candles.reverse()

        candles = candles[:limit]

        return {
            "success": True,
            "symbol": normalized_symbol,
            "count": len(candles),
            "interval_seconds": self.candle_seconds,
            "candles": candles,
        }

    # ========================================================
    # 14. SYMBOLS
    # ========================================================

    def get_symbols(self) -> list[str]:
        with self._lock:
            return sorted(self._stats.keys())

    # ========================================================
    # 15. STATUS
    # ========================================================

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            symbols: dict[str, Any] = {}

            for symbol, stats in self._stats.items():
                symbols[symbol] = {
                    "trade_count": stats["trade_count"],
                    "total_volume": stats["total_volume"],
                    "buy_volume": stats["buy_volume"],
                    "sell_volume": stats["sell_volume"],
                    "unknown_volume": stats["unknown_volume"],
                    "last_price": stats["last_price"],
                    "last_trade_at": _timestamp_to_iso(
                        stats["last_trade_at"]
                    ),
                    "price_level_count": len(
                        self._price_levels[symbol]
                    ),
                    "candle_count": len(self._candles[symbol]),
                }

            return {
                "success": True,
                "engine": "DNSE Footprint Engine",
                "storage": (
                    "SQLite+RAM"
                    if self.persistence is not None
                    else "RAM"
                ),
                "persistence_enabled": self.persistence is not None,
                "engine_persists_trades": self.engine_persists_trades,
                "recovery_completed": self._recovery_completed,
                "recovered_count": self._recovered_count,
                "started_at": _timestamp_to_iso(self._started_at),
                "uptime_seconds": max(
                    0,
                    int(time.time() - self._started_at),
                ),
                "last_trade_at": _timestamp_to_iso(self._last_trade_at),
                "received_count": self._received_count,
                "accepted_count": self._accepted_count,
                "duplicate_count": self._duplicate_count,
                "rejected_count": self._rejected_count,
                "persistence_error_count": self._persistence_error_count,
                "last_persistence_error": self._last_persistence_error,
                "trade_tape_enabled": self.keep_trade_tape,
                "candle_seconds": self.candle_seconds,
                "max_trades": self.max_trades,
                "max_candles": self.max_candles,
                "max_price_levels": self.max_price_levels,
                "symbols": symbols,
            }

    # ========================================================
    # 16. RECOVERY FROM SQLITE
    # ========================================================

    def recover_from_store(
        self,
        *,
        symbols: Optional[list[str]] = None,
        limit: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        Nạp dữ liệu từ LocalStore vào RAM.

        LocalStore.load_trades() được kỳ vọng trả về:
            {
                "symbol": "...",
                "trade_id": "...",
                "timestamp": epoch milliseconds,
                "price": ...,
                "volume": ...,
                "side": ...
            }

        Nếu LocalStore của dự án trả về cấu trúc khác, cần điều chỉnh
        adapter này cho khớp với local_store.py thực tế.
        """
        if self.persistence is None:
            return {
                "success": False,
                "error": "Persistence is not configured",
                "recovered": 0,
            }

        load_trades = getattr(self.persistence, "load_trades", None)

        if not callable(load_trades):
            return {
                "success": False,
                "error": "LocalStore không có phương thức load_trades()",
                "recovered": 0,
            }

        requested_limit = (
            max(1, int(limit))
            if limit is not None
            else self.recovery_batch_size
        )

        recovered = 0
        duplicates = 0
        rejected = 0

        try:
            # LocalStore hiện tại có thể chỉ trả một trang giới hạn.
            # Chỉ gọi một lần nếu chưa có cursor/page API.
            rows = load_trades(
                limit=requested_limit,
                order="ASC",
            )

            if isinstance(rows, dict):
                rows = rows.get("trades", rows.get("data", []))

            if not isinstance(rows, list):
                raise TypeError(
                    "LocalStore.load_trades() phải trả về list hoặc dict chứa trades"
                )

            normalized_symbols = None

            if symbols:
                normalized_symbols = {
                    item
                    for item in (
                        _normalize_symbol(symbol)
                        for symbol in symbols
                    )
                    if item
                }

            # Nạp từ cũ đến mới để OHLCV được tạo đúng thứ tự.
            rows = sorted(
                rows,
                key=lambda row: (
                    _parse_timestamp(
                        _first_value(
                            row,
                            "timestamp",
                            "time",
                            "tradeTime",
                        )
                    )
                    or 0,
                    str(
                        _first_value(
                            row,
                            "trade_id",
                            "tradeId",
                            "id",
                        )
                        or ""
                    ),
                ),
            )

            for row in rows:
                if not isinstance(row, dict):
                    rejected += 1
                    continue

                symbol = _normalize_symbol(
                    _first_value(row, "symbol", "ticker", "code")
                )

                if not symbol:
                    rejected += 1
                    continue

                if (
                    normalized_symbols is not None
                    and symbol not in normalized_symbols
                ):
                    continue

                # Không gọi add_trade() vì nó có thể ghi lại SQLite.
                normalized = self._normalize_trade(row)

                if normalized is None:
                    rejected += 1
                    continue

                trade_id = normalized["trade_id"]

                with self._lock:
                    self._initialize_symbol_locked(symbol)

                    if (
                        trade_id
                        and trade_id in self._seen_trade_ids[symbol]
                    ):
                        duplicates += 1
                        continue

                    if trade_id:
                        self._seen_trade_ids[symbol][trade_id] = None

                    self._update_price_level_locked(normalized)
                    self._update_stats_locked(normalized)

                    if self.keep_trade_tape:
                        self._trades[symbol].append(normalized)

                        if len(self._trades[symbol]) > self.max_trades:
                            self._trades[symbol] = self._trades[symbol][
                                -self.max_trades:
                            ]

                    self._update_candle_locked(normalized)

                    if (
                        self._last_trade_at is None
                        or normalized["timestamp"] > self._last_trade_at
                    ):
                        self._last_trade_at = normalized["timestamp"]

                    self._accepted_count += 1

                recovered += 1

            with self._lock:
                self._recovered_count += recovered
                self._recovery_completed = True

            logger.info(
                "Footprint recovery completed: recovered=%s duplicates=%s rejected=%s",
                recovered,
                duplicates,
                rejected,
            )

            return {
                "success": True,
                "recovered": recovered,
                "duplicates": duplicates,
                "rejected": rejected,
            }

        except Exception as exc:
            with self._lock:
                self._last_persistence_error = str(exc)

            logger.exception("Lỗi phục hồi dữ liệu từ SQLite.")

            return {
                "success": False,
                "error": str(exc),
                "recovered": recovered,
                "duplicates": duplicates,
                "rejected": rejected,
            }

    # ========================================================
    # 17. CLEAR / RESET
    # ========================================================

    def clear_symbol(self, symbol: str) -> bool:
        """
        Xóa dữ liệu của một mã khỏi RAM.

        Không xóa dữ liệu trong SQLite.
        """
        normalized_symbol = _normalize_symbol(symbol)

        if not normalized_symbol:
            return False

        with self._lock:
            self._price_levels.pop(normalized_symbol, None)
            self._trades.pop(normalized_symbol, None)
            self._candles.pop(normalized_symbol, None)
            self._seen_trade_ids.pop(normalized_symbol, None)
            self._stats.pop(normalized_symbol, None)
            self._cumulative_delta.pop(normalized_symbol, None)
            self._delta_complete.pop(normalized_symbol, None)

        logger.info(
            "Đã xóa dữ liệu Footprint trong RAM cho %s",
            normalized_symbol,
        )

        return True

    def reset(self) -> None:
        """
        Reset dữ liệu tổng hợp trong RAM.

        Không xóa dữ liệu SQLite. Nếu bật recovery tự động, dữ liệu có
        thể được nạp lại khi gọi recover_from_store().
        """
        with self._lock:
            symbols = list(self._stats.keys())

            for symbol in symbols:
                self._price_levels[symbol] = {}
                self._trades[symbol] = []
                self._candles[symbol] = OrderedDict()
                self._seen_trade_ids[symbol] = OrderedDict()
                self._stats[symbol] = self._empty_stats()
                self._cumulative_delta[symbol] = 0.0
                self._delta_complete[symbol] = True

            self._received_count = 0
            self._accepted_count = 0
            self._duplicate_count = 0
            self._rejected_count = 0
            self._last_trade_at = None
            self._started_at = time.time()
            self._recovered_count = 0
            self._recovery_completed = False

        logger.warning("Đã reset toàn bộ Footprint Engine trong RAM.")


# ============================================================
# 18. SHARED INSTANCE
# ============================================================

def _build_default_persistence() -> Any:
    """
    Tạo LocalStore mặc định nếu module tồn tại.

    Nếu LocalStore không import được, engine vẫn chạy RAM-only.
    """
    try:
        from scripts.footprint.local_store import get_local_store

        return get_local_store()

    except ImportError:
        logger.warning(
            "Không tìm thấy LocalStore; Footprint Engine chạy RAM-only."
        )
        return None

    except Exception:
        logger.exception(
            "Không thể khởi tạo LocalStore; Footprint Engine chạy RAM-only."
        )
        return None


_default_persistence = _build_default_persistence()

footprint_engine = FootprintEngine(
    persistence=_default_persistence,
    engine_persists_trades=True,
    recover_on_start=True,
)

# Alias tương thích với code cũ.
engine = footprint_engine

