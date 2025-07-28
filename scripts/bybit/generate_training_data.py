import os
import sys
import pandas as pd
import numpy as np
from supabase import create_client, Client
from ta import add_all_ta_features
from dotenv import load_dotenv
from datetime import datetime

# ====== 1. Nạp biến môi trường từ .env ======
sys.stdout.reconfigure(encoding='utf-8')
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    print("❌ Thiếu SUPABASE_URL hoặc SUPABASE_SERVICE_ROLE_KEY trong .env")
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# ===== 2. Lấy danh sách symbol cần xử lý =====
def get_watched_symbols():
    try:
        res = supabase.table("watched_symbols").select("symbol").execute()
        return [s["symbol"] for s in res.data if s.get("symbol")]
    except Exception as e:
        print(f"❌ Không thể lấy danh sách symbol: {e}")
        return []

# ===== 3. Lấy dữ liệu nến từ ohlcv_data =====
def fetch_ohlcv(symbol: str) -> pd.DataFrame:
    try:
        response = supabase.table("ohlcv_data").select("*").eq("symbol", symbol).order("timestamp").execute()
        raw = response.data
        if not raw:
            print(f"⚠️ Không có dữ liệu OHLCV cho {symbol}")
            return pd.DataFrame()
        df = pd.DataFrame(raw)
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        return df
    except Exception as e:
        print(f"❌ Lỗi khi fetch dữ liệu OHLCV của {symbol}: {e}")
        return pd.DataFrame()

# ===== 4. Tính chỉ báo kỹ thuật & target =====
def generate_features(df: pd.DataFrame) -> pd.DataFrame:
    try:
        from sklearn.preprocessing import StandardScaler

        df = df.copy()

        # ===== 1. Tính chỉ báo kỹ thuật từ TA-Lib
        df = add_all_ta_features(
            df,
            open="open", high="high", low="low", close="close", volume="volume",
            fillna=True
        )

        # ===== 2. Target: Xác định hành vi giá trong 3 nến tiếp theo (t+1, t+2, t+3)
        n_future = 3

        # Lấy max/min của 3 nến tiếp theo, bắt đầu từ t+1
        df["future_max"] = df["high"].shift(-1).rolling(window=n_future).max()
        df["future_min"] = df["low"].shift(-1).rolling(window=n_future).min()

        # Gán nhãn dự đoán
        df["target"] = "hold"
        df.loc[
            (df["future_max"] > df["close"] * 1.002) & (df["future_min"] > df["close"] * 0.998),
            "target"
        ] = "buy"
        df.loc[
            (df["future_min"] < df["close"] * 0.998) & (df["future_max"] < df["close"] * 1.002),
            "target"
        ] = "sell"

        # ===== 3. Tạo các đặc trưng kỹ thuật bổ sung
        features = {
            "volume_change_pct": df["volume"].pct_change().fillna(0),
            "price_change_pct": df["close"].pct_change().fillna(0),
            "candle_body": abs(df["close"] - df["open"]),
            "upper_wick": df["high"] - df[["close", "open"]].max(axis=1),
            "lower_wick": df[["close", "open"]].min(axis=1) - df["low"],
            "volume_spike": (df["volume"] > df["volume"].rolling(20).mean() * 1.5).astype(int),
            "rsi_reversal": ((df["momentum_rsi"] < 30) | (df["momentum_rsi"] > 70)).astype(int),
            "macd_divergence": df["trend_macd"] - df["trend_macd_signal"],
            "reversal_candle": (
                (abs(df["close"] - df["open"]) >
                 (df["high"] - df[["close", "open"]].max(axis=1) +
                  df[["close", "open"]].min(axis=1) - df["low"])) &
                ((df["high"] - df[["close", "open"]].max(axis=1)) > abs(df["close"] - df["open"]) * 0.5)
            ).astype(int),
            "ema_cross": (df["trend_ema_fast"] > df["trend_ema_slow"]).astype(int),
            "hour_of_day": df.index.hour,
            "day_of_week": df.index.dayofweek,
            "bb_width_pct": ((df["volatility_bbh"] - df["volatility_bbl"]) / df["volatility_bbm"]).fillna(0),
        }

        df = pd.concat([df, pd.DataFrame(features, index=df.index)], axis=1)

        # ===== 4. Chuyển target sang tín hiệu số
        df["signal"] = df["target"].map({"buy": 1, "sell": -1}).fillna(0).astype(int)

        # ===== 5. Lọc rác
        df = df[df["volume"] > 100]
        df = df[df["volume_change_pct"].abs() < 10]
        df.dropna(inplace=True)

        # ===== 6. Chuẩn hóa numeric columns (trừ target/signal)
        numeric_cols = df.select_dtypes(include=[np.number]).columns.drop("signal", errors="ignore")
        scaler = StandardScaler()
        df[numeric_cols] = scaler.fit_transform(df[numeric_cols])
        df[numeric_cols] = df[numeric_cols].astype(float)

        return df

    except Exception as e:
        print(f"❌ Lỗi khi tính chỉ báo kỹ thuật: {e}")
        return pd.DataFrame()


