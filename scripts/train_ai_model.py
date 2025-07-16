import os
import sys
import pandas as pd
import xgboost as xgb
import joblib
from supabase import create_client, Client
from dotenv import load_dotenv
from sklearn.metrics import classification_report, confusion_matrix
from sklearn.model_selection import train_test_split

# ✅ Unicode cho Windows terminal
sys.stdout.reconfigure(encoding='utf-8')

# ✅ Load biến môi trường
load_dotenv()

# 🔐 Kiểm tra biến môi trường
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    print("❌ Thiếu SUPABASE_URL hoặc SUPABASE_SERVICE_ROLE_KEY")
    sys.exit(1)

# 🔗 Kết nối Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

def fetch_data():
    print("📥 Đang tải dữ liệu từ Supabase...")
    try:
        res = supabase.table("ai_signals").select("*").execute()
        if not res.data:
            print("⚠️ Không có dữ liệu trả về.")
            return pd.DataFrame()
        df = pd.DataFrame(res.data)
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

    df = df[expected].copy()

    for col in expected:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna()

    label_counts = df["label_win"].value_counts()
    print(f"📊 Phân phối nhãn:\n{label_counts}")
    if len(label_counts) < 2:
        print("❌ label_win không đủ đa dạng (chỉ có 1 loại nhãn).")
        return pd.DataFrame()

    print(f"✅ Dữ liệu sau xử lý: {len(df)} dòng")
    return df

def train_model(df):
    X = df.drop("label_win", axis=1)
    y = df["label_win"].astype(int)

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y
    )

    model = xgb.XGBClassifier(
        n_estimators=100,
        max_depth=5,
        learning_rate=0.1,
        eval_metric="logloss",
        random_state=42
    )

    model.fit(X_train, y_train)
    y_pred = model.predict(X_test)

    print("📋 Classification Report:")
    print(classification_report(y_test, y_pred))
    print("🧾 Confusion Matrix:")
    print(confusion_matrix(y_test, y_pred))

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
