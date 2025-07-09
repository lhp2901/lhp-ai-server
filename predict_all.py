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
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")

if not SUPABASE_URL or not SUPABASE_ANON_KEY:
    print("❌ Thiếu SUPABASE_URL hoặc SUPABASE_ANON_KEY trong .env", file=sys.stderr)
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

MODEL_PATH = Path("scripts") / "model.pkl"
REQUIRED_COLUMNS = [
    "close", "volume", "ma20", "rsi",
    "bb_upper", "bb_lower", "foreign_buy_value", "foreign_sell_value"
]

def fetch_ai_input_data() -> pd.DataFrame:
    print("📡 Đang lấy dữ liệu cần dự đoán từ Supabase...", file=sys.stderr)
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

def predict(df: pd.DataFrame) -> pd.DataFrame:
    if not MODEL_PATH.exists():
        raise FileNotFoundError(f"❌ Không tìm thấy model tại: {MODEL_PATH}")

    model = joblib.load(MODEL_PATH)

    missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError(f"❌ Thiếu cột cần thiết: {missing}")

    X = df[REQUIRED_COLUMNS].fillna(0)
    probs = model.predict_proba(X)

    df["ai_predicted_probability"] = probs[:, 1]
    df["ai_recommendation"] = df["ai_predicted_probability"].apply(
        lambda p: "BUY" if p > 0.6 else "SELL"
    )

    return df[["symbol", "date", "ai_predicted_probability", "ai_recommendation"]]

def save_results(df: pd.DataFrame):
    print(f"💾 Đang ghi {len(df)} dòng kết quả lên Supabase...", file=sys.stderr)
    df = df.where(pd.notnull(df), None)
    payload = df.to_dict(orient="records")

    try:
        supabase.table("ai_signals") \
            .upsert(payload, on_conflict="symbol,date") \
            .execute()
    except Exception as e:
        raise RuntimeError(f"❌ Lỗi khi ghi kết quả: {e}")

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
