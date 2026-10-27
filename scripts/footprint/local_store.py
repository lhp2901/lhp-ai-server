from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple


logger = logging.getLogger(__name__)


# ============================================================
# CONSTANTS
# ============================================================

SCHEMA_VERSION = 1

VALID_SIDES = {
    "BUY",
    "SELL",
    "UNKNOWN",
}

DEFAULT_BUSY_TIMEOUT_MS = 10_000

DEFAULT_MAX_BATCH_SIZE = 5_000

DEFAULT_STATE_KEY = "footprint_engine"


# ============================================================
# EXCEPTIONS
# ============================================================

class LocalStoreError(RuntimeError):
    """Base exception for local SQLite storage."""


class SchemaVersionError(LocalStoreError):
    """Raised when the database schema is newer than this code supports."""


class TradeValidationError(ValueError):
    """Raised when trade data cannot be normalized."""


class CandleValidationError(ValueError):
    """Raised when candle data cannot be normalized."""


# ============================================================
# BASIC HELPERS
# ============================================================

def _utc_now_ms() -> int:
    """Return current UTC time in milliseconds."""
    return int(time.time() * 1000)


def _json_default(value: Any) -> Any:
    """Convert common Python objects to JSON-compatible values."""

    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)

        return value.isoformat()

    if isinstance(value, Path):
        return str(value)

    if isinstance(value, set):
        return list(value)

    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass

    return str(value)


def _json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_json_default,
    )


def _json_loads(value: Optional[str], default: Any = None) -> Any:
    if value is None:
        return default

    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _to_float(
    value: Any,
    *,
    field: str,
    default: Optional[float] = None,
) -> Optional[float]:
    if value is None or value == "":
        return default

    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            f"Invalid numeric value for {field}: {value!r}"
        ) from exc

    if result != result or result in (float("inf"), float("-inf")):
        raise ValueError(f"Non-finite numeric value for {field}")

    return result


def _to_int(
    value: Any,
    *,
    field: str,
    default: Optional[int] = None,
) -> Optional[int]:
    if value is None or value == "":
        return default

    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            f"Invalid integer value for {field}: {value!r}"
        ) from exc

    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"Non-finite integer value for {field}")

    if not number.is_integer():
        raise ValueError(
            f"{field} must be an integer, got {value!r}"
        )

    return int(number)


def _normalize_symbol(value: Any) -> str:
    if value is None:
        raise ValueError("symbol is required")

    symbol = str(value).strip().upper()

    if not symbol:
        raise ValueError("symbol cannot be empty")

    return symbol


def _first_value(
    data: Dict[str, Any],
    keys: Sequence[str],
    default: Any = None,
) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]

    return default


def _normalize_timestamp(value: Any) -> int:
    """
    Normalize a timestamp to Unix milliseconds.

    Supported input:
      - Unix seconds
      - Unix milliseconds
      - Unix microseconds
      - Unix nanoseconds
      - ISO-8601 datetime strings

    Missing timestamps are rejected rather than silently assigned
    the current time. The current time is not necessarily the time
    when the original market trade occurred.
    """

    if value is None or value == "":
        raise ValueError("timestamp is required")

    if isinstance(value, datetime):
        dt = value

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return int(dt.timestamp() * 1000)

    if isinstance(value, str):
        text = value.strip()

        if not text:
            raise ValueError("timestamp cannot be empty")

        # First try a numeric timestamp.
        try:
            number = float(text)
        except ValueError:
            number = None

        if number is None:
            iso_text = text

            if iso_text.endswith("Z"):
                iso_text = iso_text[:-1] + "+00:00"

            try:
                dt = datetime.fromisoformat(iso_text)
            except ValueError as exc:
                raise ValueError(
                    f"Unsupported timestamp format: {value!r}"
                ) from exc

            if dt.tzinfo is None:
                # Naive ISO timestamps are interpreted as UTC.
                # Change this policy if the provider documents
                # another timezone convention.
                dt = dt.replace(tzinfo=timezone.utc)

            return int(dt.timestamp() * 1000)

    else:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                f"Invalid timestamp: {value!r}"
            ) from exc

    if number is None:
        raise ValueError(f"Invalid timestamp: {value!r}")

    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"Non-finite timestamp: {value!r}")

    # Heuristic based on common Unix timestamp magnitudes.
    magnitude = abs(number)

    if magnitude < 100_000_000_000:
        # Seconds -> milliseconds.
        number *= 1_000

    elif magnitude < 100_000_000_000_000:
        # Already milliseconds.
        pass

    elif magnitude < 100_000_000_000_000_000:
        # Microseconds -> milliseconds.
        number /= 1_000

    else:
        # Nanoseconds -> milliseconds.
        number /= 1_000_000

    timestamp_ms = int(number)

    if timestamp_ms <= 0:
        raise ValueError(
            f"Timestamp must be positive: {value!r}"
        )

    return timestamp_ms


# ============================================================
# TRADE NORMALIZATION
# ============================================================

