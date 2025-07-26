import os
import pandas as pd
import numpy as np
from supabase import create_client, Client
from ta import add_all_ta_features
from dotenv import load_dotenv
from datetime import datetime

# ===== 1. Load biến môi trường =====
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

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
        df = df.copy()
        df = add_all_ta_features(
            df,
            open="open", high="high", low="low", close="close", volume="volume",
            fillna=True
        )
        df["future_close"] = df["close"].shift(-3)

        # Gán target: BUY nếu future_close > close * 1.002, SELL nếu < close * 0.998, còn lại là HOLD
        df["target"] = "hold"
        df.loc[df["future_close"] > df["close"] * 1.002, "target"] = "buy"
        df.loc[df["future_close"] < df["close"] * 0.998, "target"] = "sell"

        df.dropna(inplace=True)
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
                "ema_cross": int(row.get("trend_ema_fast", 0) > row.get("trend_ema_slow", 0)),
                "rsi": float(row.get("momentum_rsi", 0)),
                "macd": float(row.get("trend_macd", 0)),
                "macd_signal": float(row.get("trend_macd_signal", 0)),
                "macd_hist": float(row.get("trend_macd_diff", 0)),
                "bb_lower": float(row.get("volatility_bbl", 0)),
                "bb_middle": float(row.get("volatility_bbm", 0)),
                "bb_upper": float(row.get("volatility_bbh", 0)),
                "target": row.get("target", "hold"),
                "signal": 1 if row.get("target") == "buy" else (-1 if row.get("target") == "sell" else 0),
                "created_at": datetime.utcnow().isoformat()
            }

            # Check trùng trước khi insert
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
