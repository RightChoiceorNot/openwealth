"""把實價登錄資料建成「縣市+鄉鎮+段名 → 交易單價中位數」的查詢索引。

來源: data/lvr/{季度}/{a..z}_lvr_land_a.csv + a_lvr_land_a_land.csv
輸出: data/lvr_index.json

  {
    "臺北市|中山區|長安段三小段": {
      "land": {"median": 850000, "count": 12, "min": 600000, "max": 1200000},
      "build": {"median": 950000, "count": 8, ...}
    },
    ...
  }
單位：元/平方公尺
"""
import csv
import json
import sys
import statistics
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
LVR_DIR = ROOT / "data" / "lvr"
OUT_PATH = ROOT / "data" / "lvr_index.json"


def load_city_map(quarter_dir: Path) -> dict[str, str]:
    """從 manifest.csv 取 字首 → 縣市名"""
    m = {}
    fp = quarter_dir / "manifest.csv"
    if not fp.exists():
        return m
    for r in csv.DictReader(open(fp, encoding="utf-8-sig")):
        n = r["name"]
        if len(n) == 16 and n.endswith("_lvr_land_a.csv"):
            m[n[0]] = r["description"].replace("不動產買賣", "").strip()
    return m


def extract_section(土地位置: str) -> str:
    """從『奇岩段三小段』『南屯段』『北屯段二小段』取出『段+小段』作為 key
    剝掉地號 (如『 0210-0003』『 12345 地號』等尾巴)
    """
    s = (土地位置 or "").strip()
    if not s:
        return ""
    # 去尾數字/地號標示
    import re
    s = re.sub(r"\s*[\d-]+\s*地號?$", "", s)
    s = re.sub(r"\s+", "", s)
    return s


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    quarters = sorted([d for d in LVR_DIR.iterdir() if d.is_dir()])
    print(f"Quarters: {[q.name for q in quarters]}")

    # 全域索引: (city, township, section) → bucket，每種源頭分開 (單位皆為 元/㎡)
    #   "land_only":      純土地交易：單價 = 總價/土地        → 純土地物件用
    #   "house":          公寓/大樓 房地：單價 = 總價/建物    → 區分所有建物 (公寓/大樓)用
    #                     (排除透天厝/別墅/商辦)
    #   "townhouse_land": 透天/別墅 房地：單價 = 總價/土地面積 → 透天地坪估值用
    #                     (Gemini 新規則：透天本質是「買地」，建物殘值極低)
    #   "build_only":     純建物 (少)
    idx = defaultdict(lambda: {"land_only": [], "house": [], "townhouse_land": [], "build_only": []})

    for q in quarters:
        cmap = load_city_map(q)
        print(f"\n=== {q.name} ===")
        for prefix, city in cmap.items():
            # 主檔: 拿 編號 → 單價元/平方公尺 + 鄉鎮市區 + 交易標的
            main_fp = q / f"{prefix}_lvr_land_a.csv"
            land_fp = q / f"{prefix}_lvr_land_a_land.csv"
            if not main_fp.exists() or not land_fp.exists():
                continue
            main_by_id = {}
            for r in csv.DictReader(open(main_fp, encoding="utf-8-sig")):
                sn = r.get("編號") or ""
                if not sn or sn == "The serial number":
                    continue
                try:
                    unit_price = float(r.get("單價元平方公尺", "") or 0)
                except ValueError:
                    continue
                if unit_price <= 0:
                    continue
                def _f(k):
                    try: return float(r.get(k, "") or 0)
                    except: return 0.0
                main_by_id[sn] = {
                    "township": r.get("鄉鎮市區", "").strip(),
                    "target": r.get("交易標的", "").strip(),     # 房地(土地+建物)/土地/建物/...
                    "btype": r.get("建物型態", "").strip(),       # 透天厝/公寓/大樓/華廈/店面/廠辦/...
                    "unit_price": unit_price,
                    "total_price": _f("總價元"),
                    "land_area": _f("土地移轉總面積平方公尺"),
                    "bld_area": _f("建物移轉總面積平方公尺"),
                }

            # 子檔: 拿 編號 → 段名
            for r in csv.DictReader(open(land_fp, encoding="utf-8-sig")):
                sn = r.get("編號") or ""
                if not sn or sn == "The serial number":
                    continue
                main = main_by_id.get(sn)
                if not main:
                    continue
                section = extract_section(r.get("土地位置", ""))
                if not section:
                    continue
                key = (city, main["township"], section)
                target = main["target"]
                btype = main.get("btype") or ""
                # 商用 (店面/廠辦/工廠/倉庫) 單價遠高於住宅 → 排除（避免污染住宅估值）
                if btype and any(x in btype for x in ("店面", "店鋪", "廠辦", "工廠", "倉庫")):
                    continue
                if "房地" in target:
                    if btype and any(x in btype for x in ("透天厝", "別墅")):
                        # 透天：單價 = 總價 / 土地面積 (地坪單價) 灌 townhouse_land bucket
                        # 邏輯：透天本質是「買地」，建物殘值低；估值以土地坪數為主
                        if main["total_price"] > 0 and main["land_area"] > 0:
                            idx[key]["townhouse_land"].append(main["total_price"] / main["land_area"])
                    else:
                        # 公寓/大樓 房地：unit_price (= 總價/建物㎡) 灌 house bucket
                        idx[key]["house"].append(main["unit_price"])
                elif target == "土地":
                    idx[key]["land_only"].append(main["unit_price"])
                elif target == "建物":
                    idx[key]["build_only"].append(main["unit_price"])

    # 計算統計
    print(f"\n=== Computing stats for {len(idx):,} (city,township,section) groups ===")
    out = {}
    for (city, township, section), kinds in idx.items():
        rec = {}
        for kind, prices in kinds.items():
            if len(prices) >= 1:
                rec[kind] = {
                    "median": int(statistics.median(prices)),
                    "count": len(prices),
                    "min": int(min(prices)),
                    "max": int(max(prices)),
                }
        if rec:
            out[f"{city}|{township}|{section}"] = rec

    OUT_PATH.write_text(
        json.dumps(out, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"\n→ {OUT_PATH} ({OUT_PATH.stat().st_size/1024:.1f} KB, {len(out):,} keys)")


if __name__ == "__main__":
    main()
