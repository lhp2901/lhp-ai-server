import os
import sys
import json
import joblib
import pandas as pd
from datetime import datetime
from pathlib import Path
from supabase import create_client, Client
from dotenv import load_dotenv

# 🔐 Load biến môi trường từ .env
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    print("❌ Thiếu SUPABASE_URL hoặc SUPABASE_SERVICE_ROLE_KEY", file=sys.stderr)
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

MODEL_PATH = Path("model") / "model.pkl"
REQUIRED_COLUMNS = [
    "close", "volume", "ma20", "rsi",
    "bb_upper", "bb_lower", "foreign_buy_value", "foreign_sell_value"
]

def fetch_ai_input_data() -> pd.DataFrame:
    print("📡 Lấy dữ liệu chưa dự đoán từ bảng ai_signals...", file=sys.stderr)
    try:
        res = supabase.table("ai_signals") \
            .select("*") \
            .is_("ai_predicted_probability", "null") \
            .execute()
    except Exception as e:
        raise RuntimeError(f"❌ Lỗi truy vấn Supabase: {e}")

    df = pd.DataFrame(res.data or [])
    print(f"📊 Tổng dòng cần dự đoán: {len(df)}", file=sys.stderr)
    return df

def classify_recommendation(p: float) -> str:
    if p >= 0.75:
        return "BUY"
    elif p <= 0.4:
        return "SELL"
    else:
        return "WATCH"

def predict(df: pd.DataFrame) -> pd.DataFrame:
    if not MODEL_PATH.exists():
        raise FileNotFoundError(f"❌ Không tìm thấy mô hình tại: {MODEL_PATH}")

    model = joblib.load(MODEL_PATH)

    # Bổ sung các cột thiếu
    for col in REQUIRED_COLUMNS:
        if col not in df.columns:
            print(f"⚠️ Thiếu cột {col} → tạo với giá trị 0", file=sys.stderr)
            df[col] = 0

    X = df[REQUIRED_COLUMNS].fillna(0)
    probs = model.predict_proba(X)

    df["ai_predicted_probability"] = probs[:, 1]
    df["ai_recommendation"] = df["ai_predicted_probability"].apply(classify_recommendation)
    return df

def save_results(df: pd.DataFrame):
    print(f"💾 Ghi {len(df)} dòng kết quả lên Supabase...", file=sys.stderr)

    cols = ["user_id", "symbol", "date", "ai_predicted_probability", "ai_recommendation"]
    df = df[cols].copy()
    df = df.where(pd.notnull(df), None)

    try:
        supabase.table("ai_signals") \
            .upsert(df.to_dict(orient="records"), on_conflict="user_id,symbol,date") \
            .execute()
    except Exception as e:
        raise RuntimeError(f"❌ Lỗi ghi kết quả về Supabase: {e}")

def main():
    try:
        df = fetch_ai_input_data()
        if df.empty:
            print("✅ Không có dòng nào cần dự đoán hôm nay.", file=sys.stderr)
            print(json.dumps({ "message": "✅ Không cần dự đoán", "count": 0 }))
            return

        predicted = predict(df)
        save_results(predicted)

        print(json.dumps({
            "message": "✅ Dự đoán thành công",
            "count": len(predicted)
        }))

    except Exception as e:
        import traceback
        print(json.dumps({
            "error": str(e),
            "trace": traceback.format_exc()
        }), file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
