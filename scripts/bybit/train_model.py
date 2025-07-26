import os
import pandas as pd
import numpy as np
from supabase import create_client, Client
from dotenv import load_dotenv
from sklearn.model_selection import train_test_split
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report, accuracy_score, confusion_matrix
import joblib

# ===== 1. Load biến môi trường & kết nối Supabase =====
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# ===== 2. Lấy dữ liệu huấn luyện từ Supabase =====
def fetch_training_data(symbols=None):
    print("📥 Đang tải dữ liệu huấn luyện từ Supabase...")
    try:
        query = supabase.table("training_dataset").select("*")
        if symbols:
            query = query.in_("symbol", symbols)
        res = query.execute()
        if not res.data:
            raise Exception("❌ Không có dữ liệu training.")
        return pd.DataFrame(res.data)
    except Exception as e:
        raise Exception(f"❌ Lỗi khi tải dữ liệu training: {e}")

# ===== 3. Tiền xử lý dữ liệu =====
def preprocess(df: pd.DataFrame):
    if 'signal' not in df.columns:
        raise Exception("⚠️ Dữ liệu không chứa cột 'signal'.")

    drop_cols = ['id', 'symbol', 'created_at', 'target', 'signal']
    X = df.drop(columns=drop_cols, errors='ignore')

    # Xử lý đặc trưng
    X = X.apply(pd.to_numeric, errors='coerce')
    X = X.replace([np.inf, -np.inf], np.nan)
    X.dropna(inplace=True)

    # 🎯 Dữ liệu đầu ra
    y = df.loc[X.index, 'signal'].astype(int)

    print(f"📊 Dữ liệu đầu vào X shape: {X.shape}")
    print(f"🎯 Các nhãn y duy nhất: {y.unique()}")

    return X, y

# ===== 4. Huấn luyện mô hình Random Forest =====
def train_model(X_train, y_train):
    print("🧠 Đang huấn luyện mô hình Random Forest...")
    model = RandomForestClassifier(
        n_estimators=200,
        max_depth=10,
        random_state=42,
        n_jobs=-1,
        class_weight='balanced'
    )
    model.fit(X_train, y_train)
    return model

# ===== 5. Đánh giá mô hình =====
def evaluate_model(model, X_test, y_test):
    print("\n=== 📊 ĐÁNH GIÁ MÔ HÌNH ===")
    preds = model.predict(X_test)

    print(classification_report(
        y_test, preds,
        target_names=['SELL (-1)', 'HOLD (0)', 'BUY (1)'],
        labels=[-1, 0, 1]
    ))

    acc = accuracy_score(y_test, preds)
    print(f"🎯 Accuracy: {acc:.4f}")

    print("🧩 Confusion Matrix:")
    print(confusion_matrix(y_test, preds, labels=[-1, 0, 1]))

# ===== 6. Lưu mô hình ra file .pkl =====
def save_model(model, path="model/model_rf.pkl"):
    try:
        joblib.dump(model, path)
        print(f"✅ Mô hình đã được lưu tại: {path}")
    except Exception as e:
        print(f"❌ Lỗi khi lưu mô hình: {e}")

# ===== 7. Chạy pipeline huấn luyện =====
def run():
    symbols = None  # Ví dụ: ['BTCUSDT', 'ETHUSDT']
    df = fetch_training_data(symbols)
    print(f"📊 Tổng số dòng dữ liệu huấn luyện: {len(df)}")

    X, y = preprocess(df)

    print("🔀 Đang chia dữ liệu 80/20 cho train/test...")
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, stratify=y, random_state=42
    )

    model = train_model(X_train, y_train)
    evaluate_model(model, X_test, y_test)
    save_model(model)

# ===== 8. Entry Point =====
if __name__ == "__main__":
    run()
