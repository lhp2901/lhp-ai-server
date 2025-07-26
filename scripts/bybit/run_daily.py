import subprocess
import sys
import os
from datetime import datetime

# ✅ Đảm bảo đầu ra luôn in đúng UTF-8
os.environ["PYTHONIOENCODING"] = "utf-8"

def run_script(script_name: str) -> dict:
    print(f"\n🚀 ĐANG CHẠY: {script_name}")
    base_dir = os.path.dirname(os.path.realpath(__file__))
    script_path = os.path.join(base_dir, script_name)

    if not os.path.isfile(script_path):
        error_msg = f"❌ KHÔNG TÌM THẤY FILE: {script_path}"
        print(error_msg)
        return {
            "script": script_name,
            "success": False,
            "stdout": "",
            "stderr": error_msg,
        }

    try:
        completed = subprocess.run(
            [sys.executable, script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True
        )
        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()
        print(f"✅ {script_name} chạy xong!\n📤 Output:\n{stdout}")
        return {
            "script": script_name,
            "success": True,
            "stdout": stdout,
            "stderr": stderr
        }

    except subprocess.CalledProcessError as e:
        stdout = e.stdout.strip() if e.stdout else ""
        stderr = e.stderr.strip() if e.stderr else ""
        print(f"❌ LỖI khi chạy: {script_name} (Mã lỗi: {e.returncode})")
        print(f"\n📤 Output:\n{stdout}")
        print(f"\n📥 Error:\n{stderr}")
        return {
            "script": script_name,
            "success": False,
            "stdout": stdout,
            "stderr": stderr
        }

def main():
    print("🎯 BẮT ĐẦU QUY TRÌNH AI TRADING HÀNG NGÀY")
    print(f"🗓️ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"📁 Working dir: {os.path.dirname(os.path.realpath(__file__))}")

    scripts = [
        "generate_training_data.py",   # 📊 Sinh dữ liệu training từ OHLCV
        "train_model.py",              # 🤖 Huấn luyện mô hình AI
        "predict_signal.py",           # 🔮 Dự đoán buy/sell
        "ai_execute_signals.py"        # 💥 Ghi tín hiệu AI vào bảng
    ]

    summary = []
    for script in scripts:
        result = run_script(script)
        summary.append(result)
        if not result["success"]:
            print(f"🛑 DỪNG LẠI: Lỗi xảy ra tại script {script}")
            break

    print("\n📋 TỔNG KẾT:")
    for r in summary:
        status = "✅ Thành công" if r["success"] else "❌ Thất bại"
        print(f" - {r['script']}: {status}")

    if all(r["success"] for r in summary):
        print("\n🏁 ✅ TOÀN BỘ QUY TRÌNH ĐÃ CHẠY THÀNH CÔNG!")
    else:
        print("\n⚠️ Một số bước đã thất bại. Vui lòng kiểm tra lại logs.")

if __name__ == "__main__":
    main()