# ===== 5. Ghi vào bảng training_dataset =====
def insert_training_data(symbol: str, df: pd.DataFrame):
    count = 0
    for i, row in df.iterrows():
        try:
            record = {
                "timestamp": int(i.timestamp() * 1000),
                "symbol": symbol,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
                "ema_20": float(row.get("trend_ema_slow", 0)),
                "ema_50": float(row.get("trend_ema_fast", 0)),
                "ema_cross": int(row.get("ema_cross", 0)),
                "rsi": float(row.get("momentum_rsi", 0)),
                "macd": float(row.get("trend_macd", 0)),
                "macd_signal": float(row.get("trend_macd_signal", 0)),
                "macd_hist": float(row.get("trend_macd_diff", 0)),
                "bb_lower": float(row.get("volatility_bbl", 0)),
                "bb_middle": float(row.get("volatility_bbm", 0)),
                "bb_upper": float(row.get("volatility_bbh", 0)),
                "bb_width_pct": float(row.get("bb_width_pct", 0)),
                "volume_change_pct": float(row.get("volume_change_pct", 0)),
                "price_change_pct": float(row.get("price_change_pct", 0)),
                "candle_body": float(row.get("candle_body", 0)),
                "upper_wick": float(row.get("upper_wick", 0)),
                "lower_wick": float(row.get("lower_wick", 0)),
                "volume_spike": bool(row.get("volume_spike", False)),
                "rsi_reversal": int(row.get("rsi_reversal", 0)),
                "macd_divergence": float(row.get("macd_divergence", 0)),
                "reversal_candle": int(row.get("reversal_candle", 0)),
                "hour_of_day": int(row.get("hour_of_day", 0)),
                "day_of_week": int(row.get("day_of_week", 0)),
                "target": row.get("target", "hold"),
                "signal": int(row.get("signal", 0)),
                "created_at": datetime.utcnow().isoformat()
            }

            existing = supabase.table("training_dataset") \
                .select("id") \
                .eq("timestamp", record["timestamp"]) \
                .eq("symbol", symbol) \
                .execute()
            if existing.data:
                print(f"⏭️ {symbol} | Bỏ qua {i} - đã tồn tại")
                continue

            supabase.table("training_dataset").insert(record).execute()
            count += 1
        except Exception as e:
            print(f"⚠️ Lỗi khi insert {symbol} tại {i}: {e}")
    print(f"✅ {symbol}: Đã thêm {count} dòng vào training_dataset.")

# ===== 6. Hàm chính =====
def run():
    symbols = get_watched_symbols()
    if not symbols:
        print("❌ Không có symbol nào cần xử lý.")
        return

    print(f"📌 Tổng số symbol cần xử lý: {len(symbols)}")

    for symbol in symbols:
        print(f"\n🚀 Đang xử lý: {symbol}")
        df = fetch_ohlcv(symbol)
        if df.empty:
            print(f"⚠️ Bỏ qua {symbol} vì không có dữ liệu")
            continue

        df_feat = generate_features(df)
        if df_feat.empty:
            print(f"⚠️ Bỏ qua {symbol} vì không sinh được chỉ báo")
            continue

        insert_training_data(symbol, df_feat)

if __name__ == "__main__":
    run()