# scripts/predict.py

import sys
import json
import joblib
import numpy as np

# Đọc tham số từ dòng lệnh
if len(sys.argv) < 3:
    print(json.dumps({ "error": "❌ Thiếu tham số đầu vào." }))
    sys.exit(1)

model_path = sys.argv[1]
features_json = sys.argv[2]

# Load model
try:
    model = joblib.load(model_path)
except Exception as e:
    print(json.dumps({ "error": f"❌ Lỗi khi load model: {str(e)}" }))
    sys.exit(1)

# Parse JSON features
try:
    features = json.loads(features_json)
    X = np.array([[ 
        features['close'],
        features['volume'],
        features['ma20'],
        features['rsi'],
        features['bb_upper'],
        features['bb_lower'],
        features['foreign_buy_value'],
        features['foreign_sell_value']
    ]])
except Exception as e:
    print(json.dumps({ "error": f"❌ Lỗi xử lý dữ liệu vào: {str(e)}" }))
    sys.exit(1)

# Dự đoán
try:
    prob = model.predict_proba(X)[0][1]
    recommend = "BUY" if prob > 0.6 else "SELL" if prob < 0.4 else "HOLD"

    print(json.dumps({
        "probability": float(prob),
        "recommendation": recommend
    }))
except Exception as e:
    print(json.dumps({ "error": f"❌ Lỗi khi predict: {str(e)}" }))
    sys.exit(1)
