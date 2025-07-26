import os
import pandas as pd
from supabase import create_client, Client
from dotenv import load_dotenv
import joblib
from datetime import datetime

# ===== 1. Load biến môi trường =====
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# ===== 2. Cấu hình =====
MODEL_PATH = "model/model_rf.pkl"

# ===== 3. Load model ML =====
def load_model():
    try:
        model = joblib.load(MODEL_PATH)
        print("✅ Đã load model thành công!")
        return model
    except Exception as e:
        raise Exception(f"❌ Không load được model: {e}")

# ===== 4. Lấy dữ liệu gần nhất =====
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

# ===== 5. Tiền xử lý =====
def preprocess(df, model):
    # Lấy các cột model cần
    expected_features = list(model.feature_names_in_)

    # Chỉ giữ lại các cột có trong model
    df = df[[col for col in expected_features if col in df.columns]]

    df.fillna(0, inplace=True)
    return df

# ===== 6. Mapping dự đoán =====
def decode_prediction(pred):
    mapping = {1: "BUY", 0: "HOLD", -1: "SELL"}
    return mapping.get(pred, "HOLD")

# ===== 7. Ghi dự đoán lên Supabase =====
def insert_prediction(symbol, timestamp, prediction, confidence=0.0, model_name="baseline_v1"):
    record = {
        "symbol": symbol,
        "timestamp": int(timestamp),
        "prediction": prediction,
        "confidence": float(round(confidence, 4)),
        "model_name": model_name,
        "created_at": datetime.now().isoformat()
    }
    try:
        # Check nếu đã tồn tại rồi thì bỏ qua hoặc cập nhật
        existing = supabase.table("ai_predictions")\
            .select("id")\
            .eq("symbol", symbol)\
            .eq("timestamp", int(timestamp))\
            .execute()
        
        if existing.data and len(existing.data) > 0:
            print(f"⏭️ Bỏ qua prediction cho {symbol} lúc {timestamp} - đã tồn tại")
            return
        
        supabase.table("ai_predictions").insert(record).execute()
        print(f"✅ Đã lưu prediction {prediction} cho {symbol} lúc {timestamp}")
    except Exception as e:
        print(f"⚠️ Không thể insert prediction cho {symbol}: {e}")

# ===== 8. Hàm chính =====
def run():
    model = load_model()

    symbols_res = supabase.table("watched_symbols").select("symbol").execute()
    symbols = [s["symbol"] for s in symbols_res.data]
    print(f"📌 Bắt đầu dự đoán {len(symbols)} symbol...")

    for symbol in symbols:
        print(f"\n🔍 Dự đoán cho {symbol}...")
        df_latest = fetch_latest_data(symbol)
        if df_latest is None or df_latest.empty:
            continue

        try:
            X = preprocess(df_latest.copy(), model) 
            pred = model.predict(X)[0]

            # Nếu là classifier có predict_proba → dùng
            if hasattr(model, "predict_proba"):
                pred_proba = model.predict_proba(X)[0]
                confidence = max(pred_proba)
            else:
                confidence = 1.0  # fallback nếu không có predict_proba

            pred_label = decode_prediction(int(round(pred)))  # convert regressor float → int
            timestamp = df_latest.iloc[0]["timestamp"]

            insert_prediction(symbol, timestamp, pred_label, confidence)
        except Exception as e:
            print(f"❌ Lỗi khi predict {symbol}: {e}")

# ===== 9. Chạy trực tiếp =====
if __name__ == "__main__":
    run()
