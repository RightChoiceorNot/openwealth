"""抓常見美股的最新收盤價（USD），轉換成 TWD，合併進 stock_prices.json。

API：Yahoo Finance v8 chart endpoint (https://query1.finance.yahoo.com/v8/finance/chart/<TICKER>)
- 不需要 auth
- 一次一檔
- 同時拿 USD/TWD 匯率（ticker = TWD=X 或 USDTWD=X）

申報的英文公司名稱跟 ticker 對照表：根據實際申報資料常見寫法手工維護。
"""
import json
import re
import sys
import time
from pathlib import Path

import requests

import os

ROOT = Path(__file__).resolve().parent.parent
STOCK_PRICES_PATHS = [
    ROOT / "data" / "stock_prices.json",
    ROOT / "deploy" / "data" / "stock_prices.json",
]
if os.environ.get("GITHUB_ACTIONS"):
    STOCK_PRICES_PATHS = [ROOT / "data" / "stock_prices.json"]

# 申報中常見的美股英文寫法 → ticker
# key 是 normalize (去空格/大寫) 之後的字串，會跟 _norm_us 後的申報名 match
US_ALIASES = {
    # 科技
    "APPLE":           "AAPL",
    "APPLEINC":        "AAPL",
    "MICROSOFT":       "MSFT",
    "MICROSOFTCO":     "MSFT",
    "MICROSOFTCORP":   "MSFT",
    "ALPHABET":        "GOOGL",
    "ALPHABETINC":     "GOOGL",
    "GOOGLE":          "GOOGL",
    "AMAZON":          "AMZN",
    "AMAZONCOM":       "AMZN",
    "META":            "META",
    "METAPLATFORMS":   "META",
    "FACEBOOK":        "META",
    "NVIDIA":          "NVDA",
    "NVIDIACORP":      "NVDA",
    "TESLA":           "TSLA",
    "NETFLIX":         "NFLX",
    "BROADCOM":        "AVGO",
    "BROADCOMLTD":     "AVGO",
    "BROADCOMINC":     "AVGO",
    "ADVANCEDMICRO":   "AMD",
    "AMD":             "AMD",
    "ADVANCEDMICRODEVICES": "AMD",
    "INTEL":           "INTC",
    "INTELCORP":       "INTC",
    "ORACLE":          "ORCL",
    "CISCO":           "CSCO",
    "CISCOSYSTEMS":    "CSCO",
    "QUALCOMM":        "QCOM",
    "ADOBE":           "ADBE",
    "SALESFORCE":      "CRM",
    "IBM":             "IBM",
    "PALANTIR":        "PLTR",
    "ARM":             "ARM",
    "ARMHOLDINGS":     "ARM",
    "ASML":            "ASML",
    "TSM":             "TSM",
    "TSMC":            "TSM",
    "TAIWANSEMI":      "TSM",
    "MICRON":          "MU",
    "TEXASINSTRUMENTS":"TXN",
    "APPLIEDMATERIALS":"AMAT",
    "LAMRESEARCH":     "LRCX",
    "KLA":             "KLAC",
    "MARVELL":         "MRVL",
    "ANALOGDEVICES":   "ADI",
    "MICROCHIP":       "MCHP",
    "AVGO":            "AVGO",
    # 金融
    "BERKSHIRE":       "BRK-B",
    "BERKSHIREHATHAWAY":"BRK-B",
    "JPMORGAN":        "JPM",
    "JPMORGANCHASE":   "JPM",
    "VISA":            "V",
    "MASTERCARD":      "MA",
    "BANKOFAMERICA":   "BAC",
    "WELLSFARGO":      "WFC",
    "GOLDMAN":         "GS",
    "MORGANSTANLEY":   "MS",
    "AMERICANEXPRESS": "AXP",
    "BLACKROCK":       "BLK",
    # 醫療
    "JOHNSONJOHNSON":  "JNJ",
    "JOHNSONNJOHNSON": "JNJ",
    "PFIZER":          "PFE",
    "MERCK":           "MRK",
    "ABBVIE":          "ABBV",
    "ELI LILLY":       "LLY",
    "ELILILLY":        "LLY",
    "UNITEDHEALTH":    "UNH",
    "NOVONORDISK":     "NVO",
    # 消費
    "WALMART":         "WMT",
    "COSTCO":          "COST",
    "TARGET":          "TGT",
    "NIKE":            "NKE",
    "MCDONALDS":       "MCD",
    "STARBUCKS":       "SBUX",
    "COCACOLA":        "KO",
    "PEPSI":           "PEP",
    "PEPSICO":         "PEP",
    "PROCTERGAMBLE":   "PG",
    "PROCTORGAMBLE":   "PG",
    "DISNEY":          "DIS",
    "WALTDISNEY":      "DIS",
    # 能源/工業
    "EXXONMOBIL":      "XOM",
    "CHEVRON":         "CVX",
    "BOEING":          "BA",
    "CATERPILLAR":     "CAT",
    "GENERALELECTRIC": "GE",
    "GENERALMOTORS":   "GM",
    "FORD":            "F",
    # ETF
    "SPDRSP500":       "SPY",
    "SPDR":            "SPY",
    "INVESCOQQQ":      "QQQ",
    "QQQ":             "QQQ",
    "VANGUARD500":     "VOO",
    "VANGUARDS&P500":  "VOO",
    # 半導體/其他
    "ASML HOLDING":    "ASML",
    "ASMLHOLDING":     "ASML",
    # 補：GOOG (Alphabet C 股，跟 GOOGL 不同代號但價格類似)
    "GOOG":            "GOOG",
}

