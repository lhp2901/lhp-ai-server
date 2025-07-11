from flask import Flask, request, jsonify
import subprocess
import joblib
import os

from dotenv import load_dotenv
load_dotenv()

# ───────────────────────────────
app = Flask(__name__)

# Load model
model_path = os.getenv("MODEL_PATH", "model/model.pkl")
try:
    model = joblib.load(model_path)
except Exception as e:
    print(f"❌ Lỗi khi load model từ {model_path}:", str(e))
    model = None

# ──────────────── PREDICT ────────────────
@app.route("/predict", methods=["POST"])
def predict():
    if model is None:
        return jsonify({"error": "Model chưa load được!"}), 500

    try:
        data = request.get_json()

        features = [
            data["close"],
            data["volume"],
            data["ma20"],
            data["rsi"],
            data["bb_upper"],
            data["bb_lower"],
            data["foreign_buy_value"],
            data["foreign_sell_value"],
        ]

        proba = model.predict_proba([features])[0][1]
        recommendation = "MUA" if proba > 0.7 else "KHÔNG"

        return jsonify({
            "probability": round(proba, 4),
            "recommendation": recommendation
        })

    except Exception as e:
        return jsonify({"error": f"Lỗi xử lý dữ liệu: {str(e)}"}), 500

# ──────────────── TRAIN ────────────────
@app.route("/train", methods=["POST"])
def train_model():
    try:
        result = subprocess.run(
            ["python", "scripts/train_ai_model.py"],
            capture_output=True,
            text=True
        )
        return jsonify({"message": result.stdout or result.stderr})
    except Exception as e:
        return jsonify({"error": f"Lỗi train model: {str(e)}"}), 500

# ──────────────── OPTIMIZE ────────────────
@app.route("/optimize", methods=["POST"])
def optimize():
    try:
        result = subprocess.run(
            ["python", "scripts/portfolio_optimizer.py"],
            capture_output=True,
            text=True
        )
        return jsonify({"message": result.stdout or result.stderr})
    except Exception as e:
        return jsonify({"error": f"Lỗi optimize: {str(e)}"}), 500

# ──────────────── PREDICT ALL ────────────────
@app.route("/predict_all", methods=["POST"])
def predict_all():
    try:
        result = subprocess.run(
            ["python", "scripts/predict_all.py"],
            capture_output=True,
            text=True
        )
        return jsonify({"message": result.stdout or result.stderr})
    except Exception as e:
        return jsonify({"error": f"Lỗi predict_all: {str(e)}"}), 500

# ──────────────── CHẠY SERVER ────────────────
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
