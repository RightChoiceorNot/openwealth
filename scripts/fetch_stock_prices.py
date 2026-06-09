"""拉 TWSE 上市 + TPEx 上櫃 + TPEx 興櫃 收盤價，輸出 data/stock_prices.json

格式：
{
  "by_name": {
    "台積電":   {"code": "2330", "close": 2355.0, "market": "TWSE", "date": "1150529"},
    ...
  },
  "by_code": {
    "2330":     {"name": "台積電", "close": 2355.0, "market": "TWSE", "date": "1150529"},
    ...
  },
  "updated_at": "2025-05-30"
}
"""

import json
import os
import re
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT_PATHS = [
    ROOT / "data" / "stock_prices.json",
    ROOT / "deploy" / "data" / "stock_prices.json",
]
if os.environ.get("GITHUB_ACTIONS"):
    OUT_PATHS = [ROOT / "data" / "stock_prices.json"]


def safe_float(v) -> float | None:
    if v is None or v == "" or v == "--":
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def fetch_twse():
    """上市每日收盤"""
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    r = requests.get(url, timeout=30)
    out = []
    for s in r.json():
        close = safe_float(s.get("ClosingPrice"))
        if close is None or close <= 0:
            continue
        out.append({
            "code": str(s.get("Code", "")).strip(),
            "name": (s.get("Name") or "").strip(),
            "close": close,
            "market": "TWSE",
            "date": s.get("Date", ""),
        })
    return out


def fetch_tpex_main():
    """上櫃每日收盤"""
    url = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
    r = requests.get(url, timeout=30)
    out = []
    for s in r.json():
        close = safe_float(s.get("Close"))
        if close is None or close <= 0:
            continue
        out.append({
            "code": str(s.get("SecuritiesCompanyCode", "")).strip(),
            "name": (s.get("CompanyName") or "").strip(),
            "close": close,
            "market": "TPEx",
            "date": s.get("Date", ""),
        })
    return out


def fetch_tpex_esb():
    """興櫃 — 用 LatestPrice，falls back 到 Average"""
    url = "https://www.tpex.org.tw/openapi/v1/tpex_esb_latest_statistics"
    r = requests.get(url, timeout=30)
    out = []
    for s in r.json():
        close = safe_float(s.get("LatestPrice")) or safe_float(s.get("Average")) or safe_float(s.get("PreviousAveragePrice"))
        if close is None or close <= 0:
            continue
        out.append({
            "code": str(s.get("SecuritiesCompanyCode", "")).strip(),
            "name": (s.get("CompanyName") or "").strip(),
            "close": close,
            "market": "ESB",
            "date": s.get("Date", ""),
        })
    return out


def _fetch_tw_via_yfinance(cur_by_code: dict) -> list:
    """
    官方 API 無法連線時的備援：用 yfinance 從 Yahoo Finance 拉台股。
    TWSE → ticker + ".TW"
    TPEx → ticker + ".TWO"
    興櫃(ESB) 在 Yahoo Finance 無對應，略過。
    """
    try:
        import yfinance as yf
    except ImportError:
        print("  [yfinance] 未安裝，無法使用 fallback")
        return []

    SUFFIX = {"TWSE": ".TW", "TPEx": ".TWO"}
    ticker_map = {}  # yf_ticker -> (code, name, market)
    for code, info in cur_by_code.items():
        market = info.get("market", "")
        sfx = SUFFIX.get(market)
        if not sfx:
            continue
        # 只處理純數字股票代號（忽略特殊商品）
        if not re.match(r'^\d{4,6}[A-Z]?$', str(code)):
            continue
        # 跳過受益憑證基金代號（70xxxx）：Yahoo Finance 不支援，會全部噴 "possibly delisted"
        if re.match(r'^7\d{5}$', str(code)):
            continue
        ticker_map[str(code) + sfx] = (str(code), info.get("name", ""), market)

    if not ticker_map:
        print("  [yfinance] existing data 中無 TWSE/TPEx 代號")
        return []

    tickers = list(ticker_map.keys())
    today_str = date.today().isoformat()
    results = []

    BATCH = 400
    total = len(tickers)
    for i in range(0, total, BATCH):
        batch = tickers[i : i + BATCH]
        done = min(i + BATCH, total)
        print(f"  yfinance [{done}/{total}] ({done*100//total}%)...", flush=True)
        try:
            raw = yf.download(
                tickers=" ".join(batch),
                period="5d",
                auto_adjust=True,
                progress=False,
            )
            for t in batch:
                code, name, market = ticker_map[t]
                try:
                    if len(batch) == 1:
                        close_series = raw["Close"].dropna()
                    else:
                        close_series = raw["Close"][t].dropna()
                    if close_series.empty:
                        continue
                    close = round(float(close_series.iloc[-1]), 2)
                    if close <= 0:
                        continue
                    results.append({"code": code, "name": name, "close": close,
                                    "market": market, "date": today_str})
                except (KeyError, IndexError, TypeError):
                    pass
        except Exception as e:
            print(f"    batch error: {e}")

    twse_n = sum(1 for r in results if r["market"] == "TWSE")
    tpex_n = sum(1 for r in results if r["market"] == "TPEx")
    print(f"  → yfinance: TWSE {twse_n:,} / TPEx {tpex_n:,} 筆更新")
    return results