# 自動把每個 ticker 本身也設成 alias (讓申報只寫 "AAPL" 等代號也能對到)
for _t in list(set(US_ALIASES.values())):
    US_ALIASES.setdefault(_t, _t)


def _norm_us(s: str) -> str:
    """正規化英文公司名供 match"""
    if not s:
        return ""
    n = s.upper()
    n = re.sub(r"\([^)]*\)", "", n)
    n = n.replace(",", "").replace(".", "").replace("&", "AND")
    n = re.sub(r"\b(INC|CORP|CORPORATION|CO|LTD|LIMITED|COMPANY|PLC|HOLDINGS?|GROUP|THE)\b", "", n)
    return re.sub(r"\s+", "", n).strip()


def fetch_quote(ticker: str) -> dict | None:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        if r.status_code != 200:
            return None
        meta = r.json()["chart"]["result"][0]["meta"]
        return {"price": meta["regularMarketPrice"], "currency": meta["currency"]}
    except Exception as e:
        print(f"  [!] {ticker}: {e}")
        return None


def main():
    sys.stdout.reconfigure(encoding="utf-8")

    # 1. USD/TWD 匯率
    print("Fetching USD/TWD rate ...")
    fx = fetch_quote("TWD=X")  # USD/TWD
    if not fx:
        fx = fetch_quote("USDTWD=X")
    usd_twd = fx["price"] if fx else 32.0
    print(f"  USD/TWD = {usd_twd}")

    # 2. 唯一 ticker (alias 可能多對一)
    unique_tickers = sorted(set(US_ALIASES.values()))
    print(f"Fetching {len(unique_tickers)} unique US tickers ...")
    ticker_price = {}  # ticker → {usd_price, twd_price, currency, date}
    for i, t in enumerate(unique_tickers):
        q = fetch_quote(t)
        if q:
            usd = q["price"]
            twd = usd * usd_twd if q["currency"] == "USD" else usd
            ticker_price[t] = {
                "usd_price": usd,
                "close": twd,
                "currency": q["currency"],
                "twd_close": twd,
            }
            print(f"  [{i+1}/{len(unique_tickers)}] {t}: ${usd} USD = NT${twd:.0f}")
        else:
            print(f"  [{i+1}/{len(unique_tickers)}] {t}: FAIL")
        time.sleep(0.15)  # 避免被 rate limit

    # 3. 合併進 stock_prices.json
    for sp_path in STOCK_PRICES_PATHS:
        if not sp_path.exists():
            print(f"  [skip] {sp_path} 不存在")
            continue
        data = json.loads(sp_path.read_text(encoding="utf-8"))
        by_name = data.setdefault("by_name", {})
        added = 0
        for alias_norm, ticker in US_ALIASES.items():
            if ticker not in ticker_price:
                continue
            info = ticker_price[ticker]
            entry = {
                "code": ticker,
                "close": info["close"],   # 已換算成 TWD
                "market": "US",
                "date": "",
                "usd_price": info["usd_price"],
                "fx_rate": usd_twd,
            }
            by_name[alias_norm] = entry
            added += 1
        data.setdefault("counts", {})["US"] = added
        data["fx_usd_twd"] = usd_twd
        sp_path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(f"  merged into {sp_path}: +{added} aliases ({sp_path.stat().st_size/1024:.1f} KB)")


if __name__ == "__main__":
    main()
