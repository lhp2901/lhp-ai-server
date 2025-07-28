import os
import pandas as pd
import numpy as np
from dotenv import load_dotenv
from sklearn.model_selection import train_test_split, StratifiedKFold
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report, accuracy_score, confusion_matrix, f1_score, precision_score, recall_score
from sklearn.preprocessing import StandardScaler
import joblib
from supabase import create_client, Client

# ===== 1. Load env & kết nối Supabase =====
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# ===== 2. Tải dữ liệu từ Supabase =====
def fetch_training_data(symbols=None):
    print("\U0001F4E5 Đang tải dữ liệu huấn luyện từ Supabase...")
    try:
        query = supabase.table("training_dataset").select("*")
        if symbols:
            query = query.in_("symbol", symbols)
        res = query.execute()
        if not res.data:
            raise Exception("❌ Không có dữ liệu huấn luyện.")
        return pd.DataFrame(res.data)
    except Exception as e:
        raise Exception(f"❌ Lỗi khi tải dữ liệu training: {e}")

# ===== 3. Tiền xử lý dữ liệu =====
def preprocess(df: pd.DataFrame):
    print("\U0001F50D Đang tiền xử lý dữ liệu...")

    if 'signal' not in df.columns:
        raise Exception("⚠️ Dữ liệu không chứa cột 'signal'.")

    drop_cols = ['id', 'symbol', 'created_at', 'target', 'signal']
    X = df.drop(columns=drop_cols, errors='ignore')

    X = X.apply(pd.to_numeric, errors='coerce')
    X = X.replace([np.inf, -np.inf], np.nan)
    X.dropna(inplace=True)

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)
    X = pd.DataFrame(X_scaled, columns=X.columns, index=X.index)

    y = df.loc[X.index, 'signal'].astype(int)

    print(f"\U0001F4CA Shape đầu vào X: {X.shape}")
    print(f"\U0001F3AF Nhãn y duy nhất: {y.unique()}")

    return X, y, scaler

# ===== 4. Huấn luyện mô hình =====
def train_model(X_train, y_train):
    print("\U0001F9E0 Đang huấn luyện RandomForest...")
    model = RandomForestClassifier(
        n_estimators=300,
        max_depth=12,
        random_state=42,
        n_jobs=-1,
        class_weight='balanced_subsample'
    )
    model.fit(X_train, y_train)
    return model

# ===== 5. Lưu báo cáo phân loại =====
def save_report(y_true, y_pred, filename="model/report.csv"):
    report = classification_report(
        y_true, y_pred,
        output_dict=True,
        zero_division=0
    )
    df_report = pd.DataFrame(report).transpose()
    df_report.to_csv(filename)
    print(f"📁 Đã lưu classification report tại {filename}")

# ===== 6. Đánh giá mô hình =====
def evaluate_model(model, X_test, y_test):
    print("\n=== \U0001F4CA ĐÁNH GIÁ MÔ HÌNH ===")
    preds = model.predict(X_test)

    print(classification_report(
        y_test, preds,
        target_names=['SELL (-1)', 'HOLD (0)', 'BUY (1)'],
        labels=[-1, 0, 1],
        zero_division=0
    ))

    print(f"\U0001F3AF Accuracy: {accuracy_score(y_test, preds):.4f}")
    print(f"\U0001F3AF Macro F1: {f1_score(y_test, preds, average='macro'):.4f}")
    print(f"\U0001F3AF Macro Precision: {precision_score(y_test, preds, average='macro', zero_division=0):.4f}")
    print(f"\U0001F3AF Macro Recall: {recall_score(y_test, preds, average='macro', zero_division=0):.4f}")

    print("\U0001F9E9 Confusion Matrix:")
    print(confusion_matrix(y_test, preds, labels=[-1, 0, 1]))

    save_report(y_test, preds)

# ===== 7. Đánh giá chéo =====
def cross_validate(X, y):
    print("\U0001F501 Đánh giá chéo với StratifiedKFold (5 folds)...")
    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    scores = []

    for fold, (train_idx, test_idx) in enumerate(skf.split(X, y)):
        X_train, X_test = X.iloc[train_idx], X.iloc[test_idx]
        y_train, y_test = y.iloc[train_idx], y.iloc[test_idx]

        model = train_model(X_train, y_train)
        preds = model.predict(X_test)
        acc = accuracy_score(y_test, preds)
        scores.append(acc)
        print(f"🔹 Fold {fold+1}: Accuracy = {acc:.4f}")

    print(f"\U0001F4CA Trung bình: {np.mean(scores):.4f} ± {np.std(scores):.4f}")

# ===== 8. Lưu mô hình & scaler =====
def save_model(model, scaler, model_path="model/model_rf.pkl", scaler_path="model/scaler.pkl"):
    try:
        joblib.dump(model, model_path)
        joblib.dump(scaler, scaler_path)
        print(f"✅ Đã lưu mô hình tại: {model_path}")
        print(f"✅ Đã lưu scaler tại: {scaler_path}")
    except Exception as e:
        print(f"❌ Lỗi khi lưu mô hình hoặc scaler: {e}")

# ===== 9. Chạy pipeline huấn luyện =====
def run():
    symbols = None  # ['BTCUSDT', 'ETHUSDT']
    df = fetch_training_data(symbols)
    print(f"\U0001F4CA Số dòng dữ liệu: {len(df)}")

    X, y, scaler = preprocess(df)

    print("🔀 Chia dữ liệu 80/20 cho train/test...")
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, stratify=y, random_state=42
    )

    model = train_model(X_train, y_train)
    evaluate_model(model, X_test, y_test)
    cross_validate(X, y)
    save_model(model, scaler)

# ===== 10. Entry point =====
if __name__ == "__main__":
    run()