def main():
    # 先讀現有資料，避免某個來源 timeout 時蓋掉其他來源（SITCA/US）的舊值
    existing = {}
    for p in OUT_PATHS:
        if p.exists():
            try:
                existing = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
            break

    by_name = dict(existing.get("by_name") or {})
    by_code = dict(existing.get("by_code") or {})
    old_counts = dict(existing.get("counts") or {})
    counts = dict(old_counts)

    fetchers = [
        ("TWSE 上市",  "TWSE",  fetch_twse),
        ("TPEx 上櫃",  "TPEx",  fetch_tpex_main),
        ("TPEx 興櫃",  "ESB",   fetch_tpex_esb),
    ]

    any_ok = False
    for label, market_key, fn in fetchers:
        print(f"Fetching {label} ...")
        try:
            rows = fn()
            # 先清掉此 market 的舊條目，再寫入新值
            by_name = {k: v for k, v in by_name.items() if v.get("market") != market_key}
            by_code = {k: v for k, v in by_code.items() if v.get("market") != market_key}
            for s in rows:
                if s["name"]:
                    by_name[s["name"]] = {"code": s["code"], "close": s["close"],
                                          "market": s["market"], "date": s["date"]}
                if s["code"]:
                    by_code[s["code"]] = {"name": s["name"], "close": s["close"],
                                          "market": s["market"], "date": s["date"]}
            counts[market_key] = len(rows)
            print(f"  OK: {len(rows):,} 檔")
            any_ok = True
        except Exception as e:
            print(f"  SKIP ({e.__class__.__name__}: {e})")
            print(f"  → 保留 {old_counts.get(market_key, 0):,} 筆舊資料")

    # 官方 API 全部失敗 → yfinance fallback
    if not any_ok:
        print("\n[INFO] 官方 API 均無法連線，改用 yfinance (Yahoo Finance) 更新台股...")
        # 注意：此時 by_code 仍是舊資料（loop 內的清除只在成功時執行）
        fallback_rows = _fetch_tw_via_yfinance(by_code)
        if fallback_rows:
            for s in fallback_rows:
                if s["name"]:
                    by_name[s["name"]] = {"code": s["code"], "close": s["close"],
                                          "market": s["market"], "date": s["date"]}
                if s["code"]:
                    by_code[s["code"]] = {"name": s["name"], "close": s["close"],
                                          "market": s["market"], "date": s["date"]}
            counts["TWSE"] = sum(1 for r in fallback_rows if r["market"] == "TWSE")
            counts["TPEx"]  = sum(1 for r in fallback_rows if r["market"] == "TPEx")
            any_ok = True
        else:
            print("[WARN] yfinance fallback 也失敗，stock_prices.json 維持原樣。")
            return  # exit code 0

    payload = {
        "by_name": by_name,
        "by_code": by_code,
        "updated_at": date.today().isoformat(),
        "counts": counts,
        "fx_usd_twd": existing.get("fx_usd_twd"),
    }

    for p in OUT_PATHS:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(f"Wrote {p}  ({p.stat().st_size/1024:.1f} KB)")

    print(f"\nDone: {len(by_name):,} 個唯一公司名 / {len(by_code):,} 個股票代號")


if __name__ == "__main__":
    main()
