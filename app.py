from flask import Flask, request, jsonify
import subprocess
import joblib
import os
import numpy as np
import json
from dotenv import load_dotenv

try:
    from supabase import create_client
    print("✅ Đã import được create_client từ supabase")
except Exception as e:
    print("❌ Không import được create_client:", str(e))
# ─────────── Load biến môi trường ───────────
load_dotenv()

# ─────────── Khởi tạo Flask ───────────
app = Flask(__name__)

# ─────────── Load mô hình AI ───────────
MODEL_PATH = os.getenv("MODEL_PATH", "model/model.pkl")
model = None

try:
    model = joblib.load(MODEL_PATH)
    print(f"✅ Loaded model từ {MODEL_PATH}")
except Exception as e:
    print(f"❌ Lỗi khi load model từ {MODEL_PATH}: {str(e)}")

# ─────────── Predict cho 1 mã ───────────
@app.route("/predict", methods=["POST"])
def predict():
    if model is None:
        return jsonify({"error": "❌ Model chưa được load"}), 500

    try:
        data = request.get_json()

        expected_fields = [
            'close', 'volume', 'ma20', 'rsi',
            'bb_upper', 'bb_lower', 'foreign_buy_value', 'foreign_sell_value'
        ]

        features = []
        for field in expected_fields:
            if field not in data:
                return jsonify({"error": f"❌ Thiếu trường bắt buộc: {field}"}), 400
            try:
                features.append(float(data[field]))
            except Exception:
                features.append(0)

        X = np.array([features])
        prob = model.predict_proba(X)[0][1]

        recommendation = (
            "MUA" if prob > 0.7 else
            "BÁN" if prob < 0.3 else
            "GIỮ"
        )

        return jsonify({
            "probability": round(float(prob), 4),
            "recommendation": recommendation
        })

    except Exception as e:
        print("🔥 Predict error:", str(e))
        return jsonify({"error": f"❌ Lỗi xử lý dữ liệu: {str(e)}"}), 500

# ─────────── Train mô hình ───────────
@app.route("/train", methods=["POST"])
def train_model():
    try:
        result = subprocess.run(
            ["python", "scripts/train_ai_model.py"],
            capture_output=True,
            text=True
        )
        return jsonify({ "message": result.stdout or result.stderr })
    except Exception as e:
        return jsonify({ "error": f"Lỗi train model: {str(e)}" }), 500

# ─────────── Tối ưu danh mục ───────────
@app.route("/optimize", methods=["POST"])
def optimize():
    try:
        result = subprocess.run(
            ["python", "scripts/portfolio_optimizer.py"],
            capture_output=True,
            text=True
        )
        return jsonify({ "message": result.stdout or result.stderr })
    except Exception as e:
        return jsonify({ "error": f"Lỗi optimize: {str(e)}" }), 500

# ─────────── Dự đoán toàn bộ ───────────
@app.route("/predict_all", methods=["POST"])
def predict_all():
    try:
        result = subprocess.run(
            ["python", "scripts/predict_all.py"],
            capture_output=True,
            text=True
        )
        return jsonify({ "message": result.stdout or result.stderr })
    except Exception as e:
        return jsonify({ "error": f"Lỗi predict_all: {str(e)}" }), 500

@app.route("/portfolio", methods=["POST"])
def portfolio():
    try:
        # 📥 Nhận dữ liệu đầu vào từ client
        raw_data = request.get_json()
        if not raw_data or "userId" not in raw_data:
            return jsonify({"error": "❌ Thiếu userId trong request!"}), 400

        user_id = raw_data["userId"]

        # 🌐 Lấy biến môi trường
        supabase_url = os.getenv("SUPABASE_URL")
        supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

        if not supabase_url or not supabase_key:
            return jsonify({"error": "❌ Thiếu SUPABASE_URL hoặc SUPABASE_SERVICE_ROLE_KEY"}), 500

        # 🔌 Tạo client Supabase
        if not create_client:
            return jsonify({ "error": "⚠️ create_client chưa được import — kiểm tra gói supabase" }), 500

        sb = create_client(supabase_url, supabase_key)
        print(f"✅ Supabase client created for user {user_id}")

        # 📊 Lấy dữ liệu từ bảng ai_signals
        resp = sb.table("ai_signals").select("*") \
            .eq("user_id", user_id) \
            .order("date", desc=True) \
            .execute()

        records = resp.data or []

        if not records:
            return jsonify({"error": "❌ Không có dữ liệu AI signals cho user này"}), 404

        # ⚙️ Gọi script portfolio_optimizer.py
        input_json = json.dumps(records)
        p = subprocess.Popen(
            ["python", "scripts/portfolio_optimizer.py"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        stdout, stderr = p.communicate(input=input_json)

        if p.returncode != 0:
            return jsonify({
                "error": "❌ portfolio_optimizer.py trả lỗi",
                "stderr": stderr.strip()
            }), 500

        try:
            portfolio = json.loads(stdout)
        except json.JSONDecodeError:
            return jsonify({
                "error": "❌ Không parse được kết quả JSON từ optimizer",
                "raw_output": stdout
            }), 500

        return jsonify({
            "date": records[0]["date"],
            "portfolio": portfolio
        })

    except Exception as e:
        traceback_str = traceback.format_exc()
        return jsonify({
            "error": f"🔥 Lỗi xử lý portfolio: {str(e)}",
            "trace": traceback_str
        }), 500
     

# ─────────── Endpoint kiểm tra ───────────
@app.route("/", methods=["GET"])
def home():
    return "✅ LHP-AI-SERVER đang hoạt động!"

# ─────────── Chạy server ───────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
