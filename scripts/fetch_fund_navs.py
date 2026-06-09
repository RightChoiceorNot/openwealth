"""抓 SITCA 境內基金每日淨值 (IN2106.aspx)，合併到 stock_prices.json 的 by_name。

策略：
1. GET IN2106 取 ASP.NET hidden fields
2. POST 帶今天日期，retry 往前推一天直到拿到資料 (週末/假日無資料)
3. 解析 table，輸出每支基金的 NAV
4. 合併進 deploy/data/stock_prices.json 的 by_name
"""
import json
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

import os

ROOT = Path(__file__).resolve().parent.parent
STOCK_PRICES_PATHS = [
    ROOT / "data" / "stock_prices.json",
    ROOT / "deploy" / "data" / "stock_prices.json",
]
if os.environ.get("GITHUB_ACTIONS"):
    STOCK_PRICES_PATHS = [ROOT / "data" / "stock_prices.json"]

URL = "https://www.sitca.org.tw/ROC/Industry/IN2106.aspx?pid=IN2213_02"


def safe_float(s):
    if s is None:
        return None
    s = str(s).replace(",", "").strip()
    if not s or s in ("-", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def get_aspnet_hidden(session, url):
    """GET 一次取得 __VIEWSTATE 等隱藏欄位"""
    r = session.get(url, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    fields = {}
    for f in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"):
        tag = soup.find("input", {"name": f})
        if tag:
            fields[f] = tag.get("value", "")
    return fields


def fetch_for_date(session, date_str, hidden):
    """POST 取某日全公司基金淨值，回傳 (list[dict] or None, raw_html)"""
    data = dict(hidden)
    data.update({
        "ctl00$ContentPlaceHolder1$txtQ_Date": date_str,
        "ctl00$ContentPlaceHolder1$ddlQ_Comid": "",
        "ctl00$ContentPlaceHolder1$BtnQuery": "查詢",
    })
    r = session.post(URL, data=data, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    # 資料表的 class 為 DTeven / DTodd
    rows = soup.select("tr.DTeven, tr.DTodd")
    if not rows:
        return None
    out = []
    for tr in rows:
        tds = tr.find_all("td")
        if len(tds) < 8:
            continue
        # 欄位: 0 類型代號, 1 公司代號, 2 公司名稱, 3 受益憑證代號, 4 基金統編,
        #       5 基金名稱, 6 幣別, 7 淨值, 8 前一日淨值, 9 漲跌
        company = tds[2].get_text(strip=True)
        cert_code = tds[3].get_text(strip=True)
        fund_name = tds[5].get_text(strip=True)
        currency = tds[6].get_text(strip=True)
        nav = safe_float(tds[7].get_text(strip=True))
        if not fund_name or nav is None or nav <= 0:
            continue
        out.append({
            "fund_name": fund_name,
            "company": company,
            "code": cert_code,
            "currency": currency,
            "nav": nav,
            "date": date_str,
        })
    return out


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    print(f"Fetching SITCA 境內基金淨值 from {URL}")

    session = requests.Session()
    hidden = get_aspnet_hidden(session, URL)
    print(f"  got hidden fields: {list(hidden.keys())}")

    # 從今天往前 retry 最多 7 天
    today = date.today()
    funds = None
    used_date = None
    for offset in range(7):
        d = today - timedelta(days=offset)
        date_str = d.strftime("%Y%m%d")
        print(f"  POST date={date_str} ...", end=" ", flush=True)
        # 每次重新取 hidden（postback 後 ViewState 變化）
        if offset > 0:
            hidden = get_aspnet_hidden(session, URL)
            time.sleep(0.5)
        try:
            funds = fetch_for_date(session, date_str, hidden)
            if funds:
                used_date = date_str
                print(f"got {len(funds)} funds")
                break
            else:
                print("(no data)")
        except Exception as e:
            print(f"FAIL {e}")
    if not funds:
        print("[!] 7 天內都拿不到淨值，放棄")
        sys.exit(1)

    print(f"\n抓到 {len(funds)} 支境內基金，日期={used_date}")

    # 合併進 stock_prices.json (以 by_name 為主)
    for sp_path in STOCK_PRICES_PATHS:
        if not sp_path.exists():
            print(f"  [skip] {sp_path} 不存在")
            continue
        data = json.loads(sp_path.read_text(encoding="utf-8"))
        by_name = data.setdefault("by_name", {})
        by_code = data.setdefault("by_code", {})
        added = 0
        for f in funds:
            # 不覆蓋已存在的（股票優先）
            if f["fund_name"] not in by_name:
                by_name[f["fund_name"]] = {
                    "code": f["code"],
                    "close": f["nav"],
                    "market": "SITCA",
                    "date": f["date"],
                }
                added += 1
            if f["code"] and f["code"] not in by_code:
                by_code[f["code"]] = {
                    "name": f["fund_name"],
                    "close": f["nav"],
                    "market": "SITCA",
                    "date": f["date"],
                }
        data.setdefault("counts", {})["SITCA"] = len(funds)
        sp_path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(f"  merged into {sp_path}: +{added} new entries ({sp_path.stat().st_size/1024:.1f} KB)")


if __name__ == "__main__":
    main()
