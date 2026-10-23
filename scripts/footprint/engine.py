"""
DNSE Footprint Engine
=====================

Engine tổng hợp dữ liệu giao dịch thành dữ liệu Footprint.

Các chức năng chính:
1. Nhận dữ liệu giao dịch từ DNSE.
2. Chuẩn hóa symbol, price, volume, side và timestamp.
3. Tổng hợp volume theo từng mức giá.
4. Phân loại buy/sell/unknown volume.
5. Tính Delta và Cumulative Delta khi có đủ thông tin.
6. Xác định Point of Control (POC).
7. Tổng hợp nến OHLCV.
8. Lưu trade tape trong RAM.
9. Hạn chế bản ghi trùng bằng trade ID.
10. Cung cấp dữ liệu cho Flask API.

Lưu ý:
- Dữ liệu được lưu trong RAM, không tự ghi vào Supabase.
- Khởi động lại tiến trình sẽ làm mất dữ liệu trong RAM.
- BUY/SELL phải dựa trên định nghĩa side chính xác của DNSE.
- Engine không thể tự tạo ra dữ liệu Bid/Ask nếu nguồn không cung cấp.
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
# 1. ĐỌC CẤU HÌNH
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
    # Giá trị dự phòng giúp module có thể được import trong môi trường
    # thử nghiệm. Trong dự án thật, nên sử dụng scripts/footprint/config.py.
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
# 2. HÀM CHUẨN HÓA DỮ LIỆU
# ============================================================

def _to_decimal(value: Any) -> Optional[Decimal]:
    """
    Chuyển dữ liệu số thành Decimal để hạn chế sai số số thực.

    Chấp nhận:
        12500
        12500.5
        "12500"
        "12500.50"

    Không chấp nhận:
        None
        ""
        "abc"
        NaN
        Infinity
    """
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
    """Chuyển giá trị số thành float nếu hợp lệ."""
    number = _to_decimal(value)

    if number is None:
        return None

    return float(number)


def _to_positive_float(value: Any) -> Optional[float]:
    """Chỉ chấp nhận số thực lớn hơn 0."""
    number = _to_float(value)

    if number is None or number <= 0:
        return None

    return number


def _normalize_symbol(value: Any) -> Optional[str]:
    """Chuẩn hóa mã chứng khoán."""
    if value is None:
        return None

    symbol = str(value).strip().upper()

    if not symbol:
        return None

    return symbol


def _normalize_side(value: Any) -> str:
    """
    Chuẩn hóa bên giao dịch.

    Trả về một trong:
        BUY
        SELL
        UNKNOWN

    Chỉ ánh xạ giá trị được cấu hình rõ ràng hoặc các nhãn chữ
    thông dụng. Không tự suy luận bên giao dịch từ giá tăng/giảm.

    LƯU Ý:
    Nếu DNSE sử dụng mã số hoặc enum riêng, cần cấu hình đúng
    DNSE_SIDE_BUY_VALUE và DNSE_SIDE_SELL_VALUE trong .env.
    """
    if value is None:
        return FOOTPRINT_UNKNOWN_SIDE_LABEL

    raw = str(value).strip()

    if not raw:
        return FOOTPRINT_UNKNOWN_SIDE_LABEL

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

    # Những nhãn này chỉ được nhận diện khi payload thực sự cung cấp
    # nhãn văn bản tương ứng.
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

    return FOOTPRINT_UNKNOWN_SIDE_LABEL


def _parse_timestamp(value: Any) -> float:
    """
    Chuẩn hóa timestamp thành Unix epoch tính bằng giây.

    Hỗ trợ:
        - Unix timestamp dạng giây.
        - Unix timestamp dạng mili giây.
        - ISO 8601 string.
        - datetime.

    Nếu không đọc được timestamp, sử dụng thời gian hiện tại.
    """
    if value is None or value == "":
        return time.time()

    if isinstance(value, datetime):
        dt = value

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.timestamp()

    if isinstance(value, (int, float, Decimal)):
        number = float(value)

        if not math.isfinite(number) or number <= 0:
            return time.time()

        # Timestamp 13 chữ số thường là mili giây.
        if number >= 100_000_000_000:
            number /= 1000.0

        # Timestamp micro giây có thể lớn hơn nhiều.
        elif number >= 100_000_000_000_000:
            number /= 1_000_000.0

        return number

    raw = str(value).strip()

    if not raw:
        return time.time()

    # Thử đọc chuỗi số trước.
    try:
        number = float(raw)

        if math.isfinite(number) and number > 0:
            if number >= 100_000_000_000_000:
                number /= 1_000_000.0
            elif number >= 100_000_000_000:
                number /= 1000.0

            return number

    except ValueError:
        pass

    # Hỗ trợ ISO 8601, ví dụ:
    # 2026-10-23T09:15:00+07:00
    # 2026-10-23T02:15:00Z
    try:
        normalized = raw

        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"

        dt = datetime.fromisoformat(normalized)

        if dt.tzinfo is None:
            # Timestamp không có timezone được xem là UTC.
            # Nếu DNSE trả giờ Việt Nam không kèm timezone, cần
            # chuẩn hóa timezone tại lớp adapter DNSE.
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.timestamp()

    except (ValueError, OverflowError):
        logger.debug(
            "Không phân tích được timestamp %r; dùng thời gian hiện tại",
            value,
        )

        return time.time()


def _timestamp_to_iso(timestamp: float) -> str:
    """Chuyển Unix timestamp thành ISO 8601 UTC."""
    return datetime.fromtimestamp(
        timestamp,
        tz=timezone.utc,
    ).isoformat()


def _price_key(price: float) -> str:
    """
    Chuẩn hóa giá thành khóa chuỗi để gom volume theo mức giá.

    Mặc định giữ 2 chữ số thập phân. Đây là cách gom giá phục vụ
    hiển thị; không thay thế quy tắc bước giá của từng sàn.
    """
    decimals = max(0, int(FOOTPRINT_PRICE_DECIMALS))

    quantizer = Decimal("1").scaleb(-decimals)

    value = Decimal(str(price)).quantize(
        quantizer,
        rounding=ROUND_HALF_UP,
    )

    return format(value, f".{decimals}f")


def _safe_trade_id(value: Any) -> Optional[str]:
    """Chuẩn hóa trade ID."""
    if value is None:
        return None

    result = str(value).strip()

    if not result:
        return None

    return result


# ============================================================
# 3. FOOTPRINT ENGINE
# ============================================================

class FootprintEngine:
    """
    Bộ máy tổng hợp Footprint cho nhiều mã cổ phiếu.

    API chính:

        engine.add_trade({
            "symbol": "DXG",
            "price": 15.25,
            "quantity": 1000,
            "side": "BUY",
            "timestamp": 1792718100,
            "trade_id": "abc123",
        })

        engine.get_footprint("DXG")
        engine.get_trades("DXG")
        engine.get_candles("DXG")
        engine.get_status()

    Tất cả dữ liệu đều được giữ trong RAM.
    """

    def __init__(
        self,
        max_trades: int = FOOTPRINT_MAX_TRADES,
        candle_seconds: int = FOOTPRINT_CANDLE_SECONDS,
        max_candles: int = FOOTPRINT_MAX_CANDLES,
        max_price_levels: int = FOOTPRINT_MAX_PRICE_LEVELS,
        keep_trade_tape: bool = FOOTPRINT_KEEP_TRADE_TAPE,
    ) -> None:
        self.max_trades = max(100, int(max_trades))
        self.candle_seconds = max(1, int(candle_seconds))
        self.max_candles = max(10, int(max_candles))
        self.max_price_levels = max(10, int(max_price_levels))
        self.keep_trade_tape = bool(keep_trade_tape)

        # Khóa chung bảo vệ trạng thái engine.
        self._lock = threading.RLock()

        # symbol -> price_key -> thống kê tại mức giá.
        self._price_levels: dict[str, dict[str, dict[str, Any]]] = {}

        # symbol -> danh sách giao dịch gần nhất.
        self._trades: dict[str, list[dict[str, Any]]] = {}

        # symbol -> timestamp bucket -> candle.
        self._candles: dict[str, OrderedDict[int, dict[str, Any]]] = {}

        # symbol -> trade IDs đã thấy.
        self._seen_trade_ids: dict[str, OrderedDict[str, None]] = {}

        # symbol -> tổng hợp thống kê.
        self._stats: dict[str, dict[str, Any]] = {}

        # symbol -> cumulative delta hiện tại.
        self._cumulative_delta: dict[str, float] = {}

        # symbol -> trạng thái delta có đầy đủ phân loại hay không.
        self._delta_complete: dict[str, bool] = {}

        self._started_at = time.time()
        self._last_trade_at: Optional[float] = None

        self._received_count = 0
        self._accepted_count = 0
        self._duplicate_count = 0
        self._rejected_count = 0

        self._initialize_symbol_locked("DXG")

        logger.info(
            "FootprintEngine initialized: max_trades=%s, "
            "candle_seconds=%s, max_candles=%s",
            self.max_trades,
            self.candle_seconds,
            self.max_candles,
        )

    # ========================================================
    # 4. KHỞI TẠO TRẠNG THÁI MÃ
    # ========================================================

    def _initialize_symbol_locked(self, symbol: str) -> None:
        """Khởi tạo bộ nhớ cho một mã; phải gọi khi đang giữ lock."""
        if symbol in self._stats:
            return

        self._price_levels[symbol] = {}
        self._trades[symbol] = []
        self._candles[symbol] = OrderedDict()
        self._seen_trade_ids[symbol] = OrderedDict()

        self._stats[symbol] = {
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

        self._cumulative_delta[symbol] = 0.0
        self._delta_complete[symbol] = True

    def register_symbol(self, symbol: str) -> bool:
        """Đăng ký mã cần theo dõi."""
        normalized = _normalize_symbol(symbol)

        if not normalized:
            return False

        with self._lock:
            self._initialize_symbol_locked(normalized)

        return True

    def register_symbols(self, symbols: list[str]) -> list[str]:
        """Đăng ký nhiều mã; trả về danh sách mã hợp lệ."""
        registered: list[str] = []

        for symbol in symbols:
            if self.register_symbol(symbol):
                normalized = _normalize_symbol(symbol)

                if normalized:
                    registered.append(normalized)

        return registered

    # ========================================================
    # 5. CHUẨN HÓA GIAO DỊCH
    # ========================================================

    def _normalize_trade(
        self,
        trade: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """
        Chuẩn hóa một giao dịch.

        Tên trường đầu vào được hỗ trợ:
            symbol / ticker / code
            price / matchPrice / matchedPrice
            quantity / volume / qty / matchedQuantity
            side / aggressorSide / tradeSide
            timestamp / time / tradeTime / matchedAt
            trade_id / tradeId / matchId / id
        """
        if not isinstance(trade, dict):
            return None

        symbol = _normalize_symbol(
            trade.get("symbol")
            or trade.get("ticker")
            or trade.get("code")
            or trade.get("stockSymbol")
        )

        price = _to_positive_float(
            trade.get("price")
            if trade.get("price") is not None
            else trade.get("matchPrice")
            if trade.get("matchPrice") is not None
            else trade.get("matchedPrice")
            if trade.get("matchedPrice") is not None
            else trade.get("lastPrice")
        )

        volume = _to_positive_float(
            trade.get("quantity")
            if trade.get("quantity") is not None
            else trade.get("volume")
            if trade.get("volume") is not None
            else trade.get("qty")
            if trade.get("qty") is not None
            else trade.get("matchedQuantity")
            if trade.get("matchedQuantity") is not None
            else trade.get("matchedQty")
        )

        raw_side = (
            trade.get("side")
            if trade.get("side") is not None
            else trade.get("aggressorSide")
            if trade.get("aggressorSide") is not None
            else trade.get("tradeSide")
            if trade.get("tradeSide") is not None
            else trade.get("matchSide")
        )

        raw_timestamp = (
            trade.get("timestamp")
            if trade.get("timestamp") is not None
            else trade.get("time")
            if trade.get("time") is not None
            else trade.get("tradeTime")
            if trade.get("tradeTime") is not None
            else trade.get("matchedAt")
            if trade.get("matchedAt") is not None
            else trade.get("transactTime")
        )

        trade_id = _safe_trade_id(
            trade.get("trade_id")
            or trade.get("tradeId")
            or trade.get("matchId")
            or trade.get("executionId")
            or trade.get("id")
        )

        if not symbol:
            return None

        if price is None or volume is None:
            return None

        timestamp = _parse_timestamp(raw_timestamp)

        side = _normalize_side(raw_side)

        normalized = {
            "symbol": symbol,
            "price": price,
            "price_key": _price_key(price),
            "volume": volume,
            "side": side,
            "timestamp": timestamp,
            "datetime": _timestamp_to_iso(timestamp),
            "trade_id": trade_id,
            "notional": price * volume,
        }

        return normalized

    # ========================================================
    # 6. NHẬN GIAO DỊCH
    # ========================================================

    def add_trade(self, trade: dict[str, Any]) -> bool:
        """
        Thêm một giao dịch vào engine.

        Returns:
            True: giao dịch được chấp nhận.
            False: dữ liệu không hợp lệ hoặc giao dịch trùng.
        """
        with self._lock:
            self._received_count += 1

        normalized = self._normalize_trade(trade)

        if normalized is None:
            with self._lock:
                self._rejected_count += 1

            logger.debug("Bỏ qua giao dịch không hợp lệ.")
            return False

        symbol = normalized["symbol"]
        trade_id = normalized["trade_id"]

        with self._lock:
            self._initialize_symbol_locked(symbol)

            # Chống trùng theo trade ID khi nguồn cung cấp ID.
            if trade_id:
                seen = self._seen_trade_ids[symbol]

                if trade_id in seen:
                    self._duplicate_count += 1
                    return False

                seen[trade_id] = None

                # Giới hạn bộ nhớ chống trùng.
                while len(seen) > self.max_trades * 2:
                    seen.popitem(last=False)

            self._update_price_level_locked(normalized)
            self._update_candle_locked(normalized)
            self._update_stats_locked(normalized)

            if self.keep_trade_tape:
                self._trades[symbol].append(normalized)

                if len(self._trades[symbol]) > self.max_trades:
                    excess = len(self._trades[symbol]) - self.max_trades

                    del self._trades[symbol][:excess]

            self._accepted_count += 1
            self._last_trade_at = normalized["timestamp"]

        return True

    def add_trades(self, trades: list[dict[str, Any]]) -> dict[str, int]:
        """Thêm nhiều giao dịch và trả về thống kê xử lý."""
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
    # 7. TỔNG HỢP VOLUME THEO MỨC GIÁ
    # ========================================================

    def _update_price_level_locked(
        self,
        trade: dict[str, Any],
    ) -> None:
        """Cập nhật thống kê mức giá; phải gọi khi đang giữ lock."""
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

        # Delta chỉ hợp lệ khi toàn bộ volume tại mức giá
        # được phân loại thành BUY hoặc SELL.
        if level["unknown_volume"] <= 0:
            level["delta"] = (
                level["buy_volume"] - level["sell_volume"]
            )

            total_directional = (
                level["buy_volume"] + level["sell_volume"]
            )

            if total_directional > 0:
                level["delta_percent"] = (
                    level["delta"] / total_directional
                ) * 100.0

            else:
                level["delta_percent"] = None

        else:
            level["delta"] = None
            level["delta_percent"] = None

        # Giới hạn số mức giá để tránh tăng RAM vô hạn.
        # Nếu vượt giới hạn, bỏ mức giá có tổng volume thấp nhất.
        if len(levels) > self.max_price_levels:
            lowest_key = min(
                levels,
                key=lambda key: levels[key]["total_volume"],
            )

            levels.pop(lowest_key, None)

    # ========================================================
    # 8. TỔNG HỢP NẾN OHLCV
    # ========================================================

    def _update_candle_locked(
        self,
        trade: dict[str, Any],
    ) -> None:
        """Cập nhật nến theo khoảng thời gian đã cấu hình."""
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
            }

        candle = candles[bucket]

        candle["high"] = max(candle["high"], price)
        candle["low"] = min(candle["low"], price)
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

        # Sắp xếp theo bucket và giới hạn số nến.
        candles.move_to_end(bucket)

        while len(candles) > self.max_candles:
            candles.popitem(last=False)

        self._recalculate_cumulative_delta_locked(symbol)

    def _recalculate_cumulative_delta_locked(
        self,
        symbol: str,
    ) -> None:
        """
        Tính Cumulative Delta theo thứ tự thời gian.

        Nếu có bất kỳ nến nào chứa unknown volume, không thể xác nhận
        Cumulative Delta hoàn chỉnh cho toàn bộ chuỗi nến.
        """
        running_delta = 0.0
        all_complete = True

        for candle in self._candles[symbol].values():
            if candle["delta"] is None:
                candle["cumulative_delta"] = None
                all_complete = False
                continue

            running_delta += candle["delta"]
            candle["cumulative_delta"] = running_delta

        self._cumulative_delta[symbol] = running_delta
        self._delta_complete[symbol] = all_complete

    # ========================================================
    # 9. CẬP NHẬT THỐNG KÊ TỔNG
    # ========================================================

    def _update_stats_locked(
        self,
        trade: dict[str, Any],
    ) -> None:
        """Cập nhật tổng thống kê theo mã."""
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

        stats["last_price"] = price
        stats["last_volume"] = volume
        stats["last_trade_at"] = timestamp

        if stats["first_trade_at"] is None:
            stats["first_trade_at"] = timestamp

    # ========================================================
    # 10. LẤY FOOTPRINT THEO MỨC GIÁ
    # ========================================================

    def get_footprint(
        self,
        symbol: str,
        *,
        sort: str = "price",
        descending: bool = False,
    ) -> dict[str, Any]:
        """
        Lấy Footprint tổng hợp của một mã.

        sort:
            price  : sắp xếp theo mức giá.
            volume : sắp xếp theo tổng volume.
            delta  : sắp xếp theo Delta đã xác định.

        descending:
            True để sắp xếp giảm dần.
        """
        normalized_symbol = _normalize_symbol(symbol)

        if not normalized_symbol:
            return {
                "success": False,
                "error": "Invalid symbol",
                "symbol": symbol,
            }

        with self._lock:
            self._initialize_symbol_locked(normalized_symbol)

            stats = dict(self._stats[normalized_symbol])

            levels = [
                dict(level)
                for level in self._price_levels[
                    normalized_symbol
                ].values()
            ]

            cumulative_delta = self._cumulative_delta[
                normalized_symbol
            ]

            delta_complete = self._delta_complete[
                normalized_symbol
            ]

            candle_count = len(
                self._candles[normalized_symbol]
            )

        if sort == "volume":
            levels.sort(
                key=lambda item: item["total_volume"],
                reverse=descending,
            )

        elif sort == "delta":
            # Các mức giá chưa xác định Delta sẽ được đưa xuống cuối.
            levels.sort(
                key=lambda item: (
                    item["delta"] is None,
                    item["delta"] or 0.0,
                ),
                reverse=descending,
            )

        else:
            levels.sort(
                key=lambda item: item["price"],
                reverse=descending,
            )

        poc = None

        if levels:
            # POC = mức giá có total_volume cao nhất.
            poc = max(
                levels,
                key=lambda item: item["total_volume"],
            )

        directional_volume = (
            stats["buy_volume"] + stats["sell_volume"]
        )

        if stats["unknown_volume"] <= 0:
            total_delta: Optional[float] = (
                stats["buy_volume"] - stats["sell_volume"]
            )
        else:
            total_delta = None

        delta_percent = None

        if total_delta is not None and directional_volume > 0:
            delta_percent = (
                total_delta / directional_volume
            ) * 100.0

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
                "first_trade_at": (
                    _timestamp_to_iso(stats["first_trade_at"])
                    if stats["first_trade_at"] is not None
                    else None
                ),
                "last_trade_at": (
                    _timestamp_to_iso(stats["last_trade_at"])
                    if stats["last_trade_at"] is not None
                    else None
                ),
                "delta": total_delta,
                "delta_percent": delta_percent,
                "cumulative_delta": (
                    cumulative_delta
                    if delta_complete
                    else None
                ),
                "delta_complete": (
                    stats["unknown_volume"] <= 0
                ),
                "cumulative_delta_complete": delta_complete,
                "price_level_count": len(levels),
                "candle_count": candle_count,
            },
            "poc": dict(poc) if poc else None,
            "price_levels": levels,
        }

    # ========================================================
    # 11. LẤY TRADE TAPE
    # ========================================================

    def get_trades(
        self,
        symbol: str,
        *,
        limit: int = 100,
        newest_first: bool = True,
    ) -> dict[str, Any]:
        """Lấy danh sách giao dịch gần nhất của một mã."""
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

            trades = list(
                self._trades[normalized_symbol]
            )

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
    # 12. LẤY NẾN
    # ========================================================

    def get_candles(
        self,
        symbol: str,
        *,
        limit: int = 100,
        newest_first: bool = False,
    ) -> dict[str, Any]:
        """Lấy các nến đã tổng hợp từ dữ liệu giao dịch nhận được."""
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
                dict(candle)
                for candle in self._candles[
                    normalized_symbol
                ].values()
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
    # 13. LẤY DANH SÁCH MÃ
    # ========================================================

    def get_symbols(self) -> list[str]:
        """Trả về danh sách mã đã được đăng ký hoặc nhận dữ liệu."""
        with self._lock:
            return sorted(self._stats.keys())

    # ========================================================
    # 14. TRẠNG THÁI ENGINE
    # ========================================================

    def get_status(self) -> dict[str, Any]:
        """Trả về tình trạng engine và thống kê nhận dữ liệu."""
        with self._lock:
            symbols: dict[str, Any] = {}

            for symbol, stats in self._stats.items():
                symbols[symbol] = {
                    "trade_count": stats["trade_count"],
                    "total_volume": stats["total_volume"],
                    "last_price": stats["last_price"],
                    "last_trade_at": (
                        _timestamp_to_iso(stats["last_trade_at"])
                        if stats["last_trade_at"] is not None
                        else None
                    ),
                    "price_level_count": len(
                        self._price_levels[symbol]
                    ),
                    "candle_count": len(
                        self._candles[symbol]
                    ),
                }

            return {
                "success": True,
                "engine": "DNSE Footprint Engine",
                "storage": "RAM",
                "started_at": _timestamp_to_iso(
                    self._started_at
                ),
                "uptime_seconds": max(
                    0,
                    int(time.time() - self._started_at),
                ),
                "last_trade_at": (
                    _timestamp_to_iso(self._last_trade_at)
                    if self._last_trade_at is not None
                    else None
                ),
                "received_count": self._received_count,
                "accepted_count": self._accepted_count,
                "duplicate_count": self._duplicate_count,
                "rejected_count": self._rejected_count,
                "trade_tape_enabled": self.keep_trade_tape,
                "candle_seconds": self.candle_seconds,
                "symbols": symbols,
            }

    # ========================================================
    # 15. XÓA DỮ LIỆU
    # ========================================================

    def clear_symbol(self, symbol: str) -> bool:
        """
        Xóa dữ liệu của một mã khỏi RAM.

        Không thể hoàn tác.
        Không xóa dữ liệu trong Supabase.
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
        Xóa toàn bộ dữ liệu Footprint trong RAM.

        Chỉ sử dụng khi thật sự muốn bắt đầu lại phiên thu thập.
        """
        with self._lock:
            symbols = list(self._stats.keys())

            for symbol in symbols:
                self._price_levels[symbol] = {}
                self._trades[symbol] = []
                self._candles[symbol] = OrderedDict()
                self._seen_trade_ids[symbol] = OrderedDict()

                self._stats[symbol] = {
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

                self._cumulative_delta[symbol] = 0.0
                self._delta_complete[symbol] = True

            self._received_count = 0
            self._accepted_count = 0
            self._duplicate_count = 0
            self._rejected_count = 0
            self._last_trade_at = None
            self._started_at = time.time()

        logger.warning("Đã reset toàn bộ Footprint Engine trong RAM.")


# ============================================================
# 16. TẠO INSTANCE DÙNG CHUNG
# ============================================================

# Có thể import instance này từ routes.py hoặc dnse_stream.py.
#
# Ví dụ:
# from scripts.footprint.engine import footprint_engine
#
# Không tạo một FootprintEngine mới ở mỗi request Flask,
# nếu không dữ liệu sẽ bị phân tán giữa nhiều instance.

footprint_engine = FootprintEngine()


# Alias để hỗ trợ code cũ nếu trước đây bạn đã import tên engine.
engine = footprint_engine
