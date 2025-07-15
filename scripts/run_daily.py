import subprocess
from datetime import datetime

def run_script(path: str, description: str) -> dict:
    print(f"\n🚀 Bắt đầu: {description}")
    start = datetime.now()

    try:
        result = subprocess.run(
            ["python", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",      # 🛠 fix lỗi tiếng Việt khi đọc stdout/stderr
            errors="replace"       # 🛡 tránh UnicodeDecodeError
        )

        end = datetime.now()
        duration = (end - start).seconds

        stdout = result.stdout.strip() if result.stdout else ""
        stderr = result.stderr.strip() if result.stderr else ""
        returncode = result.returncode
        success = returncode == 0

        if success:
            print(f"✅ {description} đã chạy xong sau {duration} giây.")
        else:
            print(f"❌ {description} lỗi (exit code {returncode})")
            if stderr:
                print("🧨 STDERR:\n" + stderr)

        return {
            "step": description,
            "script": path,
            "success": success,
            "returncode": returncode,
            "stdout": stdout,
            "stderr": stderr,
            "duration": duration,
        }

    except Exception as e:
        print(f"🔥 Exception subprocess: {e}")
        return {
            "step": description,
            "script": path,
            "success": False,
            "returncode": -1,
            "stdout": "",
            "stderr": str(e),
            "duration": 0,
        }

def main():
    print(f"\n📅 [RUN DAILY] Ngày {datetime.now().strftime('%Y-%m-%d')}")
    print("🧠 Bắt đầu pipeline: Insert → Label → Evaluate")

    steps = [
        ("scripts/insert_ai_signals.py", "Tạo tín hiệu mới"),
        ("scripts/label_ai_signals.py", "Gắn nhãn thắng/thua"),
        ("scripts/evaluate_ai_accuracy.py", "Đánh giá độ chính xác AI"),
    ]

    summary = []

    for path, desc in steps:
        result = run_script(path, desc)
        summary.append(result)

        if not result["success"]:
            print(f"🛑 Dừng pipeline tại bước: {desc}")
            break

    print("\n📋 Tổng kết:")
    for step in summary:
        status = "✅ Thành công" if step["success"] else "❌ Thất bại"
        print(f" - {step['step']}: {status} ({step['duration']}s)")

    if all(s["success"] for s in summary):
        print("\n🎯 Toàn bộ pipeline AI đã chạy thành công! Ready to conquer the market.")
    else:
        print("\n⚠️ Một hoặc nhiều bước gặp lỗi. Xem log chi tiết.")

if __name__ == "__main__":
    main()
