"""下載內政部實價登錄 (LVR) 資料 — 智能補缺失的季度。

實價登錄每季公告 (1/3/5/7/9/11 月)，每季資料含全國 22 縣市。
本 script:
  1. 算出目前該有哪些季度 (民國年 + S1/S2/S3/S4)
  2. 檢查 data/lvr/{季度}/ 已存在則 skip
  3. 缺的季度從 plvr.land.moi.gov.tw 下載 + 解壓

只保留最近 N 季 (預設 8 季 = 2 年) 節省空間。
"""
import io
import os
import re
import shutil
import sys
import zipfile
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
LVR_DIR = ROOT / "data" / "lvr"
LVR_DIR.mkdir(parents=True, exist_ok=True)

KEEP_QUARTERS = 8  # 保留最近 8 季 (2 年)


def current_quarter_label() -> str:
    """傳回現在 LVR 可下載的最新季度 label (e.g. '114S1')"""
    today = date.today()
    # 民國年
    roc_y = today.year - 1911
    m = today.month
    # 各季公告月份:
    #   Q1 (1-3 月) 公告於 Q2 末 (約 5/15)
    #   Q2 (4-6) → 8/15
    #   Q3 (7-9) → 11/15
    #   Q4 (10-12) → 隔年 2/15
    # 簡化：今天 m → 算「已可下載的最新季」
    # 1, 2 月 → 上一年 Q3 (公告 11 月)
    # 3, 4, 5 月 → 上一年 Q4 (公告 2 月)
    # 6, 7, 8 月 → 該年 Q1 (公告 5 月)
    # 9, 10, 11 月 → 該年 Q2 (公告 8 月)
    # 12 月 → 該年 Q3 (公告 11 月)
    if m in (1, 2):
        return f"{roc_y - 1}S3"
    elif m in (3, 4, 5):
        return f"{roc_y - 1}S4"
    elif m in (6, 7, 8):
        return f"{roc_y}S1"
    elif m in (9, 10, 11):
        return f"{roc_y}S2"
    else:  # 12
        return f"{roc_y}S3"


def prev_quarter(q: str) -> str:
    """114S2 → 114S1; 114S1 → 113S4"""
    m = re.match(r"(\d+)S(\d)", q)
    if not m:
        raise ValueError(q)
    y, s = int(m.group(1)), int(m.group(2))
    if s == 1:
        return f"{y - 1}S4"
    return f"{y}S{s - 1}"


def needed_quarters(keep: int = KEEP_QUARTERS) -> list[str]:
    """列出最近 keep 季 (新 → 舊)"""
    out = [current_quarter_label()]
    for _ in range(keep - 1):
        out.append(prev_quarter(out[-1]))
    return out


def download_quarter(q: str) -> bool:
    """下載並解壓單一季度。回傳是否成功。"""
    out_dir = LVR_DIR / q
    if out_dir.exists() and any(out_dir.iterdir()):
        return True  # already have it
    url = f"https://plvr.land.moi.gov.tw/DownloadSeason?season={q}&type=ZIP&fileName=lvr_landcsv.zip"
    print(f"  download {q} ...", flush=True)
    try:
        r = requests.get(url, timeout=180)
        if r.status_code != 200 or len(r.content) < 1000:
            print(f"    [!] HTTP {r.status_code}, size={len(r.content)}")
            return False
        out_dir.mkdir(parents=True, exist_ok=True)
        z = zipfile.ZipFile(io.BytesIO(r.content))
        z.extractall(out_dir)
        print(f"    OK: {len(z.namelist())} files, {len(r.content)/1024:.0f} KB", flush=True)
        return True
    except Exception as e:
        print(f"    [!] {e}")
        if out_dir.exists():
            shutil.rmtree(out_dir, ignore_errors=True)
        return False


def cleanup_old_quarters(keep: list[str]):
    """刪除不在 keep 列表的舊資料"""
    keep_set = set(keep)
    for d in LVR_DIR.iterdir():
        if d.is_dir() and d.name not in keep_set:
            print(f"  cleanup old: {d.name}")
            shutil.rmtree(d, ignore_errors=True)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    quarters = needed_quarters()
    print(f"Target quarters: {quarters}")

    ok_count = 0
    for q in quarters:
        if download_quarter(q):
            ok_count += 1

    cleanup_old_quarters(quarters)

    print(f"\nDone: {ok_count}/{len(quarters)} quarters ready in {LVR_DIR}")


if __name__ == "__main__":
    main()
