import sys
import json
import pandas as pd

def read_input():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw)

        if not isinstance(data, list):
            raise ValueError("Dữ liệu đầu vào phải là list các dict")

        return data
    except Exception as e:
        print(json.dumps({"error": f"❌ Lỗi đọc input JSON: {e}"}), file=sys.stderr)
        sys.exit(1)

def validate_data(df: pd.DataFrame):
    required_cols = {'symbol', 'ai_predicted_probability', 'ai_recommendation'}
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(f"Thiếu các cột: {', '.join(missing)}")

    df = df[df['ai_predicted_probability'].notnull()].copy()
    df['ai_predicted_probability'] = pd.to_numeric(df['ai_predicted_probability'], errors='coerce')
    df['ai_recommendation'] = df['ai_recommendation'].astype(str)

    df = df[df['ai_predicted_probability'].notnull()]
    return df

def select_portfolio(df: pd.DataFrame):
    df = df.sort_values(by='ai_predicted_probability', ascending=False)

    buy_df = df[df['ai_recommendation'].str.upper() == 'BUY'].copy()

    if not buy_df.empty:
        total_prob = buy_df['ai_predicted_probability'].sum()
        buy_df['allocation'] = buy_df['ai_predicted_probability'] / total_prob
        buy_df['recommendation'] = 'BUY'
        print("✅ Có mã BUY → Phân bổ theo xác suất", file=sys.stderr)

        return buy_df[['symbol', 'ai_predicted_probability', 'recommendation', 'allocation']] \
            .rename(columns={'ai_predicted_probability': 'probability'}) \
            .to_dict(orient='records')

    fallback = df.head(3).copy()
    fallback['recommendation'] = 'WATCH'
    fallback['allocation'] = 1.0 / len(fallback) if len(fallback) > 0 else 0
    print("🟡 Không có mã BUY → fallback sang WATCH", file=sys.stderr)

    return fallback[['symbol', 'ai_predicted_probability', 'recommendation', 'allocation']] \
        .rename(columns={'ai_predicted_probability': 'probability'}) \
        .to_dict(orient='records')

def main():
    try:
        raw_data = read_input()
        df = pd.DataFrame(raw_data)

        df = validate_data(df)
        if df.empty:
            print(json.dumps({"message": "⚠️ Không có dữ liệu hợp lệ để tối ưu"}))
            return

        result = select_portfolio(df)
        print(json.dumps(result, ensure_ascii=False))

    except Exception as e:
        import traceback
        print(json.dumps({
            "error": str(e),
            "trace": traceback.format_exc()
        }), file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