def normalize_trade(
    trade: Dict[str, Any],
    *,
    default_symbol: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Normalize a trade payload into the internal storage format.

    The original payload is preserved in raw_json.

    Provider-specific field aliases should be added only after
    checking actual DNSE stream payloads.
    """

    if not isinstance(trade, dict):
        raise TradeValidationError(
            "Trade must be a dictionary"
        )

    symbol_value = _first_value(
        trade,
        (
            "symbol",
            "ticker",
            "stock_code",
            "stockCode",
            "code",
            "instrument",
        ),
        default_symbol,
    )

    try:
        symbol = _normalize_symbol(symbol_value)

        price = _to_float(
            _first_value(
                trade,
                (
                    "price",
                    "match_price",
                    "matchPrice",
                    "matchedPrice",
                    "last_price",
                    "lastPrice",
                ),
            ),
            field="price",
        )

        volume = _to_float(
            _first_value(
                trade,
                (
                    "volume",
                    "quantity",
                    "qty",
                    "match_volume",
                    "matchVolume",
                    "matchedVolume",
                    "matched_quantity",
                    "matchedQuantity",
                ),
            ),
            field="volume",
        )

        timestamp_value = _first_value(
            trade,
            (
                "timestamp",
                "time",
                "trade_time",
                "tradeTime",
                "match_time",
                "matchTime",
                "matched_at",
                "matchedAt",
                "created_at",
                "createdAt",
                "ts",
            ),
        )

        timestamp_ms = _normalize_timestamp(timestamp_value)

    except (TypeError, ValueError) as exc:
        raise TradeValidationError(str(exc)) from exc

    if price is None or price <= 0:
        raise TradeValidationError(
            f"price must be positive, got {price!r}"
        )

    if volume is None or volume <= 0:
        raise TradeValidationError(
            f"volume must be positive, got {volume!r}"
        )

    raw_side = _first_value(
        trade,
        (
            "side",
            "aggressor",
            "aggressor_side",
            "aggressorSide",
            "match_side",
            "matchSide",
            "buy_sell",
            "buySell",
            "direction",
        ),
        "UNKNOWN",
    )

    side_text = str(raw_side).strip().upper()

    side_aliases = {
        "B": "BUY",
        "BUY": "BUY",
        "BID": "BUY",
        "MUA": "BUY",
        "1": "BUY",
        "S": "SELL",
        "SELL": "SELL",
        "ASK": "SELL",
        "BAN": "SELL",
        "-1": "SELL",
        "UNKNOWN": "UNKNOWN",
        "": "UNKNOWN",
        "NONE": "UNKNOWN",
        "NULL": "UNKNOWN",
    }

    side = side_aliases.get(side_text, "UNKNOWN")

    provider_trade_id = _first_value(
        trade,
        (
            "trade_id",
            "tradeId",
            "match_id",
            "matchId",
            "execution_id",
            "executionId",
            "transaction_id",
            "transactionId",
            "id",
        ),
    )

    if provider_trade_id is not None:
        provider_trade_id = str(provider_trade_id).strip()

        if not provider_trade_id:
            provider_trade_id = None

    received_at_ms = _utc_now_ms()

    return {
        "symbol": symbol,
        "trade_id": provider_trade_id,
        "timestamp": timestamp_ms,
        "price": float(price),
        "volume": float(volume),
        "side": side,
        "received_at": received_at_ms,
        "raw_json": _json_dumps(trade),
    }


# ============================================================
# CANDLE NORMALIZATION
# ============================================================

def normalize_candle(
    candle: Dict[str, Any],
    *,
    default_symbol: Optional[str] = None,
    default_timeframe: str = "1m",
) -> Dict[str, Any]:
    if not isinstance(candle, dict):
        raise CandleValidationError(
            "Candle must be a dictionary"
        )

    try:
        symbol = _normalize_symbol(
            _first_value(
                candle,
                (
                    "symbol",
                    "ticker",
                    "stock_code",
                    "stockCode",
                    "code",
                ),
                default_symbol,
            )
        )

        timeframe = str(
            _first_value(
                candle,
                (
                    "timeframe",
                    "interval",
                    "resolution",
                    "period",
                ),
                default_timeframe,
            )
        ).strip()

        if not timeframe:
            raise ValueError("timeframe cannot be empty")

        timestamp_ms = _normalize_timestamp(
            _first_value(
                candle,
                (
                    "timestamp",
                    "time",
                    "start_time",
                    "startTime",
                    "open_time",
                    "openTime",
                    "ts",
                ),
            )
        )

        open_price = _to_float(
            _first_value(candle, ("open", "o")),
            field="open",
        )

        high_price = _to_float(
            _first_value(candle, ("high", "h")),
            field="high",
        )

        low_price = _to_float(
            _first_value(candle, ("low", "l")),
            field="low",
        )

        close_price = _to_float(
            _first_value(candle, ("close", "c")),
            field="close",
        )

        volume = _to_float(
            _first_value(
                candle,
                (
                    "volume",
                    "vol",
                    "total_volume",
                    "totalVolume",
                ),
            ),
            field="volume",
            default=0.0,
        )

        trade_count = _to_int(
            _first_value(
                candle,
                (
                    "trade_count",
                    "tradeCount",
                    "count",
                ),
            ),
            field="trade_count",
            default=0,
        )

    except (TypeError, ValueError) as exc:
        raise CandleValidationError(str(exc)) from exc

    prices = {
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "close": close_price,
    }

    for name, value in prices.items():
        if value is None or value <= 0:
            raise CandleValidationError(
                f"{name} must be positive"
            )

    if high_price < low_price:
        raise CandleValidationError(
            "high must be greater than or equal to low"
        )

    if high_price < max(open_price, close_price):
        raise CandleValidationError(
            "high cannot be lower than open or close"
        )

    if low_price > min(open_price, close_price):
        raise CandleValidationError(
            "low cannot be higher than open or close"
        )

    if volume is None or volume < 0:
        raise CandleValidationError(
            "volume must be non-negative"
        )

    if trade_count is None or trade_count < 0:
        raise CandleValidationError(
            "trade_count must be a non-negative integer"
        )

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "timestamp": timestamp_ms,
        "open": float(open_price),
        "high": float(high_price),
        "low": float(low_price),
        "close": float(close_price),
        "volume": float(volume),
        "trade_count": int(trade_count),
        "raw_json": _json_dumps(candle),
    }


# ============================================================
# SQLITE STORE
# ============================================================

class LocalStore:
    """
    SQLite persistence for the Footprint system.

    Design principles:
      - One connection per operation.
      - WAL for concurrent readers.
      - Explicit transactions for writes.
      - Parameterized SQL statements.
      - No automatic data deletion.
      - Stable provider IDs used for trade deduplication.

    Important:
      If trade_id is missing, the store does not invent an ID.
      Such trades cannot be deduplicated reliably across reconnects.
    """

    def __init__(
        self,
        db_path: Optional[str | Path] = None,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
    ) -> None:
        self.db_path = Path(
            db_path or default_database_path()
        ).expanduser().resolve()

        self.busy_timeout_ms = max(
            1,
            int(busy_timeout_ms),
        )

        self.max_batch_size = max(
            1,
            int(max_batch_size),
        )

        self._lock = threading.RLock()
        self._initialized = False
        self._closed = False

        self.db_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.initialize()

    # --------------------------------------------------------
    # CONNECTION MANAGEMENT
    # --------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self._closed:
            raise LocalStoreError(
                "LocalStore has been closed"
            )

        connection = sqlite3.connect(
            str(self.db_path),
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )

        connection.row_factory = sqlite3.Row

        connection.execute(
            f"PRAGMA busy_timeout = {self.busy_timeout_ms}"
        )

        connection.execute(
            "PRAGMA foreign_keys = ON"
        )

        connection.execute(
            "PRAGMA synchronous = NORMAL"
        )

        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()

        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(
        self,
        connection: sqlite3.Connection,
        *,
        immediate: bool = True,
    ) -> Iterator[sqlite3.Connection]:
        """
        Explicit transaction context.

        Roll back the whole transaction when an exception occurs.
        """

        connection.execute(
            "BEGIN IMMEDIATE" if immediate else "BEGIN"
        )

        try:
            yield connection
            connection.commit()

        except Exception:
            connection.rollback()
            raise

    # --------------------------------------------------------
    # INITIALIZATION AND SCHEMA
    # --------------------------------------------------------

    def initialize(self) -> None:
        """
        Initialize database schema.

        The database is rejected if its schema version is newer
        than the code's supported version. This avoids silently
        downgrading or overwriting schema metadata.
        """

        with self._lock:
            if self._initialized:
                return

            if self._closed:
                raise LocalStoreError(
                    "Cannot initialize a closed LocalStore"
                )

            with self._connection() as connection:
                # WAL is persistent at the database level.
                connection.execute(
                    "PRAGMA journal_mode = WAL"
                )

                with self._transaction(connection):
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS schema_meta (
                            key TEXT PRIMARY KEY,
                            value TEXT NOT NULL,
                            updated_at INTEGER NOT NULL
                        )
                        """
                    )

                    version_row = connection.execute(
                        """
                        SELECT value
                        FROM schema_meta
                        WHERE key = 'schema_version'
                        """
                    ).fetchone()

                    current_version = (
                        int(version_row["value"])
                        if version_row is not None
                        else 0
                    )

                    if current_version > SCHEMA_VERSION:
                        raise SchemaVersionError(
                            "Database schema version "
                            f"{current_version} is newer than "
                            f"supported version {SCHEMA_VERSION}. "
                            "Do not downgrade this database."
                        )

                    # Version 0 -> 1.
                    if current_version < 1:
                        self._create_schema_v1(connection)

                        connection.execute(
                            """
                            INSERT INTO schema_meta (
                                key,
                                value,
                                updated_at
                            )
                            VALUES (?, ?, ?)
                            ON CONFLICT(key) DO UPDATE SET
                                value = excluded.value,
                                updated_at = excluded.updated_at
                            """,
                            (
                                "schema_version",
                                str(SCHEMA_VERSION),
                                _utc_now_ms(),
                            ),
                        )

                    else:
                        # Validate the expected schema is present.
                        self._create_schema_v1(connection)

                        connection.execute(
                            """
                            INSERT INTO schema_meta (
                                key,
                                value,
                                updated_at
                            )
                            VALUES (?, ?, ?)
                            ON CONFLICT(key) DO UPDATE SET
                                updated_at = excluded.updated_at
                            """,
                            (
                                "schema_version",
                                str(SCHEMA_VERSION),
                                _utc_now_ms(),
                            ),
                        )

            self._initialized = True

            logger.info(
                "Footprint SQLite initialized: %s",
                self.db_path,
            )

    @staticmethod
    def _create_schema_v1(
        connection: sqlite3.Connection,
    ) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS footprint_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                symbol TEXT NOT NULL,
                trade_id TEXT,

                timestamp INTEGER NOT NULL,
                price REAL NOT NULL CHECK(price > 0),
                volume REAL NOT NULL CHECK(volume > 0),

                side TEXT NOT NULL DEFAULT 'UNKNOWN'
                    CHECK(side IN ('BUY', 'SELL', 'UNKNOWN')),

                received_at INTEGER NOT NULL,

                raw_json TEXT NOT NULL,

                created_at INTEGER NOT NULL
            )
            """
        )

        # Deduplicate only trades with a provider-supplied ID.
        # Trades without trade_id remain insertable.
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                ux_footprint_trades_symbol_trade_id
            ON footprint_trades(symbol, trade_id)
            WHERE trade_id IS NOT NULL
            """
        )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS
                ix_footprint_trades_symbol_timestamp
            ON footprint_trades(symbol, timestamp)
            """
        )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS
                ix_footprint_trades_timestamp
            ON footprint_trades(timestamp)
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS footprint_candles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                timestamp INTEGER NOT NULL,

                open REAL NOT NULL CHECK(open > 0),
                high REAL NOT NULL CHECK(high > 0),
                low REAL NOT NULL CHECK(low > 0),
                close REAL NOT NULL CHECK(close > 0),

                volume REAL NOT NULL DEFAULT 0
                    CHECK(volume >= 0),

                trade_count INTEGER NOT NULL DEFAULT 0
                    CHECK(trade_count >= 0),

                raw_json TEXT NOT NULL,

                updated_at INTEGER NOT NULL,

                UNIQUE(symbol, timeframe, timestamp)
            )
            """
        )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS
                ix_footprint_candles_symbol_time
            ON footprint_candles(
                symbol,
                timeframe,
                timestamp
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS footprint_state (
                key TEXT PRIMARY KEY,

                value_json TEXT NOT NULL,

                updated_at INTEGER NOT NULL
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS footprint_stream_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                event_type TEXT NOT NULL,

                symbol TEXT,

                timestamp INTEGER NOT NULL,

                payload_json TEXT NOT NULL
            )
            """
        )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS
                ix_footprint_stream_events_timestamp
            ON footprint_stream_events(timestamp)
            """
        )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS
                ix_footprint_stream_events_type_timestamp
            ON footprint_stream_events(event_type, timestamp)
            """
        )

    # --------------------------------------------------------
    # TRADE WRITE
    # --------------------------------------------------------

    def save_trade(
        self,
        trade: Dict[str, Any],
        *,
        default_symbol: Optional[str] = None,
    ) -> bool:
        """
        Save one trade.

        Returns:
            True  -> inserted.
            False -> duplicate provider ID.

        Invalid trade data raises TradeValidationError.
        """

        normalized = normalize_trade(
            trade,
            default_symbol=default_symbol,
        )

        return self._insert_normalized_trade(normalized)

    def _insert_normalized_trade(
        self,
        trade: Dict[str, Any],
    ) -> bool:
        now_ms = _utc_now_ms()

        with self._lock:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO footprint_trades (
                        symbol,
                        trade_id,
                        timestamp,
                        price,
                        volume,
                        side,
                        received_at,
                        raw_json,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, trade_id)
                    WHERE trade_id IS NOT NULL
                    DO NOTHING
                    """,
                    (
                        trade["symbol"],
                        trade["trade_id"],
                        trade["timestamp"],
                        trade["price"],
                        trade["volume"],
                        trade["side"],
                        trade["received_at"],
                        trade["raw_json"],
                        now_ms,
                    ),
                )

                return cursor.rowcount == 1

    def save_trades(
        self,
        trades: Iterable[Dict[str, Any]],
        *,
        default_symbol: Optional[str] = None,
        strict: bool = False,
    ) -> Dict[str, int]:
        """
        Save trades in batches.

        Returns:
            {
                "inserted": number of inserted trades,
                "duplicates": number of duplicate trades,
                "rejected": number of invalid trades,
            }

        With strict=True, invalid input raises an exception and
        the current database transaction is rolled back.
        """

        inserted = 0
        duplicates = 0
        rejected = 0

        batch: List[Dict[str, Any]] = []

        def flush_batch(
            items: List[Dict[str, Any]],
        ) -> Tuple[int, int]:
            if not items:
                return 0, 0

            local_inserted = 0
            local_duplicates = 0
            now_ms = _utc_now_ms()

            with self._lock:
                with self._connection() as connection:
                    with self._transaction(connection):
                        for item in items:
                            cursor = connection.execute(
                                """
                                INSERT INTO footprint_trades (
                                    symbol,
                                    trade_id,
                                    timestamp,
                                    price,
                                    volume,
                                    side,
                                    received_at,
                                    raw_json,
                                    created_at
                                )
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                ON CONFLICT(symbol, trade_id)
                                WHERE trade_id IS NOT NULL
                                DO NOTHING
                                """,
                                (
                                    item["symbol"],
                                    item["trade_id"],
                                    item["timestamp"],
                                    item["price"],
                                    item["volume"],
                                    item["side"],
                                    item["received_at"],
                                    item["raw_json"],
                                    now_ms,
                                ),
                            )

                            if cursor.rowcount == 1:
                                local_inserted += 1
                            else:
                                local_duplicates += 1

            return local_inserted, local_duplicates

        for trade in trades:
            try:
                normalized = normalize_trade(
                    trade,
                    default_symbol=default_symbol,
                )

            except (TradeValidationError, TypeError, ValueError):
                rejected += 1

                if strict:
                    raise

                continue

            batch.append(normalized)

            if len(batch) >= self.max_batch_size:
                batch_inserted, batch_duplicates = flush_batch(batch)

                inserted += batch_inserted
                duplicates += batch_duplicates

                batch.clear()

        if batch:
            batch_inserted, batch_duplicates = flush_batch(batch)

            inserted += batch_inserted
            duplicates += batch_duplicates

        return {
            "inserted": inserted,
            "duplicates": duplicates,
            "rejected": rejected,
        }

    # --------------------------------------------------------
    # TRADE READ
    # --------------------------------------------------------

    def load_trades(
        self,
        symbol: Optional[str] = None,
        *,
        limit: int = 1_000,
        before_timestamp: Optional[int] = None,
        after_timestamp: Optional[int] = None,
        order: str = "DESC",
        include_raw: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Load trades.

        Default order is newest first, useful for API/UI display.

        For engine recovery or replay, use:
            order="ASC"

        symbol=None loads trades across all symbols.

        Timestamp filters are inclusive:
            after_timestamp  -> timestamp >= value
            before_timestamp -> timestamp <= value
        """

        limit = int(limit)

        if limit <= 0:
            return []

        order = str(order).upper().strip()

        if order not in {"ASC", "DESC"}:
            raise ValueError("order must be ASC or DESC")

        conditions: List[str] = []
        params: List[Any] = []

        if symbol is not None:
            conditions.append("symbol = ?")
            params.append(_normalize_symbol(symbol))

        if after_timestamp is not None:
            conditions.append("timestamp >= ?")
            params.append(_normalize_timestamp(after_timestamp))

        if before_timestamp is not None:
            conditions.append("timestamp <= ?")
            params.append(_normalize_timestamp(before_timestamp))

        where_sql = (
            "WHERE " + " AND ".join(conditions)
            if conditions
            else ""
        )

        sql = f"""
            SELECT
                id,
                symbol,
                trade_id,
                timestamp,
                price,
                volume,
                side,
                received_at,
                raw_json,
                created_at
            FROM footprint_trades
            {where_sql}
            ORDER BY timestamp {order}, id {order}
            LIMIT ?
        """

        params.append(limit)

        with self._connection() as connection:
            rows = connection.execute(
                sql,
                params,
            ).fetchall()

        result: List[Dict[str, Any]] = []

        for row in rows:
            item = {
                "id": row["id"],
                "symbol": row["symbol"],
                "trade_id": row["trade_id"],
                "timestamp": row["timestamp"],
                "price": row["price"],
                "volume": row["volume"],
                "side": row["side"],
                "received_at": row["received_at"],
                "created_at": row["created_at"],
            }

            if include_raw:
                item["raw"] = _json_loads(
                    row["raw_json"],
                    default={},
                )

            result.append(item)

        return result

    def count_trades(
        self,
        symbol: Optional[str] = None,
        *,
        after_timestamp: Optional[int] = None,
        before_timestamp: Optional[int] = None,
    ) -> int:
        conditions: List[str] = []
        params: List[Any] = []

        if symbol is not None:
            conditions.append("symbol = ?")
            params.append(_normalize_symbol(symbol))

        if after_timestamp is not None:
            conditions.append("timestamp >= ?")
            params.append(_normalize_timestamp(after_timestamp))

        if before_timestamp is not None:
            conditions.append("timestamp <= ?")
            params.append(_normalize_timestamp(before_timestamp))

        where_sql = (
            "WHERE " + " AND ".join(conditions)
            if conditions
            else ""
        )

        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM footprint_trades
                {where_sql}
                """,
                params,
            ).fetchone()

        return int(row["total"])

    def get_latest_trade_timestamp(
        self,
        symbol: Optional[str] = None,
    ) -> Optional[int]:
        conditions: List[str] = []
        params: List[Any] = []

        if symbol is not None:
            conditions.append("symbol = ?")
            params.append(_normalize_symbol(symbol))

        where_sql = (
            "WHERE " + " AND ".join(conditions)
            if conditions
            else ""
        )

        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT MAX(timestamp) AS latest_timestamp
                FROM footprint_trades
                {where_sql}
                """,
                params,
            ).fetchone()

        value = row["latest_timestamp"]

        return int(value) if value is not None else None

    def get_oldest_trade_timestamp(
        self,
        symbol: Optional[str] = None,
    ) -> Optional[int]:
        conditions: List[str] = []
        params: List[Any] = []

        if symbol is not None:
            conditions.append("symbol = ?")
            params.append(_normalize_symbol(symbol))

        where_sql = (
            "WHERE " + " AND ".join(conditions)
            if conditions
            else ""
        )

        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT MIN(timestamp) AS oldest_timestamp
                FROM footprint_trades
                {where_sql}
                """,
                params,
            ).fetchone()

        value = row["oldest_timestamp"]

        return int(value) if value is not None else None

    # --------------------------------------------------------
    # CANDLE WRITE
    # --------------------------------------------------------

    def save_candle(
        self,
        candle: Dict[str, Any],
        *,
        default_symbol: Optional[str] = None,
        default_timeframe: str = "1m",
    ) -> bool:
        """
        Insert or update a candle.

        Returns True when the candle is saved successfully.
        Existing candles with the same symbol, timeframe and
        timestamp are updated.
        """

        normalized = normalize_candle(
            candle,
            default_symbol=default_symbol,
            default_timeframe=default_timeframe,
        )

        now_ms = _utc_now_ms()

        with self._lock:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO footprint_candles (
                        symbol,
                        timeframe,
                        timestamp,
                        open,
                        high,
                        low,
                        close,
                        volume,
                        trade_count,
                        raw_json,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, timeframe, timestamp)
                    DO UPDATE SET
                        open = excluded.open,
                        high = excluded.high,
                        low = excluded.low,
                        close = excluded.close,
                        volume = excluded.volume,
                        trade_count = excluded.trade_count,
                        raw_json = excluded.raw_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        normalized["symbol"],
                        normalized["timeframe"],
                        normalized["timestamp"],
                        normalized["open"],
                        normalized["high"],
                        normalized["low"],
                        normalized["close"],
                        normalized["volume"],
                        normalized["trade_count"],
                        normalized["raw_json"],
                        now_ms,
                    ),
                )

        return True

    def save_candles(
        self,
        candles: Iterable[Dict[str, Any]],
        *,
        default_symbol: Optional[str] = None,
        default_timeframe: str = "1m",
        strict: bool = False,
    ) -> Dict[str, int]:
        inserted_or_updated = 0
        rejected = 0

        normalized_batch: List[Dict[str, Any]] = []

        for candle in candles:
            try:
                normalized_batch.append(
                    normalize_candle(
                        candle,
                        default_symbol=default_symbol,
                        default_timeframe=default_timeframe,
                    )
                )

            except (CandleValidationError, TypeError, ValueError):
                rejected += 1

                if strict:
                    raise

        if not normalized_batch:
            return {
                "saved": 0,
                "rejected": rejected,
            }

        now_ms = _utc_now_ms()

        with self._lock:
            with self._connection() as connection:
                with self._transaction(connection):
                    for item in normalized_batch:
                        connection.execute(
                            """
                            INSERT INTO footprint_candles (
                                symbol,
                                timeframe,
                                timestamp,
                                open,
                                high,
                                low,
                                close,
                                volume,
                                trade_count,
                                raw_json,
                                updated_at
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(symbol, timeframe, timestamp)
                            DO UPDATE SET
                                open = excluded.open,
                                high = excluded.high,
                                low = excluded.low,
                                close = excluded.close,
                                volume = excluded.volume,
                                trade_count = excluded.trade_count,
                                raw_json = excluded.raw_json,
                                updated_at = excluded.updated_at
                            """,
                            (
                                item["symbol"],
                                item["timeframe"],
                                item["timestamp"],
                                item["open"],
                                item["high"],
                                item["low"],
                                item["close"],
                                item["volume"],
                                item["trade_count"],
                                item["raw_json"],
                                now_ms,
                            ),
                        )

                        inserted_or_updated += 1

        return {
            "saved": inserted_or_updated,
            "rejected": rejected,
        }

    # --------------------------------------------------------
    # CANDLE READ
    # --------------------------------------------------------

    def load_candles(
        self,
        symbol: str,
        *,
        timeframe: str = "1m",
        limit: int = 1_000,
        before_timestamp: Optional[int] = None,
        after_timestamp: Optional[int] = None,
        order: str = "ASC",
        include_raw: bool = False,
    ) -> List[Dict[str, Any]]:
        symbol = _normalize_symbol(symbol)
        timeframe = str(timeframe).strip()

        if not timeframe:
            raise ValueError("timeframe cannot be empty")

        limit = int(limit)

        if limit <= 0:
            return []

        order = str(order).upper().strip()

        if order not in {"ASC", "DESC"}:
            raise ValueError("order must be ASC or DESC")

        conditions = [
            "symbol = ?",
            "timeframe = ?",
        ]

        params: List[Any] = [
            symbol,
            timeframe,
        ]

        if after_timestamp is not None:
            conditions.append("timestamp >= ?")
            params.append(_normalize_timestamp(after_timestamp))

        if before_timestamp is not None:
            conditions.append("timestamp <= ?")
            params.append(_normalize_timestamp(before_timestamp))

        params.append(limit)

        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    id,
                    symbol,
                    timeframe,
                    timestamp,
                    open,
                    high,
                    low,
                    close,
                    volume,
                    trade_count,
                    raw_json,
                    updated_at
                FROM footprint_candles
                WHERE {" AND ".join(conditions)}
                ORDER BY timestamp {order}, id {order}
                LIMIT ?
                """,
                params,
            ).fetchall()

        result = []

        for row in rows:
            item = {
                "id": row["id"],
                "symbol": row["symbol"],
                "timeframe": row["timeframe"],
                "timestamp": row["timestamp"],
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"],
                "trade_count": row["trade_count"],
                "updated_at": row["updated_at"],
            }

            if include_raw:
                item["raw"] = _json_loads(
                    row["raw_json"],
                    default={},
                )

            result.append(item)

        return result

    def count_candles(
        self,
        symbol: Optional[str] = None,
        *,
        timeframe: Optional[str] = None,
    ) -> int:
        conditions: List[str] = []
        params: List[Any] = []

        if symbol is not None:
            conditions.append("symbol = ?")
            params.append(_normalize_symbol(symbol))

        if timeframe is not None:
            conditions.append("timeframe = ?")
            params.append(str(timeframe).strip())

        where_sql = (
            "WHERE " + " AND ".join(conditions)
            if conditions
            else ""
        )

        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM footprint_candles
                {where_sql}
                """,
                params,
            ).fetchone()

        return int(row["total"])

    # --------------------------------------------------------
    # ENGINE STATE
    # --------------------------------------------------------

    def save_state(
        self,
        key: str,
        value: Any,
    ) -> None:
        """
        Persist a JSON-serializable engine state.

        Example:
            store.save_state("footprint_engine", state_dict)
        """

        key = str(key).strip()

        if not key:
            raise ValueError("state key cannot be empty")

        payload = _json_dumps(value)
        now_ms = _utc_now_ms()

        with self._lock:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO footprint_state (
                        key,
                        value_json,
                        updated_at
                    )
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value_json = excluded.value_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        key,
                        payload,
                        now_ms,
                    ),
                )

    def load_state(
        self,
        key: str = DEFAULT_STATE_KEY,
        *,
        default: Any = None,
    ) -> Any:
        key = str(key).strip()

        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT value_json
                FROM footprint_state
                WHERE key = ?
                """,
                (key,),
            ).fetchone()

        if row is None:
            return default

        return _json_loads(
            row["value_json"],
            default=default,
        )

    def load_all_state(self) -> Dict[str, Any]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT key, value_json
                FROM footprint_state
                ORDER BY key ASC
                """
            ).fetchall()

        result: Dict[str, Any] = {}

        for row in rows:
            result[row["key"]] = _json_loads(
                row["value_json"],
                default=None,
            )

        return result

    def delete_state(self, key: str) -> bool:
        with self._lock:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    DELETE FROM footprint_state
                    WHERE key = ?
                    """,
                    (str(key).strip(),),
                )

                return cursor.rowcount > 0

    # --------------------------------------------------------
    # STREAM EVENTS
    # --------------------------------------------------------

    def record_stream_event(
        self,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        symbol: Optional[str] = None,
        timestamp: Optional[Any] = None,
    ) -> int:
        """
        Record a stream lifecycle or diagnostic event.

        Suggested event types:
            CONNECTING
            CONNECTED
            DISCONNECTED
            RECONNECTING
            ERROR
            HEARTBEAT
            RECOVERY_STARTED
            RECOVERY_COMPLETED
        """

        event_type = str(event_type).strip().upper()

        if not event_type:
            raise ValueError("event_type cannot be empty")

        if payload is None:
            payload = {}

        event_timestamp = (
            _normalize_timestamp(timestamp)
            if timestamp is not None
            else _utc_now_ms()
        )

        normalized_symbol = (
            _normalize_symbol(symbol)
            if symbol is not None
            else None
        )

        with self._lock:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO footprint_stream_events (
                        event_type,
                        symbol,
                        timestamp,
                        payload_json
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        event_type,
                        normalized_symbol,
                        event_timestamp,
                        _json_dumps(payload),
                    ),
                )

                return int(cursor.lastrowid)

    def load_stream_events(
        self,
        *,
        event_type: Optional[str] = None,
        symbol: Optional[str] = None,
        limit: int = 500,
        order: str = "DESC",
        after_timestamp: Optional[int] = None,
        before_timestamp: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        limit = int(limit)

        if limit <= 0:
            return []

        order = str(order).upper().strip()

        if order not in {"ASC", "DESC"}:
            raise ValueError("order must be ASC or DESC")

        conditions: List[str] = []
        params: List[Any] = []

        if event_type is not None:
            conditions.append("event_type = ?")
            params.append(str(event_type).strip().upper())

        if symbol is not None:
            conditions.append("symbol = ?")
            params.append(_normalize_symbol(symbol))

        if after_timestamp is not None:
            conditions.append("timestamp >= ?")
            params.append(_normalize_timestamp(after_timestamp))

        if before_timestamp is not None:
            conditions.append("timestamp <= ?")
            params.append(_normalize_timestamp(before_timestamp))

        where_sql = (
            "WHERE " + " AND ".join(conditions)
            if conditions
            else ""
        )

        params.append(limit)

        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    id,
                    event_type,
                    symbol,
                    timestamp,
                    payload_json
                FROM footprint_stream_events
                {where_sql}
                ORDER BY timestamp {order}, id {order}
                LIMIT ?
                """,
                params,
            ).fetchall()

        return [
            {
                "id": row["id"],
                "event_type": row["event_type"],
                "symbol": row["symbol"],
                "timestamp": row["timestamp"],
                "payload": _json_loads(
                    row["payload_json"],
                    default={},
                ),
            }
            for row in rows
        ]

    # --------------------------------------------------------
    # STORAGE STATUS AND INTEGRITY
    # --------------------------------------------------------

    def get_storage_status(self) -> Dict[str, Any]:
        with self._connection() as connection:
            trade_count = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM footprint_trades
                """
            ).fetchone()["total"]

            candle_count = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM footprint_candles
                """
            ).fetchone()["total"]

            event_count = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM footprint_stream_events
                """
            ).fetchone()["total"]

            state_count = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM footprint_state
                """
            ).fetchone()["total"]

            latest_trade = connection.execute(
                """
                SELECT MAX(timestamp) AS latest_timestamp
                FROM footprint_trades
                """
            ).fetchone()["latest_timestamp"]

            oldest_trade = connection.execute(
                """
                SELECT MIN(timestamp) AS oldest_timestamp
                FROM footprint_trades
                """
            ).fetchone()["oldest_timestamp"]

            journal_mode = connection.execute(
                "PRAGMA journal_mode"
            ).fetchone()[0]

            database_integrity = connection.execute(
                "PRAGMA quick_check"
            ).fetchone()[0]

        file_size = (
            self.db_path.stat().st_size
            if self.db_path.exists()
            else 0
        )

        return {
            "backend": "sqlite",
            "path": str(self.db_path),
            "exists": self.db_path.exists(),
            "initialized": self._initialized,
            "closed": self._closed,
            "schema_version": SCHEMA_VERSION,
            "journal_mode": str(journal_mode).lower(),
            "trade_count": int(trade_count),
            "candle_count": int(candle_count),
            "stream_event_count": int(event_count),
            "state_count": int(state_count),
            "latest_trade_timestamp": (
                int(latest_trade)
                if latest_trade is not None
                else None
            ),
            "oldest_trade_timestamp": (
                int(oldest_trade)
                if oldest_trade is not None
                else None
            ),
            "database_size_bytes": file_size,
            "integrity_check": database_integrity,
            "healthy": database_integrity == "ok",
        }

    def check_integrity(self) -> Dict[str, Any]:
        with self._connection() as connection:
            quick_check = connection.execute(
                "PRAGMA quick_check"
            ).fetchall()

            foreign_key_check = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()

        quick_messages = [
            row[0]
            for row in quick_check
        ]

        foreign_key_errors = [
            tuple(row)
            for row in foreign_key_check
        ]

        healthy = (
            quick_messages == ["ok"]
            and len(foreign_key_errors) == 0
        )

        return {
            "healthy": healthy,
            "quick_check": quick_messages,
            "foreign_key_errors": foreign_key_errors,
        }

    def checkpoint(
        self,
        mode: str = "PASSIVE",
    ) -> Dict[str, Any]:
        """
        Run a WAL checkpoint.

        PASSIVE is suitable for routine checks.
        TRUNCATE should be used deliberately, not on every trade.
        """

        mode = str(mode).upper().strip()

        if mode not in {
            "PASSIVE",
            "FULL",
            "RESTART",
            "TRUNCATE",
        }:
            raise ValueError(
                "mode must be PASSIVE, FULL, RESTART or TRUNCATE"
            )

        with self._connection() as connection:
            row = connection.execute(
                f"PRAGMA wal_checkpoint({mode})"
            ).fetchone()

        return {
            "busy": int(row[0]),
            "log_frames": int(row[1]),
            "checkpointed_frames": int(row[2]),
            "mode": mode,
        }

    # --------------------------------------------------------
    # RETENTION
    # --------------------------------------------------------

    def prune_trades_before(
        self,
        timestamp: Any,
        *,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        """
        Delete trades older than the supplied timestamp.

        Safety:
            dry_run=True by default.

        The caller must explicitly pass dry_run=False to delete
        records. No retention runs automatically at startup.
        """

        cutoff_ms = _normalize_timestamp(timestamp)

        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM footprint_trades
                WHERE timestamp < ?
                """,
                (cutoff_ms,),
            ).fetchone()

            matched = int(row["total"])

            if dry_run:
                return {
                    "dry_run": True,
                    "cutoff_timestamp": cutoff_ms,
                    "matched": matched,
                    "deleted": 0,
                }

        with self._lock:
            with self._connection() as connection:
                with self._transaction(connection):
                    cursor = connection.execute(
                        """
                        DELETE FROM footprint_trades
                        WHERE timestamp < ?
                        """,
                        (cutoff_ms,),
                    )

                    deleted = int(cursor.rowcount)

        return {
            "dry_run": False,
            "cutoff_timestamp": cutoff_ms,
            "matched": matched,
            "deleted": deleted,
        }

    # --------------------------------------------------------
    # DATABASE BACKUP
    # --------------------------------------------------------

    def backup_to(
        self,
        destination: str | Path,
    ) -> Path:
        """
        Create a consistent SQLite backup.

        SQLite's backup API is used so that the backup also works
        when WAL is enabled.
        """

        destination_path = Path(
            destination
        ).expanduser().resolve()

        if destination_path == self.db_path:
            raise ValueError(
                "Backup destination cannot equal source database"
            )

        destination_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        with self._lock:
            with self._connection() as source:
                target = sqlite3.connect(
                    str(destination_path)
                )

                try:
                    source.backup(target)
                    target.commit()

                finally:
                    target.close()

        logger.info(
            "Footprint database backup created: %s",
            destination_path,
        )

        return destination_path

    # --------------------------------------------------------
    # LIFECYCLE
    # --------------------------------------------------------

    def close(self) -> None:
        """
        Mark the store as closed.

        This class does not keep a persistent connection open, so
        there is no connection pool to drain.
        """

        with self._lock:
            if self._closed:
                return

            self._closed = True
            self._initialized = False

        logger.info(
            "Footprint SQLite store closed: %s",
            self.db_path,
        )

    def reopen(self) -> None:
        """
        Re-enable a previously closed store and initialize it.
        """

        with self._lock:
            self._closed = False
            self._initialized = False

        self.initialize()

    def __enter__(self) -> "LocalStore":
        return self

    def __exit__(
        self,
        exc_type: Any,
        exc_value: Any,
        traceback: Any,
    ) -> None:
        self.close()


# ============================================================
# DEFAULT DATABASE PATH
# ============================================================

def default_database_path() -> Path:
    """
    Resolve the SQLite database path.

    Priority:
      1. FOOTPRINT_SQLITE_PATH environment variable.
      2. <project-root>/data/footprint.db

    The project root is inferred from this module's location.
    """

    configured_path = os.getenv(
        "FOOTPRINT_SQLITE_PATH"
    )

    if configured_path:
        return Path(configured_path).expanduser().resolve()

    # Expected structure:
    #
    # project/
    #   scripts/
    #     footprint/
    #       local_store.py
    #
    # parents[0] -> scripts/footprint
    # parents[1] -> scripts
    # parents[2] -> project root
    project_root = Path(__file__).resolve().parents[2]

    return project_root / "data" / "footprint.db"


# ============================================================
# DEFAULT STORE SINGLETON
# ============================================================

_default_store: Optional[LocalStore] = None
_default_store_lock = threading.RLock()


def get_local_store(
    db_path: Optional[str | Path] = None,
) -> LocalStore:
    """
    Return the process-wide default store.

    If db_path is supplied and differs from the active store,
    raise an error instead of silently switching databases.
    """

    global _default_store

    with _default_store_lock:
        if _default_store is not None and _default_store._closed:
            _default_store = None

        if _default_store is None:
            _default_store = LocalStore(db_path=db_path)

            return _default_store

        if db_path is not None:
            requested_path = Path(
                db_path
            ).expanduser().resolve()

            if requested_path != _default_store.db_path:
                raise LocalStoreError(
                    "A default LocalStore already exists at "
                    f"{_default_store.db_path}; requested "
                    f"another database at {requested_path}. "
                    "Close the default store before switching."
                )

        return _default_store


def close_default_store() -> None:
    """
    Close and clear the default singleton.

    Call only during controlled application shutdown. Other modules
    holding a reference to the previous store must reacquire it
    through get_local_store() after closure.
    """

    global _default_store

    with _default_store_lock:
        if _default_store is not None:
            _default_store.close()
            _default_store = None


# ============================================================
# PUBLIC API
# ============================================================

__all__ = [
    "SCHEMA_VERSION",
    "VALID_SIDES",
    "LocalStore",
    "LocalStoreError",
    "SchemaVersionError",
    "TradeValidationError",
    "CandleValidationError",
    "normalize_trade",
    "normalize_candle",
    "default_database_path",
    "get_local_store",
    "close_default_store",
]