import sys
import json
import pandas as pd
import traceback
from supabase import create_client, Client
from dotenv import load_dotenv

REQUIRED_COLS = {"symbol", "date", "ai_predicted_probability", "ai_recommendation"}

def read_input():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ValueError("Dữ liệu đầu vào phải là list các dict")
        return pd.DataFrame(data)
    except Exception as e:
        print(json.dumps({"error": f"❌ Lỗi đọc input JSON: {e}"}), file=sys.stderr)
        sys.exit(1)

def validate_and_prepare(df: pd.DataFrame) -> pd.DataFrame:
    missing = REQUIRED_COLS - set(df.columns)
    if missing:
        raise ValueError(f"Thiếu các cột: {', '.join(missing)}")

    df = df.copy()
    df["ai_predicted_probability"] = pd.to_numeric(df["ai_predicted_probability"], errors="coerce")
    df["ai_recommendation"] = df["ai_recommendation"].astype(str).str.upper()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")

    # Loại bỏ dòng thiếu giá trị cốt lõi
    df = df.dropna(subset=["symbol", "date", "ai_predicted_probability"])
    return df

def get_latest_signals(df: pd.DataFrame) -> pd.DataFrame:
    latest = df["date"].max()
    df_latest = df[df["date"] == latest].copy()
    df_latest = df_latest.sort_values(by="ai_predicted_probability", ascending=False)
    df_latest = df_latest.drop_duplicates(subset="symbol", keep="first")
    return df_latest

def allocate_portfolio(df: pd.DataFrame) -> list:
    buy_df = df[df["ai_recommendation"] == "BUY"].copy()

    if not buy_df.empty:
        total = buy_df["ai_predicted_probability"].sum()
        if total > 0:
            buy_df["allocation"] = buy_df["ai_predicted_probability"] / total
        else:
            buy_df["allocation"] = 1.0 / len(buy_df)
        buy_df["recommendation"] = "BUY"
        print("✅ Có mã BUY → Phân bổ theo xác suất", file=sys.stderr)
        return buy_df[["symbol", "ai_predicted_probability", "recommendation", "allocation"]] \
            .rename(columns={"ai_predicted_probability": "probability"}) \
            .to_dict(orient="records")

    # fallback → WATCH 3 mã top xác suất
    fallback = df.head(3).copy()
    fallback["recommendation"] = "WATCH"
    fallback["allocation"] = 1.0 / len(fallback) if len(fallback) > 0 else 0
    print("🟡 Không có mã BUY → fallback sang WATCH", file=sys.stderr)
    return fallback[["symbol", "ai_predicted_probability", "recommendation", "allocation"]] \
        .rename(columns={"ai_predicted_probability": "probability"}) \
        .to_dict(orient="records")

def main():
    try:
        df = read_input()
        df = validate_and_prepare(df)
        df = get_latest_signals(df)

        if df.empty:
            print(json.dumps({"message": "⚠️ Không có dữ liệu hợp lệ để tối ưu"}))
            return

        result = allocate_portfolio(df)
        print(json.dumps(result, ensure_ascii=False))

    except Exception as e:
        print(json.dumps({
            "error": str(e),
            "trace": traceback.format_exc()
        }), file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
