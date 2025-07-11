import os
import sys
import pandas as pd
import xgboost as xgb
import joblib
from supabase import create_client, Client
from dotenv import load_dotenv

# ✅ Fix lỗi Unicode khi chạy từ Node.js trên Windows
sys.stdout.reconfigure(encoding='utf-8')

load_dotenv()

# 🔗 Kết nối Supabase
supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_ANON_KEY")
)

def fetch_data():
    print("📥 Đang tải dữ liệu từ Supabase...")
    res = supabase.table("ai_signals").select("*").execute()
    df = pd.DataFrame(res.data or [])
    print(f"📊 Tổng số dòng tải về: {len(df)}")
    return df

def preprocess(df):
    expected = [
        "close", "volume", "ma20", "rsi", "bb_upper", "bb_lower",
        "foreign_buy_value", "foreign_sell_value",
        "label_win"
    ]

    for col in expected:
        if col not in df.columns:
            print(f"⚠️ Cột thiếu: {col} → tạo với giá trị 0")
            df[col] = 0

    df = df[expected].dropna()
    print(f"✅ Dữ liệu huấn luyện còn lại: {len(df)} dòng")
    return df

def train_model(df):
    X = df.drop("label_win", axis=1)
    y = df["label_win"].astype(int)
    model = xgb.XGBClassifier(n_estimators=100, max_depth=5, random_state=42)
    model.fit(X, y)
    return model

def save_model(model):
    os.makedirs("scripts", exist_ok=True)
    path = os.path.join("scripts", "model.pkl")
    joblib.dump(model, path)
    print(f"💾 Đã lưu mô hình vào: {path}")

def main():
    df = fetch_data()
    df = preprocess(df)

    if df.empty:
        print("❌ Không còn dữ liệu đủ để huấn luyện.")
        return

    model = train_model(df)
    save_model(model)
    print("✅ Mô hình đã huấn luyện và lưu thành công.")

if __name__ == "__main__":
    main()
