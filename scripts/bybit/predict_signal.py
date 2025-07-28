import os
import pandas as pd
from supabase import create_client, Client
from dotenv import load_dotenv
import joblib
from datetime import datetime
from sklearn.preprocessing import StandardScaler

# ===== 1. Load ENV =====
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# ===== 2. Cấu hình =====
MODEL_PATH = "model/model_rf.pkl"
SCALER_PATH = "model/scaler.pkl"
CANDLE_LOOKBACK = 50

# ===== 3. Load model ML & scaler =====
def load_model_and_scaler():
    try:
        model = joblib.load(MODEL_PATH)
        scaler = joblib.load(SCALER_PATH)
        print("✅ Đã load model & scaler thành công!")
        return model, scaler
    except Exception as e:
        raise Exception(f"❌ Không load được model hoặc scaler: {e}")

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
def preprocess(df, model, scaler):
    expected_features = list(model.feature_names_in_)
    df = df[[col for col in expected_features if col in df.columns]]
    df.fillna(0, inplace=True)
    df_scaled = scaler.transform(df)
    return pd.DataFrame(df_scaled, columns=expected_features)

# ===== 7. Dự đoán =====
def decode_prediction(pred):
    return {1: "BUY", 0: "HOLD", -1: "SELL"}.get(pred, "HOLD")

# ===== 8. Tính SL/TP từ hỗ trợ kháng cự =====
import numpy as np
import pandas as pd
from datetime import datetime
# Giả sử đã import: supabase, joblib, model, scaler, v.v.

# ===== 1. Tính TP/SL thông minh dựa trên ATR =====
def calculate_trade_levels(
    candles: pd.DataFrame, 
    entry_price: float, 
    signal: str, 
    rr_ratio: float = 1.8, 
    atr_window: int = 21
):
    """
    Tính toán TP/SL thông minh dựa trên ATR và Risk:Reward Ratio.
    
    Parameters:
    - candles: DataFrame chứa các cột 'high', 'low', 'close'
    - entry_price: giá vào lệnh
    - signal: 'BUY' | 'SELL' | 'HOLD'
    - rr_ratio: tỷ lệ risk:reward (mặc định 1.8)
    - atr_window: số nến dùng để tính ATR (mặc định 21 nến)

    Returns:
    - entry, tp, sl, high_50, low_50
    """

    candles = candles.copy()
    candles["prev_close"] = candles["close"].shift(1)
    candles["tr"] = np.maximum.reduce([
        candles["high"] - candles["low"],
        abs(candles["high"] - candles["prev_close"]),
        abs(candles["low"] - candles["prev_close"]),
    ])

    atr = candles["tr"].rolling(window=atr_window).mean().iloc[-1]

    if pd.isna(atr) or atr == 0:
        atr = 0.002  # fallback cứng nếu ATR fail

    if signal == "BUY":
        sl = entry_price - atr
        tp = entry_price + atr * rr_ratio
    elif signal == "SELL":
        sl = entry_price + atr
        tp = entry_price - atr * rr_ratio
    else:  # HOLD thì không cần tính
        return round(entry_price, 6), None, None, None, None

    high_50 = candles["high"].tail(50).max()
    low_50 = candles["low"].tail(50).min()

    return round(entry_price, 6), round(tp, 6), round(sl, 6), round(high_50, 6), round(low_50, 6)

# ===== 2. Ghi lại kết quả dự đoán =====
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

# ===== 3. Chạy chính =====
def run():
    model, scaler = load_model_and_scaler()
    symbols_res = supabase.table("watched_symbols").select("symbol").eq("active", True).execute()
    symbols = [s["symbol"] for s in symbols_res.data]
    print(f"🚀 Chạy AI cho {len(symbols)} symbols...")

    for symbol in symbols:
        print(f"\n🔍 Dự đoán {symbol}...")

        df_latest = fetch_latest_data(symbol)
        if df_latest is None or df_latest.empty:
            print("⚠️ Không có dữ liệu mới.")
            continue

        candles = fetch_candles(symbol)
        if candles.empty:
            print("⚠️ Không lấy được nến.")
            continue

        try:
            # 1. Tiền xử lý
            X = preprocess(df_latest.copy(), model, scaler)

            # 2. Dự đoán
            pred = model.predict(X)[0]
            confidence = max(model.predict_proba(X)[0]) if hasattr(model, "predict_proba") else 1.0
            pred_label = decode_prediction(int(round(pred)))
            signal = pred_label

            # 3. Tính toán SL/TP
            timestamp = int(df_latest["timestamp"].iloc[0])
            entry_price = candles["close"].iloc[-1]
            entry, tp, sl, high, low = calculate_trade_levels(candles, entry_price, signal)

            # 4. Ghi lại kết quả
            insert_prediction(symbol, timestamp, pred_label, confidence, entry, tp, sl, high, low, entry)

        except Exception as e:
            print(f"❌ Lỗi khi predict {symbol}: {e}")

# ===== 4. Main =====
if __name__ == "__main__":
    run()
