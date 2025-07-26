import os
import pandas as pd
from supabase import create_client, Client
from dotenv import load_dotenv
import joblib
from datetime import datetime

# ===== 1. Load ENV =====
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# ===== 2. Cấu hình =====
MODEL_PATH = "model/model_rf.pkl"
CANDLE_LOOKBACK = 50

# ===== 3. Load model ML =====
def load_model():
    try:
        model = joblib.load(MODEL_PATH)
        print("✅ Đã load model thành công!")
        return model
    except Exception as e:
        raise Exception(f"❌ Không load được model: {e}")

# ===== 4. Lấy dữ liệu dự đoán gần nhất =====
def fetch_latest_data(symbol):
    try:
        res = supabase.table("training_dataset")\
            .select("*")\
            .eq("symbol", symbol)\
            .order("timestamp", desc=True)\
            .limit(1)\
            .execute()
        if not res.data:
            print(f"⚠️ Không có dữ liệu mới cho {symbol}")
            return None
        return pd.DataFrame(res.data)
    except Exception as e:
        print(f"❌ Lỗi fetch dữ liệu cho {symbol}: {e}")
        return None

# ===== 5. Lấy 50 nến để tính toán SL/TP =====
def fetch_candles(symbol):
    try:
        res = supabase.table("ohlcv_data")\
            .select("timestamp, open, high, low, close")\
            .eq("symbol", symbol)\
            .order("timestamp", desc=True)\
            .limit(CANDLE_LOOKBACK)\
            .execute()
        return pd.DataFrame(res.data[::-1])  # đảo lại theo thời gian tăng
    except Exception as e:
        print(f"❌ Lỗi fetch candles cho {symbol}: {e}")
        return pd.DataFrame()

# ===== 6. Tiền xử lý =====
def preprocess(df, model):
    expected_features = list(model.feature_names_in_)
    df = df[[col for col in expected_features if col in df.columns]]
    df.fillna(0, inplace=True)
    return df

# ===== 7. Dự đoán =====
def decode_prediction(pred):
    return {1: "BUY", 0: "HOLD", -1: "SELL"}.get(pred, "HOLD")

# ===== 8. Tính SL/TP từ hỗ trợ kháng cự =====
def calculate_trade_levels(candles: pd.DataFrame):
    high = candles["high"].max()
    low = candles["low"].min()
    current_price = candles["close"].iloc[-1]

    # Kháng cự: đỉnh cũ gần nhất lớn hơn current_price
    resistance = candles[candles["high"] > current_price]["high"].min()
    support = candles[candles["low"] < current_price]["low"].max()

    tp = resistance if not pd.isna(resistance) else high
    sl = support if not pd.isna(support) else low

    return current_price, tp, sl, high, low

# ===== 9. Ghi kết quả =====
def insert_prediction(symbol, timestamp, prediction, confidence, entry, tp, sl, high, low, current_price):
    record = {
        "symbol": symbol,
        "timestamp": int(timestamp),
        "prediction": prediction,
        "confidence": float(round(confidence, 4)),
        "model_name": "baseline_v2",
        "entry_price": entry,
        "tp": tp,
        "sl": sl,
        "high": high,
        "low": low,
        "current_price": current_price,
        "executed": False,
        "created_at": datetime.now().isoformat()
    }

    try:
        existing = supabase.table("ai_predictions")\
            .select("id")\
            .eq("symbol", symbol)\
            .eq("timestamp", int(timestamp))\
            .execute()
        if existing.data:
            print(f"⏭️ Prediction {symbol} tại {timestamp} đã tồn tại")
            return

        supabase.table("ai_predictions").insert(record).execute()
        print(f"✅ Lưu {prediction} cho {symbol} @ {entry}")
    except Exception as e:
        print(f"❌ Insert prediction lỗi: {e}")

# ===== 10. Chạy chính =====
def run():
    model = load_model()
    symbols_res = supabase.table("watched_symbols").select("symbol").eq("active", True).execute()
    symbols = [s["symbol"] for s in symbols_res.data]
    print(f"🚀 Chạy AI cho {len(symbols)} symbols...")

    for symbol in symbols:
        print(f"\n🔍 Dự đoán {symbol}...")
        df_latest = fetch_latest_data(symbol)
        if df_latest is None or df_latest.empty:
            continue

        candles = fetch_candles(symbol)
        if candles.empty:
            continue

        try:
            X = preprocess(df_latest.copy(), model)
            pred = model.predict(X)[0]
            confidence = max(model.predict_proba(X)[0]) if hasattr(model, "predict_proba") else 1.0
            pred_label = decode_prediction(int(round(pred)))
            timestamp = df_latest.iloc[0]["timestamp"]

            entry, tp, sl, high, low = calculate_trade_levels(candles)
            insert_prediction(symbol, timestamp, pred_label, confidence, entry, tp, sl, high, low, entry)
        except Exception as e:
            print(f"❌ Lỗi khi predict {symbol}: {e}")

if __name__ == "__main__":
    run()
