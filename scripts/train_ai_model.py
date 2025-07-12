import os
import sys
import pandas as pd
import xgboost as xgb
import joblib
from supabase import create_client, Client
from dotenv import load_dotenv

# ✅ Fix Unicode cho Windows
sys.stdout.reconfigure(encoding='utf-8')

# ✅ Load biến môi trường
load_dotenv()

# 🔐 Kiểm tra biến môi trường
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")  # Dùng key này mới đủ quyền

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    print("❌ Thiếu SUPABASE_URL hoặc SUPABASE_SERVICE_ROLE_KEY")
    sys.exit(1)

# 🔗 Kết nối Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

def fetch_data():
    print("📥 Đang tải dữ liệu từ Supabase...")
    try:
        res = supabase.table("ai_signals").select("*").execute()
        df = pd.DataFrame(res.data or [])
        print(f"📊 Tổng số dòng tải về: {len(df)}")
        return df
    except Exception as e:
        print(f"❌ Lỗi khi tải dữ liệu: {e}")
        return pd.DataFrame()

def preprocess(df):
    expected = [
        "close", "volume", "ma20", "rsi", "bb_upper", "bb_lower",
        "foreign_buy_value", "foreign_sell_value", "label_win"
    ]

    for col in expected:
        if col not in df.columns:
            print(f"⚠️ Thiếu cột {col} → tạo với giá trị 0")
            df[col] = 0

    df = df[expected]
    
    # Chuyển đổi về số
    for col in expected:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    
    df = df.dropna()

    # Bỏ nếu không đủ đa dạng nhãn
    label_counts = df["label_win"].value_counts()
    if len(label_counts) < 2:
        print("❌ label_win không đủ đa dạng (chỉ có 1 loại nhãn).")
        return pd.DataFrame()

    print(f"✅ Dữ liệu sau xử lý: {len(df)} dòng")
    return df

def train_model(df):
    X = df.drop("label_win", axis=1)
    y = df["label_win"].astype(int)
    model = xgb.XGBClassifier(n_estimators=100, max_depth=5, random_state=42)
    model.fit(X, y)
    return model

def save_model(model):
    os.makedirs("model", exist_ok=True)
    path = os.path.join("model", "model.pkl")
    joblib.dump(model, path)
    print(f"💾 Mô hình đã lưu tại: {path}")

def main():
    df = fetch_data()
    df = preprocess(df)

    if df.empty:
        print("❌ Không đủ dữ liệu để huấn luyện mô hình.")
        return

    model = train_model(df)
    save_model(model)
    print("🎉 Huấn luyện và lưu mô hình thành công!")

if __name__ == "__main__":
    main()
