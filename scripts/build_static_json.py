#!/usr/bin/env python3
"""把 declarations.db 整個 dump 成靜態 JSON，給純前端使用。

產出：
  deploy/data/politicians.json   - 全部 2,932 人摘要清單（給首頁 + 搜尋）
  deploy/data/per/<name>.json    - 每人 1 個 JSON，含所有申報詳情

前端不再需要 Flask，純靜態 CDN/GH Pages 即可。
"""
import json
import os
import re
import sqlite3
import urllib.parse
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "declarations.db"
# 前台不顯示的 source — DB 保留但 build 時跳過 (例如 g0v 多為已卸任公職)
HIDE_SOURCES = {"g0v"}
# 寫到兩個位置：deploy/data/（部署用） + data/（外層 dev server 用）
OUT_DIRS = [ROOT / "deploy" / "data", ROOT / "data"]
OUT_DIR = OUT_DIRS[0]   # 主要寫入位置
PER_DIR = OUT_DIR / "per"
STOCK_PRICES_PATH = ROOT / "data" / "stock_prices.json"
# In GitHub Actions the repo root IS the data root (no deploy/ subdir)
if os.environ.get("GITHUB_ACTIONS"):
    OUT_DIRS = [ROOT / "data"]
    OUT_DIR = ROOT / "data"
    PER_DIR = ROOT / "data" / "per"
    STOCK_PRICES_PATH = ROOT / "data" / "stock_prices.json"


def clean_name(s: str) -> str:
    """清理 PDF 解析出的雜散空白：CJK 字元之間 / 括號內外的多餘空白移除
    並去除 PDF 補正/異動表格的列號標記 (1★, 2★ 等)。"""
    if not s:
        return ''
    n = re.sub(r'\s+', ' ', str(s))
    # CJK 字之間反覆收斂去空白（中文「摩 根 士 丹 利」→「摩根士丹利」）
    prev = None
    while prev != n:
        prev = n
        n = re.sub(r'(?<=[一-鿿])\s+(?=[一-鿿])', '', n)
    # 括號內側 leading/trailing 空白移除
    n = re.sub(r'([（(])\s+', r'\1', n)
    n = re.sub(r'\s+([）)])', r'\1', n)
    # CJK 與括號之間的空白去掉
    n = re.sub(r'(?<=[一-鿿])\s+(?=[（(])', '', n)
    n = re.sub(r'(?<=[）)])\s+(?=[一-鿿])', '', n)
    # 補正/異動表格的列號標記：開頭的 ★/☆ 或 數字+★ (例: "2★台幣"→"台幣"、"★元大台灣50"→"元大台灣50")
    n = re.sub(r'^\s*\d*\s*[★☆]\s*', '', n)
    return n.strip()


def _norm_stock(s: str, aggressive: bool = False) -> str:
    """正規化股票/公司名供 fuzzy match (中英文皆適用)。

    aggressive=False (預設, 建 stock_prices.json 索引用)：
      保守版 — 不剝除會讓名稱過短 (<3 CJK) 的尾綴。
      例：「元大電子」→「元大電子」(不會被剝成「元大」造成污染)
    aggressive=True (處理申報名用)：
      積極版 — 不管剩多少都剝。
      例：「鴻海精密工業股份有限公司」→「鴻海」→ 可以 exact match 到清單裡的「鴻海」
    """
    if not s:
        return ''
    n = clean_name(s)
    n = re.sub(r'[（(][^）)]*[）)]', '', n)
    n = (n.replace('股份有限公司', '').replace('有限公司', '')
           .replace('商業銀行', '銀行').replace('金融控股', '金'))
    stripped = re.sub(r'(精密工業|科技|工業|實業|企業|金控|證券|投信|電子|電腦|電器|建設|開發|生技|生物科技|製藥|資訊|集團|興業|機械|食品|國際|光電)$', '', n)
    if aggressive or sum(1 for ch in stripped if '一' <= ch <= '鿿') >= 3:
        n = stripped
    n = re.sub(r'^[\d\s★☆▲▼※◎●○]+', '', n)
    n = re.sub(r'[,\.]', '', n)
    n = re.sub(r'\b(INC|CORP|CORPORATION|CO|LTD|LIMITED|COMPANY|PLC|HOLDINGS?|GROUP|THE)\b', '', n, flags=re.IGNORECASE)
    n = n.replace('&', 'AND')
    return n.replace(' ', '').strip().upper()


# 公司全名 → 上市簡稱 對照表
# 主要來源：data/company_aliases.json (由 fetch_company_aliases.py 從 TWSE/TPEx OpenAPI 抓 2300+ 筆)
# 以下手寫部分作為 fallback：補非上市公司或申報名變體 (例：把 "中國信託金融控股" → "中信金"
# 在官方表是 "中國信託金融控股股份有限公司" → "中信金"，去掉股份字會 match 不到)
COMPANY_ALIASES_PATH = ROOT / "data" / "company_aliases.json"
COMPANY_ALIASES_USER_PATH = ROOT / "data" / "company_aliases_user.json"


def _load_company_aliases() -> dict[str, str]:
    """載入 官方 + 手寫 + 使用者填表 的 公司全名 → 簡稱 對應表

    優先序: 使用者填表 > 手寫 > 官方
    使用者填表可標 None (已下市/找不到) → matcher 回傳 None → 前端顯示「估」
    """
    aliases = dict(COMPANY_ALIASES_MANUAL)
    # 官方 (覆蓋手寫)
    if COMPANY_ALIASES_PATH.exists():
        try:
            raw = json.loads(COMPANY_ALIASES_PATH.read_text(encoding='utf-8'))
            official = raw.get('aliases', {})
            aliases.update(official)
            for full, short in list(official.items()):
                stripped = full.replace('股份有限公司', '').replace('有限公司', '').strip()
                if stripped and stripped != full and stripped not in aliases:
                    aliases[stripped] = short
            print(f"  [aliases] loaded {len(official):,} official + {len(COMPANY_ALIASES_MANUAL)} manual")
        except Exception as e:
            print(f"  [aliases] failed to load {COMPANY_ALIASES_PATH}: {e}")
    # 使用者填表 (最高優先 - 覆蓋官方/手寫)
    if COMPANY_ALIASES_USER_PATH.exists():
        try:
            user = json.loads(COMPANY_ALIASES_USER_PATH.read_text(encoding='utf-8'))
            user_filled = sum(1 for v in user.values() if v)
            user_none = sum(1 for v in user.values() if v is None)
            aliases.update(user)
            print(f"  [aliases] + {len(user)} user-filled ({user_filled} → 簡稱, {user_none} → None)")
        except Exception as e:
            print(f"  [aliases] failed to load {COMPANY_ALIASES_USER_PATH}: {e}")
    print(f"  [aliases] total: {len(aliases):,}")
    return aliases


# 手寫補充（針對申報書中常見、官方表沒有的變體）
COMPANY_ALIASES_MANUAL = {
    # 半導體 / 電子
    '台灣積體電路製造': '台積電',
    '聯華電子': '聯電',
    '友達光電': '友達',
    '群創光電': '群創',
    '日月光半導體製造': '日月光投控',
    '日月光半導體': '日月光投控',
    '奇美電子': '奇美電',
    '仁寶電腦工業': '仁寶',
    '宏碁': '宏碁',
    '宏碁電腦': '宏碁',
    '華碩電腦': '華碩',
    '微星科技': '微星',
    '研華科技': '研華',
    '聯發科技': '聯發科',
    '台達電子工業': '台達電',
    '光寶科技': '光寶科',
    '緯創資通': '緯創',
    '英業達': '英業達',
    '大立光電': '大立光',
    # 金融
    '中國信託金融控股': '中信金',
    '中國信託商業銀行': '中信銀',
    '中華開發金融控股': '凱基金',
    '上海商業儲蓄銀行': '上海商銀',
    '台北富邦商業銀行': '北富銀',
    '京城商業銀行': '京城銀',
    '彰化商業銀行': '彰銀',
    '玉山商業銀行': '玉山銀',
    '元大商業銀行': '元大銀',
    '台中商業銀行': '台中銀',
    '聯邦商業銀行': '聯邦銀',
    '遠東國際商業銀行': '遠東銀',
    '新光商業銀行': '新光銀',
    '凱基商業銀行': '凱基銀',
    '中國輸出入銀行': '輸出入銀',
    '合作金庫金融控股': '合庫金',
    '永豐金融控股': '永豐金',
    '台新金融控股': '台新新光金',
    '台新新光金融控股': '台新新光金',
    '玉山金融控股': '玉山金',
    '兆豐金融控股': '兆豐金',
    '第一金融控股': '第一金',
    '富邦金融控股': '富邦金',
    '國泰金融控股': '國泰金',
    '元大金融控股': '元大金',
    # 鋼鐵 / 塑膠 / 水泥
    '中國鋼鐵': '中鋼',
    '中鴻鋼鐵': '中鴻',
    '台灣塑膠工業': '台塑',
    '南亞塑膠工業': '南亞',
    '台灣化學纖維': '台化',
    '台灣水泥': '台泥',
    '亞洲水泥': '亞泥',
    # 電信
    '中華電信': '中華電',
    '台灣大哥大': '台灣大',
    '遠傳電信': '遠傳',
    '亞太電信': '亞太電',
    # 航運 / 觀光
    '中華航空': '華航',
    '長榮海運': '長榮',
    '長榮航空': '長榮航',
    '陽明海運': '陽明',
    '萬海航運': '萬海',
    '裕民航運': '裕民',
    '台灣高速鐵路': '台灣高鐵',
    # 食品
    '統一企業': '統一',
    '味全食品工業': '味全',
    '味王': '味王',
    # 電線電纜 / 其他
    '太平洋電線電纜': '太電',
    '大同': '大同',
    '台灣電力': None,  # 台電 未上市
    '中華郵政': None,  # 未上市
    # KY/海外掛牌
    '譜瑞': '譜瑞-KY',
    '中租迪和': '中租-KY',
    # 其他常見
    '聯強國際': '聯強',
    '台塑石化': '台塑化',
    '南亞電路板': '南電',
    '長華電材': '長華*',
    '長華科': '長華*',          # 用戶填法的別名 (官方為 "長華電材股份有限公司" → "長華*")
    '台灣港務': None,           # 國營未上市
    '興航': None,               # 復興航空 (已下市)
    '復興航空': None,
    # 政治人物常見填法 ↔ 官方簡稱對照 (從未配對清單中挑出明確可對的)
    '開發金': '凱基金',          # 開發金 2883 已改名凱基金
    '京城銀': '京城銀行',
    '陽信商銀': '陽信銀',
    '日月光': '日月光投控',
    '台灣積電': '台積電',
    '臺灣積電': '台積電',
    '臺化': '台化',
    '亞太電': '亞太電信',         # 3682
    '新光銀行': '新光銀',
    '亞太電信': '亞太電信',
    '中租-KY': '中租-KY',
    '中租－KY': '中租-KY',
    '中租迪和-KY': '中租-KY',
    '康友-KY': '康友-KY',
    '康友－KY': '康友-KY',
    '永冠-KY': '永冠-KY',
    '永冠－KY': '永冠-KY',
    '泰福-KY': '泰福-KY',
    '泰福－KY': '泰福-KY',
    '臻鼎-KY': '臻鼎-KY',
    '臻鼎－KY': '臻鼎-KY',
    '亞德客-KY': '亞德客-KY',
    '亞德客－KY': '亞德客-KY',
    '高鐵': '台灣高鐵',
    '中橡': '中橡',              # 2104
    '台開': '台開',              # 2841
    '南科': '南亞科',            # 2408
    '矽品': '矽品',              # 已下市 (被日月光合併)
    '矽品精密': '矽品',
    '矽品精密工業': '矽品',
    '光寶': '光寶科',
    '光磊': '光磊',
    '台一': '台一',
    '合庫': '合庫金',
    '中保': '中保科',
    '中華銀': None,              # 中華商銀已下市
    '中華商銀': None,
    '中興商銀': None,            # 已下市
    '寶華': None,                # 寶華銀已下市
    '寶華銀': None,
    '寶華銀行': None,
    '中國力霸': None,            # 已下市
    '力霸': None,
    '太電': None,                # 太平洋電線電纜已下市
    '華映': None,                # 已下市
    '華隆': None,                # 已下市
    '誠洲': None,                # 已下市
    '茂德科技': None,            # 已下市
    '茂德': None,
    '勝華': None,                # 已下市
    '勝華科技': None,
    '東雲': None,                # 已下市
    '碧悠電子': None,            # 已下市
    '耀文電子': None,            # 已下市
    '長億實業': None,            # 已下市
    '宏總': None,                # 已下市
    '臺鳳': None,                # 台鳳已下市
    '台鳳': None,
    '歌林': None,                # 已下市
    '英群': None,                # 已下市
    '紐新': None,                # 已下市
    '台中二信': None,            # 信用合作社未上市
    '基隆一信': None,
    '新竹一信': None,
    '彰化第十信用合作社': None,
    '中央銀行': None,
    '中華郵政': None,
    '台灣電力公司': None,
    '臺灣電力公司': None,
    '台灣糖業': None,
    '中華電信公司': '中華電',
}


def load_stock_matcher():
    """讀 stock_prices.json，回傳 match_fn(decl_name, sec_type=None) -> (close, code, market) | None。

    全部 sec_type 都用同一個表 (TWSE/TPEx/SITCA/US)。
    早期版本對 sec_type='基金' 限制只配 SITCA，但 ETF 如「元大高股息」「元大台灣50」「群益台ESG低碳50」
    在 TWSE 是短名稱、在 SITCA 是冗長正式名稱，限制 SITCA 反而抓不到。
    現在靠較嚴的 _norm_stock (≥3 CJK 才剝後綴) + 較嚴的 lookup (短 key 禁用 fuzzy) 防誤匹配。"""
    if not STOCK_PRICES_PATH.exists():
        print(f"[!] {STOCK_PRICES_PATH} 不存在 — 跳過股價 enrichment")
        return lambda _decl, _sec=None: None

    raw = json.loads(STOCK_PRICES_PATH.read_text(encoding='utf-8'))
    by_name = raw['by_name']
    # norm_table 按長度 desc：clause 2 (申報 startswith 清單) 要拿最長前綴
    # 另存一份 ASC：clause 3 (清單 startswith 申報) 要拿最短包含 (最接近 exact 的)
    norm_table_desc = sorted(
        ((_norm_stock(name), info) for name, info in by_name.items() if _norm_stock(name)),
        key=lambda kv: -len(kv[0])
    )
    norm_table_asc = sorted(norm_table_desc, key=lambda kv: len(kv[0]))
    norm_dict = {k: v for k, v in norm_table_desc}

    # 預先計算 alias map 的 norm key (一次)。資料來源：官方 OpenAPI + 手寫補充 + 使用者填表
    all_aliases = _load_company_aliases()
    valid_short_norms = {k for k, _ in norm_table_desc}
    alias_norm_to_target = {}
    # blocked_norms: 使用者/手寫標 None (已下市/未上市) 的名稱 → 不對 (回傳 None，前端顯示「估」)
    blocked_norms = set()
    # prefix_index: 申報名只寫部分公司全名時 (例: "聯嘉光電") 對到唯一公司全名 ("聯嘉光電投資控股")
    from collections import defaultdict as _dd
    prefix_index = _dd(list)
    for full, short in all_aliases.items():
        full_norm = _norm_stock(full, aggressive=False)
        if not full_norm:
            continue
        if short is None:
            blocked_norms.add(full_norm)
            # 也加 aggressive norm (申報全名 → 短化後同樣 block)
            blocked_norms.add(_norm_stock(full, aggressive=True))
            continue
        short_norm = _norm_stock(short, aggressive=False)
        if not short_norm or short_norm not in valid_short_norms:
            continue
        alias_norm_to_target[full_norm] = short_norm
        # 為每段長度 ≥4 的前綴建索引 (上限 len-1 避免重複 exact)
        for L in range(4, len(full_norm)):
            prefix_index[full_norm[:L]].append(short_norm)
    # 只保留 unique-prefix → short_norm (有歧義的 prefix 丟掉)
    prefix_index = {p: list(set(targets))[0] for p, targets in prefix_index.items() if len(set(targets)) == 1}

    def lookup(decl_name: str, sec_type: str | None = None):
        # 試保守 norm 先 (避免把申報「鴻海精密工業」直接吃成「鴻海」之外的東西)
        nm = _norm_stock(decl_name, aggressive=False)
        # 也算積極版 — 處理「鴻海精密工業股份有限公司」這種長申報名
        nm_loose = _norm_stock(decl_name, aggressive=True)
        if not nm or len(nm) < 2:
            return None
        # -1. Blocked (使用者標 None / 已下市) → 直接放棄
        if nm in blocked_norms or nm_loose in blocked_norms:
            return None
        # 0. Alias map: 申報全名 → 上市簡稱
        if nm in alias_norm_to_target:
            return norm_dict.get(alias_norm_to_target[nm])
        if nm_loose != nm and nm_loose in alias_norm_to_target:
            return norm_dict.get(alias_norm_to_target[nm_loose])
        # 1a. 保守版 exact
        if nm in norm_dict:
            return norm_dict[nm]
        # 1b. 積極版 exact (鴻海精密工業 → 鴻海)
        if nm_loose and nm_loose != nm and nm_loose in norm_dict and len(nm_loose) >= 2:
            return norm_dict[nm_loose]
        # 1.5 申報名是某官方公司全名的 unique prefix (例: "聯嘉光電" → "聯嘉光電投資控股")
        if len(nm) >= 4 and nm in prefix_index:
            short_norm = prefix_index[nm]
            if short_norm in norm_dict:
                return norm_dict[short_norm]
        cjk_len = sum(1 for ch in nm if '一' <= ch <= '鿿')
        # 短 key (<3 CJK) 不允許 fuzzy / 前綴搶占
        if cjk_len > 0 and cjk_len < 3:
            return None
        # 2. 申報 startswith 清單 (處理「鴻海精密工業」→「鴻海」)；清單 key 必須 ≥3 字
        for stock_nm, info in norm_table_desc:
            if len(stock_nm) >= 3 and nm.startswith(stock_nm):
                return info
        # 3. 清單 startswith 申報 (處理「安聯台灣大壩基金」→「安聯台灣大壩基金-A類型-新臺幣」)
        #    申報 ≥6 字；按 ASC 排序，挑「最接近」申報長度的那筆
        if len(nm) >= 6:
            for stock_nm, info in norm_table_asc:
                if len(stock_nm) >= len(nm) and stock_nm.startswith(nm):
                    return info
        return None

    return lookup


def safe_filename(name: str) -> str:
    """Use Chinese name directly as filename. Browser encodes URL, server decodes
    back to Chinese to find file. Windows allows Chinese chars in filenames.

    For names with FS-illegal chars (/ \\ : * ? " < > |) → replace with _.
    """
    illegal = '/\\:*?"<>|'
    safe = "".join("_" if c in illegal else c for c in name)
    return safe + ".json"


def normalize_date(s):
    """民國年 / ISO 都轉成 ISO（用於可比較字串）。"""
    if not s:
        return ''
    s = str(s).strip()
    # ISO 格式
    m = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})', s)
    if m:
        return f'{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}'
    # 民國格式：114年12月27日 / 民國114年 12月 27日 / 114.12.27
    s2 = re.sub(r'^民國\s*', '', s)
    m = re.match(r'^(\d{1,3})[\s年./-]+(\d{1,2})[\s月./-]+(\d{1,2})', s2)
    if m:
        y = int(m.group(1))
        if y < 200:
            y += 1911
        return f'{y:04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}'
    return s

# 5 類機關分類（中央/地方/公營事業/公立學校/其他）
SIX_CITIES = ('臺北市', '新北市', '桃園市', '臺中市', '臺南市', '高雄市',
              '台北市', '台中市', '台南市')
PROVINCIAL_CITIES = ('基隆市', '新竹市', '嘉義市')
COUNTIES = ('新竹縣', '苗栗縣', '彰化縣', '南投縣', '雲林縣', '嘉義縣',
            '屏東縣', '宜蘭縣', '花蓮縣', '臺東縣', '台東縣',
            '澎湖縣', '金門縣', '連江縣',
            # 已升格直轄市前的舊縣名（舊申報資料仍會出現）
            '臺北縣', '台北縣', '桃園縣', '臺中縣', '台中縣',
            '臺南縣', '台南縣', '高雄縣')


def categorize_org(name: str) -> str:
    """回傳 '中央機關' / '地方機關' / '公營事業' / '其他'。

    註：本資料庫不含學校（校長級不在公職人員財產申報名單）。
    """
    if not name:
        return '其他'

    # 1) 非政府組織（先擋）
    if re.search(r'(農會|漁會|協會|公會|商會|工會|職業工會|基金會|財團法人|社團法人|合作社)', name):
        return '其他'

    is_local_prefix = name.startswith(SIX_CITIES + PROVINCIAL_CITIES + COUNTIES)

    # 2) 地方優先（縣市開頭一律地方，含地方公營如台北捷運）
    if is_local_prefix:
        return '地方機關'
    if re.search(r'(鄉公所|鎮公所|區公所|市公所|代表會)$', name):
        return '地方機關'
    # 地方政府轉投資的公營企業：以縣市「短名」開頭（去掉市/縣字）+ 公司/捷運/自來水等
    if re.match(r'^(臺北|台北|新北|桃園|臺中|台中|臺南|台南|高雄|基隆|新竹|嘉義)(捷運|自來水|大眾運輸|大眾捷運|港務|翡翠水庫)', name):
        return '地方機關'

    # 3) 中央：明確開頭
    if re.match(r'^(總統府|副總統府|國家安全會議|國家安全局|國史館)', name):
        return '中央機關'
    if re.match(r'^(行政院|立法院|司法院|考試院|監察院)', name):
        return '中央機關'
    # 「中央」「國立」「國家」開頭一律中央
    if re.match(r'^(中央|國立|國家)', name):
        return '中央機關'
    # 法院 / 檢察署
    if re.search(r'(法院|檢察署|地檢署)$', name):
        return '中央機關'
    # 中央部會（XX 部 / XX 署，地方已先排除）
    if re.search(r'(部|署)$', name) and len(name) <= 6:
        return '中央機關'
    # 各種委員會（地方已先排除）
    if re.search(r'委員會$', name):
        return '中央機關'
    # 中央委員會下屬子機關（海洋委員會海洋保育署 / 客家委員會XX署 / 原民會XX處 等）
    if re.match(r'^(海洋委員會|客家委員會|原住民族委員會|僑務委員會|大陸委員會|國家通訊傳播委員會|公平交易委員會|金融監督管理委員會)', name):
        return '中央機關'
    # 中央部會簡稱含括
    if re.search(r'(衛福部|衛生福利部|內政部|外交部|國防部|財政部|教育部|法務部|經濟部|交通部|勞動部|文化部|農業部|環境部|數位發展部|考選部|審計部|主計總處|人事行政總處|警政署|消防署|移民署|關務署|農糧署|林業署|海巡署|疾管署|食藥署)', name):
        return '中央機關'

    # 4) 公營事業（地方已先排除，剩下的公司為中央國營）
    # 不限結尾匹配 — 涵蓋「臺灣中油股份有限公司大林煉油廠」這種帶分支的名字
    if re.search(r'(股份有限公司|有限公司)', name):
        return '公營事業'
    if re.search(r'公司$', name):
        return '公營事業'
    if re.match(r'^(台灣|臺灣|中華|台|中)(銀行|電信|郵政|鐵路|電力|自來水|糖業|菸酒|肥料|高鐵|港務|船務|航空)', name):
        return '公營事業'
    if re.search(r'銀行$', name):
        return '公營事業'

    return '其他'


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PER_DIR.mkdir(parents=True, exist_ok=True)

    # Clean old per files
    for f in PER_DIR.glob("*.json"):
        f.unlink()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    print("=== Loading all declarations ===")
    cur.execute("""
        SELECT d.id, d.issue_id, d.page_start, d.name, d.organization, d.title,
               d.decl_date, d.decl_type, d.is_change, d.spouse,
               d.total_deposits, d.total_cash, d.total_securities, d.total_debt,
               i.issue_number, i.source
        FROM declarations d
        JOIN issues i ON d.issue_id = i.id
        ORDER BY d.name, d.decl_date DESC
    """)
    all_decls = [dict(r) for r in cur.fetchall()]
    print(f"  Loaded {len(all_decls):,} declarations")

    # 前台隱藏指定 source 的申報（DB 仍保留）
    if HIDE_SOURCES:
        before = len(all_decls)
        all_decls = [d for d in all_decls if d.get("source") not in HIDE_SOURCES]
        print(f"  Filtered out HIDE_SOURCES={HIDE_SOURCES}: -{before - len(all_decls):,} declarations")

    # Load all detail tables by declaration_id
    print("=== Loading detail tables ===")
    detail_tables = ["deposits", "securities", "real_estate", "debts", "other_assets"]
    details_by_decl = {t: defaultdict(list) for t in detail_tables}
    for t in detail_tables:
        cur.execute(f"SELECT * FROM {t}")
        rows = cur.fetchall()
        for r in rows:
            d = dict(r)
            decl_id = d.pop("declaration_id")
            d.pop("id", None)
            # debts 表用 debtor 欄位（PDF 第 2 欄）但其他表用 owner — 統一改名給前端用
            if t == "debts" and "debtor" in d:
                d["owner"] = d.pop("debtor")
            details_by_decl[t][decl_id].append(d)
        print(f"  {t}: {len(rows):,}")

    conn.close()

    # ── 推測 owner 為「X○」「X○○」隱碼名 → spouse name（PDF 部分欄位隱碼）─
    print("\n=== Resolving masked owner names (X○ → spouse) ===")
    decl_by_id = {d["id"]: d for d in all_decls}
    resolved = 0
    for table_name, by_decl in details_by_decl.items():
        for decl_id, rows in by_decl.items():
            decl = decl_by_id.get(decl_id)
            spouse = (decl or {}).get("spouse") or ""
            politician = (decl or {}).get("name") or ""
            for r in rows:
                owner = r.get("owner")
                if not owner or "○" not in owner:
                    continue
                # X○ or X○○: 首字符當姓氏，比對 spouse / politician 首字
                first = owner[0]
                if spouse and first == spouse[0]:
                    r["owner"] = spouse
                    resolved += 1
                elif politician and first == politician[0]:
                    r["owner"] = politician
                    resolved += 1
    print(f"  resolved: {resolved:,}")

    # ── 清理 PDF 解析雜散空白：所有 string 欄位 (detail tables + declarations) ─
    print("\n=== Cleaning string fields (remove PDF parsing whitespace) ===")
    cleaned = 0
    # detail tables
    for table_name, by_decl in details_by_decl.items():
        for rows in by_decl.values():
            for r in rows:
                for k, v in list(r.items()):
                    if isinstance(v, str) and v:
                        new = clean_name(v)
                        if new != v:
                            r[k] = new
                            cleaned += 1
    # declarations 主表
    for d in all_decls:
        for k, v in list(d.items()):
            if isinstance(v, str) and v:
                new = clean_name(v)
                if new != v:
                    d[k] = new
                    cleaned += 1
    print(f"  cleaned: {cleaned:,} fields")

    # ── 過濾異族 owner：PDF 解析時相鄰申報書被合併成同一 declaration，
    # 會把別人的明細掛在當事人名下。判定：owner 含中文字、不含本人/配偶名 → 視為污染丟棄。
    # 保留：空 owner、含 ○ 隱碼、純英數代號（無中文字）、含本人或配偶名為子字串。
    print("\n=== Filtering foreign-owner items (cross-declaration contamination) ===")
    def _owner_belongs(owner: str, politician: str, spouse: str) -> bool:
        if not owner:
            return True
        o = owner.strip()
        if not o:
            return True
        # 隱碼名 (含 ○)：首字必須匹配本人或配偶；否則就是別人家屬被誤掛
        if "○" in o:
            first = o[0]
            if politician and first == politician[0]:
                return True
            if spouse and first == spouse[0]:
                return True
            return False
        # 沒有 CJK 中文字 (純英數/代號)：保留 (大多為錯置的代號欄位)
        if not any("一" <= ch <= "鿿" for ch in o):
            return True
        if politician and politician in o:
            return True
        if spouse and spouse in o:
            return True
        return False

    dropped = 0
    for table_name, by_decl in details_by_decl.items():
        for decl_id, rows in list(by_decl.items()):
            decl = decl_by_id.get(decl_id)
            if not decl:
                # 對應的 decl 已被 HIDE_SOURCES 濾掉 (例如 g0v) — 不需要再過濾，反正不會輸出
                continue
            pol = decl.get("name") or ""
            sp = decl.get("spouse") or ""
            kept = [r for r in rows if _owner_belongs(r.get("owner") or "", pol, sp)]
            d = len(rows) - len(kept)
            if d:
                dropped += d
                by_decl[decl_id] = kept
    print(f"  dropped: {dropped:,} foreign-owner items")

    # ── 將錯置 owner 的證券識別碼 (CUSIP/ISIN/Ticker) 歸位到 match_code ──
    # 範例：美國國庫債券 owner='US912810PX00' (CUSIP 應為識別碼, 非所有人)
    # 判定：securities 表 + owner 是純英數大寫且 >= 8 字 + 至少含 1 數字 → 視為錯置代號
    # 修正：把該值搬到 match_code，owner 清空 (前端會 fallback 顯示本人/隱藏)
    print("\n=== Re-routing misplaced security identifiers (CUSIP/ISIN in owner field) ===")
    _CODE_PAT = re.compile(r"^[A-Z][A-Z0-9]{7,}$")
    rerouted = 0
    for r in (row for rows in details_by_decl["securities"].values() for row in rows):
        own = (r.get("owner") or "").strip()
        if not own or not _CODE_PAT.match(own) or not any(c.isdigit() for c in own):
            continue
        if r.get("match_code"):
            r["match_code"] = r.get("match_code") or own
        else:
            r["match_code"] = own
        r["owner"] = ""
        rerouted += 1
    print(f"  rerouted: {rerouted:,} securities (owner → match_code)")

    # ── 股價 enrichment：為每筆 securities 補 current_value ─────────
    print("\n=== Enriching securities with stock prices ===")
    stock_match = load_stock_matcher()
    # 債券/公債類不該對 stock price (面額固定不像股票漲跌)
    SKIP_SEC_TYPES = {"債券", "公債", "其他公債", "其他證券"}
    enrich_count = 0
    total_sec = 0
    skipped_bond = 0
    # 偵測「PDF 在名稱後方括號內標註此筆為海外/ADR/國外股票」這類註記 (要跳過本地股票配對)
    # 注意必須是括號內的「國外/海外/ADR」，避免把「永豐美國500大」「日本基金」這類正常 ETF 名稱誤殺
    OVERSEAS_NOTE_RE = re.compile(r'[（(][^）)]*(?:國外|海外|ADR|未上市|未上櫃)[^）)]*[）)]')
    for sec_list in details_by_decl["securities"].values():
        for s in sec_list:
            total_sec += 1
            sec_type = s.get("sec_type") or ""
            if sec_type in SKIP_SEC_TYPES:
                skipped_bond += 1
                continue  # 不 enrich，前端會顯示原 twd_amount + 「估」
            name = s.get("name") or ""
            # 名稱有「(未交付信託原因：國外股票)」這類括號註記 → 不該配 TWSE/TPEx 本地股票
            if OVERSEAS_NOTE_RE.search(name):
                continue
            # 其他證券通常是海外/未上市 — 也不配本地股票表
            if sec_type == "其他證券":
                continue
            info = stock_match(name, sec_type)
            if not info:
                continue
            qty = s.get("quantity")
            try:
                qty_f = float(qty) if qty is not None else 0
            except (ValueError, TypeError):
                continue
            if qty_f <= 0:
                continue
            cv = qty_f * info["close"]
            s["current_value"] = cv
            s["match_code"] = info["code"]
            s["match_market"] = info["market"]
            s["match_close"] = info["close"]
            enrich_count += 1
    pct = enrich_count / total_sec * 100 if total_sec else 0
    print(f"  enriched: {enrich_count:,} / {total_sec:,} ({pct:.1f}%)  (skipped bond/公債: {skipped_bond:,})")

    # ── 不動產 enrichment：對「沒填取得價額」的用實價登錄同段近期交易中位數估價 ──
    print("\n=== Enriching real_estate with 實價登錄 (LVR) estimates ===")
    LVR_INDEX_PATH = ROOT / "data" / "lvr_index.json"
    if LVR_INDEX_PATH.exists():
        try:
            lvr_idx = json.loads(LVR_INDEX_PATH.read_text(encoding="utf-8"))
        except Exception:
            lvr_idx = {}
    else:
        lvr_idx = {}

    # 省轄市/直轄市底下不再分區 (基隆/新竹/嘉義市) — index 中 township 也用 city name
    _NO_DISTRICT_CITIES = {"基隆市", "新竹市", "嘉義市"}
    def _parse_re_loc(loc):
        s = (loc or "").strip()
        s = re.sub(r"[（(].*?[）)]", "", s).strip()
        # 標準格式: 縣市 + 區/鄉/鎮 + 段名
        m = re.match(r"^(.+?[市縣])(.+?[區鄉鎮市])(.+)$", s)
        if m:
            city, twp, sec = m.group(1), m.group(2), m.group(3).strip()
            # 修正「平鎮區」被誤切成 (平鎮, 區六和段) 的問題：
            # lazy match 遇到鄉鎮名中含有 [區鄉鎮市] 字元（如「平鎮」中的「鎮」）時
            # 會提早停止，導致 section 開頭多出一個行政字元 → 補回 township
            # 但只在 sec[0] 是該縣市正確行政後綴時才補：
            #   直轄市/省轄市 (市) → 轄區後綴為「區」；縣 → 鄉/鎮/市/區 均可
            # 避免把段名首字「市」（如「市府段」）誤判為後綴
            if sec and sec[0] in "區鄉鎮市":
                expected_sfx = "區" if city.endswith("市") else "區鄉鎮市"
                if sec[0] in expected_sfx:
                    twp = twp + sec[0]
                    sec = sec[1:].strip()
            # OCR 常在段名中插入多餘空格（如「灰 段」→「灰段」），去除後才能命中 LVR index
            sec = re.sub(r"\s+", "", sec)
            return city, twp, sec
        # 省轄市無區: 新竹市/基隆市/嘉義市 + 段名
        for c in _NO_DISTRICT_CITIES:
            if s.startswith(c):
                return c, c, s[len(c):].strip()
        return None

    # ── ratio 解析工具 ──────────────────────────────────────────
    def _ratio_pair(rs):
        """回傳 (numerator, denominator) tuple；'全部'/共有 → (1, 1)；無效 → None。"""
        rs = (rs or "").strip()
        if not rs or rs == "全部" or rs in ("公同共有", "共有"):
            return (1, 1)
        m = re.match(r"^(\d+)\s*分之\s*(\d+)$", rs)
        if m:
            return (int(m.group(2)), int(m.group(1)))
        return None  # 不完整字串 (e.g. "100000 分" 缺分子) → 視為未知

    def _ratio(rs):
        p = _ratio_pair(rs)
        if not p:
            return 0.0
        return p[0] / p[1]

    def _is_full_ratio(rs):
        p = _ratio_pair(rs)
        return p == (1, 1)

    # 預建 city|township -> {section} 索引，供段名模糊比對用
    _twp_secs: dict = {}
    for _k in lvr_idx:
        _parts = _k.split("|")
        if len(_parts) == 3:
            _twp_secs.setdefault(f"{_parts[0]}|{_parts[1]}", set()).add(_parts[2])

    def _lvr_lookup_one(city, township, section, bucket_name, min_count=1):
        """查單一 bucket。回傳 stats dict or None。
        精確比對失敗時嘗試前綴模糊比對（僅接受唯一候選），處理 PDF 字型缺字問題。
        例：灰段（缺「磘」）→ 灰磘段；學府（缺「段」）→ 學府段。
        """
        city_variants = (city, city.replace("臺", "台"), city.replace("台", "臺"))
        # 1. 精確比對
        for c in city_variants:
            rec = lvr_idx.get(f"{c}|{township}|{section}")
            if rec and rec.get(bucket_name) and rec[bucket_name]["count"] >= min_count:
                return rec[bucket_name]
        # 2. 前綴模糊比對：section 去掉尾字「段」後作為前綴
        #    候選條件：候選段名以此前綴開頭，且長度僅多 1–2 字（= PDF 漏字數量）
        prefix = section.rstrip("段")
        candidates: set = set()
        for c in city_variants:
            for cs in _twp_secs.get(f"{c}|{township}", set()):
                if cs.startswith(prefix) and 1 <= len(cs) - len(section) <= 2:
                    candidates.add(cs)
        if len(candidates) == 1:
            matched_sec = candidates.pop()
            for c in city_variants:
                rec = lvr_idx.get(f"{c}|{township}|{matched_sec}")
                if rec and rec.get(bucket_name) and rec[bucket_name]["count"] >= min_count:
                    return rec[bucket_name]
        return None

    # ── 任務一：資料配對演算法 (3 階段, collection-based) ────────
    def _dedup_re_list(r_list):
        """申報本欄 / 変動情形 / 信託申報 去重。
        同一 declaration 中 (normalized 坐落, 面積, 所有人) 相同的 entries 保留一筆。
        優先序：信託申報 (is_trust=1) > 変動情形 (trust=0 無括弧) > 申報本欄 (trust=0 有括弧)
        当 変動情形 蓋過 申報本欄 時，保留 申報本欄 的括弧標注（未能交付信託原因等）至 location。
        """
        import copy as _copy
        from collections import defaultdict as _dd2

        def _stype(r):
            """1=申報本欄, 2=変動情形, 3=信託申報"""
            if r.get("is_trust"):
                return 3
            if re.search(r"[（(]", (r.get("location") or "")):
                return 1
            return 2

        def _nkey(r):
            loc = re.sub(r"[（(].*?[）)]", "", (r.get("location") or "")).strip()
            # ownership_ratio 也納入 key：同一筆地在三個申報欄位持分相同，仍會正確去重；
            # 但同段地不同地號（持分各異）不能合併，否則多筆真實地號會被砍成一筆
            return (
                (r.get("estate_type") or ""),
                loc,
                str(r.get("area") or ""),
                str(r.get("owner") or ""),
                str(r.get("ownership_ratio") or ""),
            )

        by_key = _dd2(list)
        for r in r_list:
            by_key[_nkey(r)].append(r)

        def _real_price(r):
            """price > 1 才算真實申報值（cy 解析器對無價格建物有時存 1.0）"""
            p = r.get("price")
            return float(p) if (p is not None and float(p) > 1) else None

        def _clean_single(r):
            r = _copy.copy(r)
            if r.get("price") is not None and float(r.get("price") or 0) <= 1:
                r["price"] = None
            return r

        result = []
        for items in by_key.values():
            if len(items) == 1:
                result.append(_clean_single(items[0]))
                continue

            # 依申報欄位分組（stype 1=申報本欄, 2=変動情形, 3=信託申報）
            by_stype = _dd2(list)
            for r in items:
                by_stype[_stype(r)].append(r)

            max_per_section = max(len(v) for v in by_stype.values())

            if max_per_section > 1:
                # 某欄位內有多筆相同 key → 真正不同的地號（地號各異但面積相同），全部保留
                # 取最高優先欄位的所有筆數；若 winner 缺 price 則從低優先欄位補一個共用值
                for priority in [3, 2, 1]:
                    if priority not in by_stype:
                        continue
                    fallback_price = None
                    for lp in [2, 1]:
                        if lp >= priority:
                            continue
                        for other in by_stype.get(lp, []):
                            p = _real_price(other)
                            if p is not None:
                                fallback_price = p
                                break
                        if fallback_price is not None:
                            break
                    for r in by_stype[priority]:
                        r = _copy.copy(r)
                        if r.get("price") is not None and float(r.get("price") or 0) <= 1:
                            r["price"] = None
                        if _real_price(r) is None and fallback_price is not None:
                            r["price"] = fallback_price
                        result.append(r)
                    break
            else:
                # 每個欄位最多一筆 → 同地號在不同申報欄重複，取最高優先
                items_s = sorted(items, key=_stype, reverse=True)
                winner = _copy.copy(items_s[0])
                # 若 winner 是 変動情形，從 申報本欄 補回括弧標注
                if _stype(winner) == 2:
                    for other in items_s[1:]:
                        if _stype(other) == 1:
                            ann = other.get("location") or ""
                            if re.search(r"[（(]", ann):
                                winner["location"] = ann
                            break
                # 若 winner 無 price（信託申報通常不記申報現值），從其他版本補回
                if _real_price(winner) is None:
                    for other in items_s[1:]:
                        p = _real_price(other)
                        if p is not None:
                            winner["price"] = p
                            break
                else:
                    winner["price"] = _real_price(winner)
                result.append(winner)
        return result

    def group_real_estate(r_list):
        """把同 declaration 的不動產 entries 用 3 階段配對成「物件群組」。
        Stage 1/2/3 都用「collection」邏輯：同段同條件的所有 entries 組成 1 個 group
        (不是 pair-wise 1+1)。例：同段 4 土地 + 3 建物 同持分 → 1 group。
        Return: list of {"land": [..], "build": [..], "section": str, "mapping": str}
        """
        from collections import defaultdict as _dd
        def _loc_key(loc_str):
            """去括號備註後作為分組 key：
            「東峰段(未能交付信託原因：自用房屋之坐落基地)」和
            「東峰段(未能交付信託原因：自用房屋)」應視為同一段。"""
            return re.sub(r"[（(].*?[）)]", "", (loc_str or "").strip()).strip()

        # Pre-stage：「房地總價額」跨段或多筆配對
        # 情境1：土地在「木柵段二小段」、建物在「木新里木新路」→ _loc_key 不同段，正常分組無法配對
        # 情境2：都更後分配 1 土地 + 4 建物，各有不同持分/原因，Stage 1/2 無法歸為同組
        # 做法：找出所有 location 含「房地總價」且有 price 的 entries，
        #        按 price 分桶，若同一金額同時有土地+建物 → 先組成一個群組排除出後續分組
        _fudai_buckets = _dd(lambda: {"land": [], "build": []})
        for r in r_list:
            loc = (r.get("location") or "").strip()
            price = r.get("price")
            if price and "房地總價" in loc:
                et = r.get("estate_type") or ""
                if et == "土地":
                    _fudai_buckets[price]["land"].append(r)
                elif et == "建物":
                    _fudai_buckets[price]["build"].append(r)
        fudai_used = set()
        groups = []
        for price, bucket in _fudai_buckets.items():
            fl, fb = bucket["land"], bucket["build"]
            if fl and fb:
                groups.append({"land": fl, "build": fb,
                               "section": f"_fudai_{int(price)}", "mapping": "fudai_price_group"})
                for r in fl + fb:
                    fudai_used.add(id(r))

        by_section = _dd(lambda: {"land": [], "build": []})
        for r in r_list:
            if id(r) in fudai_used:
                continue  # 已由 pre-stage 處理
            loc = (r.get("location") or "").strip()
            if not loc:
                continue
            key = _loc_key(loc)
            et = r.get("estate_type") or ""
            if et == "土地":
                by_section[key]["land"].append(r)
            elif et == "建物":
                by_section[key]["build"].append(r)

        for loc, items in by_section.items():
            lands = list(items["land"])
            builds = list(items["build"])
            used = set()

            # Stage 1: 同段內，相同 ratio (非全部) → 1 個群組 (含所有同 ratio entries)
            ratio_buckets = _dd(lambda: {"land": [], "build": []})
            for l in lands:
                rp = _ratio_pair(l.get("ownership_ratio"))
                if rp and rp != (1, 1):
                    ratio_buckets[rp]["land"].append(l)
            for b in builds:
                rp = _ratio_pair(b.get("ownership_ratio"))
                if rp and rp != (1, 1):
                    ratio_buckets[rp]["build"].append(b)
            for rp, bucket in ratio_buckets.items():
                if bucket["land"] and bucket["build"]:
                    groups.append({"land": bucket["land"], "build": bucket["build"],
                                   "section": loc, "mapping": "ratio_match"})
                    for e in bucket["land"] + bucket["build"]:
                        used.add(id(e))

            # Stage 2: 同段內，剩餘的同日 + 同原因 + 同 ratio → 1 個群組
            # 注意：PDF 抽出的日期字串空白位置常不一致 (e.g. "106年06 月12日" vs
            # "106年06月 12日") — 比對前先 strip 所有空白
            # 同 ratio 條件防止「同一購地交易中不同持分的物件 (透天+大樓+純土地) 被混為一談」
            def _norm(s):
                return re.sub(r"\s+", "", (s or "").strip())
            def _date_only(s):
                """只取「XX年XX月XX日」部分，忽略 cy 來源 date 欄位中的附帶備註文字。"""
                n = _norm(s)
                m = re.match(r"(\d+年\d+月\d+日)", n)
                return m.group(1) if m else n
            rem_l = [l for l in lands if id(l) not in used]
            rem_b = [b for b in builds if id(b) not in used]
            ds_buckets = _dd(lambda: {"land": [], "build": []})
            for l in rem_l:
                ld = _date_only(l.get("acquisition_date"))
                lr = _norm(l.get("acquisition_reason"))
                lp = _ratio_pair(l.get("ownership_ratio")) or (0, 0)
                if ld and lr:
                    ds_buckets[(ld, lr, lp)]["land"].append(l)
            for b in rem_b:
                bd = _date_only(b.get("acquisition_date"))
                br = _norm(b.get("acquisition_reason"))
                bp = _ratio_pair(b.get("ownership_ratio")) or (0, 0)
                if bd and br:
                    ds_buckets[(bd, br, bp)]["build"].append(b)
            for key, bucket in ds_buckets.items():
                if bucket["land"] and bucket["build"]:
                    groups.append({"land": bucket["land"], "build": bucket["build"],
                                   "section": loc, "mapping": "date_source"})
                    for e in bucket["land"] + bucket["build"]:
                        used.add(id(e))

            # Stage 2.5: 持分土地 + 同日同原因之建物 (公寓型態：土地 ratio ≠ 建物 ratio)
            # 情境：10000分之90 土地 + 全部 建物 + 10000分之69 建物 (雙連段)
            # Stage 1/2 因 ratio 不同無法配對，此 stage 忽略 ratio 只看日期+原因
            rem_l25 = [l for l in lands if id(l) not in used]
            rem_b25 = [b for b in builds if id(b) not in used]
            dr_buckets = _dd(lambda: {"land": [], "build": []})
            for l in rem_l25:
                ld = _date_only(l.get("acquisition_date"))
                lr = _norm(l.get("acquisition_reason"))
                lp = _ratio_pair(l.get("ownership_ratio")) or (0, 0)
                if ld and lr and lp != (1, 1):  # 只做持分土地 (全部土地留給 Stage 3)
                    dr_buckets[(ld, lr)]["land"].append(l)
            for b in rem_b25:
                bd = _date_only(b.get("acquisition_date"))
                br = _norm(b.get("acquisition_reason"))
                if bd and br:
                    dr_buckets[(bd, br)]["build"].append(b)
            for key, bucket in dr_buckets.items():
                if bucket["land"] and bucket["build"]:
                    groups.append({"land": bucket["land"], "build": bucket["build"],
                                   "section": loc, "mapping": "date_source_frac"})
                    for e in bucket["land"] + bucket["build"]:
                        used.add(id(e))

            # Stage 2.6: 自用房屋坐落基地 ↔ 自用房屋建物配對
            # 土地 location 含「自用房屋」且 建物 location 含「自用房屋」→ 同段配對
            rem_l26 = [l for l in lands if id(l) not in used]
            rem_b26 = [b for b in builds if id(b) not in used]
            self_l = [l for l in rem_l26 if re.search(r"自用房屋", (l.get("location") or ""))]
            self_b = [b for b in rem_b26 if re.search(r"自用房屋", (b.get("location") or ""))]
            if self_l and self_b:
                groups.append({"land": self_l, "build": self_b,
                               "section": loc, "mapping": "jiyutaku"})
                for e in self_l + self_b:
                    used.add(id(e))

            # Stage 2.7: 信託申報中，同段同持分的剩餘土地併入已有群組
            # 解決：公寓基地兩個子地號信託移轉日期不同，Stage 2.5 只配對到其中一筆
            rem_l27 = [l for l in lands if id(l) not in used]
            for l in list(rem_l27):
                lp = _ratio_pair(l.get("ownership_ratio"))
                if not lp or lp == (1, 1):
                    continue
                if not (l.get("is_trust") or
                        re.sub(r"\s+", "", (l.get("acquisition_reason") or "")) == "信託"):
                    continue
                for g in groups:
                    if g.get("section") != loc:
                        continue
                    if any(_ratio_pair(x.get("ownership_ratio")) == lp for x in g["land"]):
                        g["land"].append(l)
                        used.add(id(l))
                        break

            # Stage 3: 同段歸戶 (fallback)
            # Gemini 規則：「持分多為全部」才視為複合型專案 (購地自建)
            # 非全部持分的 leftover → 各自獨立成「純土地」
            rem_l = [l for l in lands if id(l) not in used]
            rem_b = [b for b in builds if id(b) not in used]
            full_l = [l for l in rem_l if _is_full_ratio(l.get("ownership_ratio"))]
            frac_l = [l for l in rem_l if not _is_full_ratio(l.get("ownership_ratio"))]
            full_b = [b for b in rem_b if _is_full_ratio(b.get("ownership_ratio"))]
            frac_b = [b for b in rem_b if not _is_full_ratio(b.get("ownership_ratio"))]

            # 全部持分的 lands + builds → 複合型 (購地自建)
            if full_l and full_b:
                groups.append({"land": full_l, "build": full_b, "section": loc, "mapping": "complex"})
            elif full_l:
                # 全部持分土地 (無對應建物) → 純土地 (透天用地 / 農牧地)
                for l in full_l:
                    groups.append({"land": [l], "build": [], "section": loc, "mapping": "land_only"})
            elif full_b:
                for b in full_b:
                    groups.append({"land": [], "build": [b], "section": loc, "mapping": "build_only"})

            # 持分土地 leftover → 純土地 (公寓基地, 持分小)
            for l in frac_l:
                groups.append({"land": [l], "build": [], "section": loc, "mapping": "land_only"})
            for b in frac_b:
                groups.append({"land": [], "build": [b], "section": loc, "mapping": "build_only"})
        return groups

    # ── 任務一輔助：群組類別偵測 (4 類) ─────────────────────────
    def detect_category(group):
        """4 類偵測：apartment / townhouse / land_only / complex / parking

        類別優先序：
        1. mapping="complex" (Stage 3 fallback) → 複合型 (購地自建/農牧場專案)
        2. 純土地 / 純建物 → land_only / apartment
        3. 車位/公設 → parking
        4. 全部 ratio → townhouse; 持分 → apartment
        """
        lands = group["land"]
        builds = group["build"]
        mapping = group.get("mapping") or ""

        # Stage 3 fallback 一律 complex (自地自建、農牧場、無法精確切割的專案)
        if mapping == "complex":
            return "complex"

        if not builds and lands:
            return "land_only"
        if not lands and builds:
            return "apartment"  # 純建物罕見，先當公寓
        # 公設/車位偵測：建物持分極低 + 備註含關鍵字
        for b in builds:
            ratio = _ratio(b.get("ownership_ratio"))
            if ratio and 0 < ratio < 0.01:  # < 1%
                loc = b.get("location") or ""
                if "車位" in loc or "停車" in loc:
                    return "parking"
        # 公寓 vs 透天：看土地持分樣態
        land_ratios = [_ratio(l.get("ownership_ratio")) for l in lands]
        has_full_land = any(abs(r - 1.0) < 1e-6 for r in land_ratios if r)
        has_frac_land = any(0 < r < 1.0 for r in land_ratios if r)
        if has_full_land and not has_frac_land:
            # 容積率 >= 3 → 大型建物，改歸公寓大樓
            total_land_area  = sum(float(l.get("area") or 0) for l in lands)
            total_build_area = sum(float(b.get("area") or 0) for b in builds)
            if total_land_area > 0 and total_build_area / total_land_area >= 3.0:
                return "apartment"
            return "townhouse"
        if has_frac_land:
            return "apartment"
        return "complex"

    # ── 有效面積安全閥 ─────────────────────────────────────────
    # 有效面積（area × ratio）超過以下上限時，LVR 估值結果明顯不合理，跳過。
    # 申報原始價格（price）仍保留供前端顯示。
    _MAX_BUILD_EFF = 5_000   # 建物有效面積上限（平方公尺）
    _MAX_LAND_EFF  = 2_000_000  # 土地有效面積上限（100 公頃以上幾乎不可能是住宅用地）

    # ── 任務三：依類別估值 ─────────────────────────────────────
    def value_group(group):
        """套 4 類估值規則。回傳 (enriched_count, category)"""
        section_loc = group["section"]
        parsed = _parse_re_loc(section_loc)
        if not parsed:
            return 0, "no_section"
        city, township, section = parsed
        cat = detect_category(group)

        if cat == "apartment":
            # 先標分類（無論有無 LVR 資料，前端都要看到「區分所有建物」標籤）
            for b in group["build"]:
                b["valuation_category"] = "區分所有建物"
            for l in group["land"]:
                l["valuation_category"] = "區分所有建物"
                l["valuation_note"] = "已併入同案建物估算"
            # 區分所有建物：建物 × ratio × house 房地單價，土地端歸 0 標已併入
            # 注意：必須乘 ratio，否則公設/共用部分大面積×低持分會被當全持分算到爆
            stats = _lvr_lookup_one(city, township, section, "house", min_count=1)
            if not stats:
                # fallback：無公寓房屋 LVR 資料時，改以土地單價估算地價
                # 優先 townhouse_land（含建物的總地價），次選 land_only
                land_stats = _lvr_lookup_one(city, township, section, "townhouse_land", min_count=3)
                src = "townhouse_land"
                if not land_stats:
                    land_stats = _lvr_lookup_one(city, township, section, "land_only", min_count=3)
                    src = "land_only"
                if not land_stats:
                    land_stats = _lvr_lookup_one(city, township, section, "land_only", min_count=1)
                    src = "land_only"
                if land_stats:
                    unit = land_stats["median"]
                    cnt = 0
                    for l in group["land"]:
                        area = float(l.get("area") or 0)
                        ratio = _ratio(l.get("ownership_ratio"))
                        l["valuation_category"] = "區分所有建物"
                        if area > 0 and ratio > 0 and area * ratio <= _MAX_LAND_EFF:
                            l["estimated_price"] = int(area * ratio * unit)
                            l["lvr_unit_price"] = unit
                            l["lvr_sample_count"] = land_stats["count"]
                            l["lvr_source"] = src
                            l["valuation_note"] = "無公寓LVR，以地價估算（僅供參考）"
                            cnt += 1
                    for b in group["build"]:
                        b["valuation_category"] = "區分所有建物"
                        b["estimated_price"] = 0
                        b["lvr_source"] = "included_in_land"
                        b["valuation_note"] = "已併入同案土地估算"
                    return cnt, cat
                return 0, cat
            unit = stats["median"]
            cnt = 0
            for b in group["build"]:
                area = float(b.get("area") or 0)
                ratio = _ratio(b.get("ownership_ratio"))
                b["valuation_category"] = "區分所有建物"
                if area > 0 and ratio > 0:
                    oversized = area * ratio > _MAX_BUILD_EFF
                    b["estimated_price"] = int(area * ratio * unit)
                    b["lvr_unit_price"] = unit
                    b["lvr_sample_count"] = stats["count"]
                    b["lvr_source"] = "house"
                    if oversized:
                        b["valuation_note"] = "大型建物，以住宅實價登錄估算，僅供參考"
                    cnt += 1
                else:
                    # 公設/共有部分：面積或持分不明，不估值但保持同群組分類
                    b["estimated_price"] = 0
                    b["lvr_source"] = "included_in_build"
                    b["valuation_note"] = "公設/共有部分，已併入主建物估算"
            for l in group["land"]:
                l["estimated_price"] = 0
                l["lvr_source"] = "included_in_build"
                l["valuation_category"] = "區分所有建物"
                l["valuation_note"] = "已併入同案建物估算"
            return cnt, cat

        if cat == "townhouse":
            # 透天/別墅：土地 × 地坪單價 (總價/土地面積)，建物端歸 0
            stats = _lvr_lookup_one(city, township, section, "townhouse_land", min_count=1)
            if not stats:
                # fallback：土地單價 (純土地) 用 land_only
                stats = _lvr_lookup_one(city, township, section, "land_only", min_count=3)
                if not stats:
                    return 0, cat
            unit = stats["median"]
            cnt = 0
            for l in group["land"]:
                area = float(l.get("area") or 0)
                ratio = _ratio(l.get("ownership_ratio"))
                if area > 0 and ratio > 0:
                    if area * ratio > _MAX_LAND_EFF:
                        l["valuation_category"] = "透天/別墅"
                        continue
                    l["estimated_price"] = int(area * ratio * unit)
                    l["lvr_unit_price"] = unit
                    l["lvr_sample_count"] = stats["count"]
                    l["lvr_source"] = "townhouse_land"
                    l["valuation_category"] = "透天/別墅"
                    cnt += 1
            for b in group["build"]:
                b["estimated_price"] = 0
                b["lvr_source"] = "included_in_land"
                b["valuation_category"] = "透天/別墅"
                b["valuation_note"] = "已併入同案土地估算"
            return cnt, cat

        if cat == "land_only":
            # 純土地：land_only 單價 × 持分面積
            stats = _lvr_lookup_one(city, township, section, "land_only", min_count=3)
            if not stats:
                # fallback：house * 0.7 (都市住宅土地 ≈ 房地 70%)
                h = _lvr_lookup_one(city, township, section, "house", min_count=3)
                if h:
                    stats = {**h, "median": int(h["median"] * 0.7)}
                else:
                    stats = _lvr_lookup_one(city, township, section, "land_only", min_count=1)
                    if not stats:
                        # 無 LVR 資料仍標記類別，讓前端能顯示「純土地」badge
                        for l in group["land"]:
                            l["valuation_category"] = "純土地"
                        return 0, cat
            unit = stats["median"]
            cnt = 0
            for l in group["land"]:
                area = float(l.get("area") or 0)
                ratio = _ratio(l.get("ownership_ratio"))
                if area > 0 and ratio > 0:
                    if area * ratio > _MAX_LAND_EFF:
                        l["valuation_category"] = "純土地"
                        continue
                    l["estimated_price"] = int(area * ratio * unit)
                    l["lvr_unit_price"] = unit
                    l["lvr_sample_count"] = stats["count"]
                    l["lvr_source"] = "land_only"
                    l["valuation_category"] = "純土地"
                    cnt += 1
            return cnt, cat

        if cat == "parking":
            # 車位/公設：暫不估值 (TODO: 行政區車位均價)
            for b in group["build"]:
                b["estimated_price"] = None
                b["lvr_source"] = "parking_skip"
                b["valuation_category"] = "車位/公設"
                b["valuation_note"] = "車位獨立估值待補"
            return 0, cat

        if cat == "complex":
            # 複合型 (Stage 3 fallback)：自地自建 / 農牧場 / 多筆混合
            # 任務二「購地自建加總法」：若已有 price，全部加總；不再 LVR 估值
            has_any_price = any((e.get("price") or 0) > 0 for e in group["land"] + group["build"])
            cnt = 0
            if not has_any_price:
                # 估值策略：台灣 house LVR 單價已含土地成分，不能同時加土地 land_only
                # → 有建物且有 house LVR：建物估算，土地標 included_in_build
                # → 無 house LVR（農牧地等）：土地估算，建物標 included_in_land
                build_stats = _lvr_lookup_one(city, township, section, "house", min_count=1) \
                              if group["build"] else None
                land_stats = None if build_stats else (
                    _lvr_lookup_one(city, township, section, "land_only", min_count=1) or
                    _lvr_lookup_one(city, township, section, "townhouse_land", min_count=1)
                )
                for e in group["land"] + group["build"]:
                    e["valuation_category"] = "複合型 (購地自建)"
                    et = e.get("estate_type")
                    area = float(e.get("area") or 0)
                    ratio = _ratio(e.get("ownership_ratio"))
                    max_eff = _MAX_BUILD_EFF if et == "建物" else _MAX_LAND_EFF
                    if build_stats and et == "建物":
                        if area > 0 and ratio > 0 and area * ratio <= max_eff:
                            e["estimated_price"] = int(area * ratio * build_stats["median"])
                            e["lvr_unit_price"] = build_stats["median"]
                            e["lvr_sample_count"] = build_stats["count"]
                            e["lvr_source"] = "complex_build"
                            cnt += 1
                    elif build_stats and et == "土地":
                        # house LVR 已含地價，土地端不另估
                        e["estimated_price"] = 0
                        e["lvr_source"] = "included_in_build"
                        e["valuation_note"] = "土地已併入建物估算（house LVR 含地價）"
                    elif land_stats and et == "土地":
                        if area > 0 and ratio > 0 and area * ratio <= max_eff:
                            e["estimated_price"] = int(area * ratio * land_stats["median"])
                            e["lvr_unit_price"] = land_stats["median"]
                            e["lvr_sample_count"] = land_stats["count"]
                            e["lvr_source"] = "complex_land"
                            cnt += 1
                    elif land_stats and et == "建物":
                        # 無 house LVR，建物端不另估
                        e["estimated_price"] = 0
                        e["lvr_source"] = "included_in_land"
                        e["valuation_note"] = "建物已併入土地估算"
            else:
                for e in group["land"] + group["build"]:
                    e["valuation_category"] = "複合型 (購地自建)"
            return cnt, cat

        return 0, cat

    # ── 任務二：已有金額的群組處理 (房地總價 / 購地自建) ──────────
    def apply_price_logic(group):
        """處理已有 price 的群組：房地總價排除 + 購地自建加總。"""
        lands = group["land"]
        builds = group["build"]
        # 房地總價排除：若任一 entry 的 location 含「房地總價」標註，視為該邊已含對方
        for entries, other in [(lands, builds), (builds, lands)]:
            for e in entries:
                loc = e.get("location") or ""
                if "房地總價" in loc:
                    for o in other:
                        if o.get("price"):
                            o["price_attributed"] = 0
                            o["valuation_note"] = "已併入對方之房地總價"
        # 購地自建：土地=買賣 + 建物=第一次登記 → 兩邊金額皆計 (預設行為)
        # 不需特別處理；只標記類別讓 UI 顯示
        has_buy_land = any((l.get("acquisition_reason") or "") == "買賣" for l in lands)
        has_first_build = any((b.get("acquisition_reason") or "") == "第一次登記" for b in builds)
        if has_buy_land and has_first_build:
            for e in lands + builds:
                e["valuation_category"] = e.get("valuation_category") or "購地自建"

    # ── 主流程 ────────────────────────────────────────────────
    # 申報本欄 / 変動情形 / 信託申報 去重（更新 dict 本身，影響後續 JSON 輸出與估值）
    for _did in list(details_by_decl["real_estate"]):
        details_by_decl["real_estate"][_did] = _dedup_re_list(
            details_by_decl["real_estate"][_did]
        )

    re_enriched = 0
    re_total_no_price = 0
    cat_stats = defaultdict(int)
    _gid = 0  # 全域群組流水號，寫入每筆 entry，讓前台不必重新分組
    for r_list in details_by_decl["real_estate"].values():
        groups = group_real_estate(r_list)
        for g in groups:
            # 已有 price 的群組：套 price logic
            has_any_price = any((e.get("price") or 0) > 0 for e in g["land"] + g["build"])
            if has_any_price:
                apply_price_logic(g)
            # 無 price 的 entries 走估值 (price 已存的不覆寫)
            cnt, cat = value_group(g)
            # fudai_price_group 的 section="_fudai_N" 無法被 _parse_re_loc 解析，
            # value_group 提早返回 no_section 未設 valuation_category；補上類別標籤
            if cat == "no_section":
                _fb = detect_category(g)
                _fb_label = {"apartment": "區分所有建物", "townhouse": "透天/別墅",
                             "land_only": "純土地", "complex": "複合型 (購地自建)"}.get(_fb, "")
                if _fb_label:
                    for e in g["land"] + g["build"]:
                        if not e.get("valuation_category"):
                            e["valuation_category"] = _fb_label
            cat_stats[cat] += 1
            re_enriched += cnt
            # 寫入群組 ID（前台用此做分群，確保 Python 分組結果在顯示層忠實呈現）
            for e in g["land"] + g["build"]:
                e["_group_id"] = _gid
                if not e.get("price"):
                    re_total_no_price += 1
            _gid += 1

    pct = re_enriched / re_total_no_price * 100 if re_total_no_price else 0
    print(f"  LVR enriched: {re_enriched:,} / {re_total_no_price:,} entries ({pct:.1f}%)")
    print(f"  category distribution:")
    for cat, n in sorted(cat_stats.items(), key=lambda x: -x[1]):
        print(f"    {cat:<20}: {n:,} groups")

    # 全部 decls 加上 normalized 日期，後續排序統一用 ISO
    for d in all_decls:
        d["_iso_date"] = normalize_date(d.get("decl_date"))

    # ── 同 (politician, decl_date) 多個 decl 去重：
    # 後一期廉政專刊登出「補正」時，會把申報人事後自願更正的 1-2 行單獨重刊，
    # 系統若全部累計，會把同一筆債權/債務算成兩次。保留 detail 最多的一個。
    print("\n=== Deduping same-date 補正 republications ===")
    from collections import defaultdict as _dd
    same_day_groups = _dd(list)
    for d in all_decls:
        key = (d.get("name"), d.get("_iso_date"))
        if key[0] and key[1]:
            same_day_groups[key].append(d)
    drop_ids = set()
    for key, lst in same_day_groups.items():
        if len(lst) <= 1:
            continue
        # 計算每個 decl 的 detail 數量
        def _cnt(dx):
            i = dx["id"]
            return sum(len(details_by_decl[t].get(i, [])) for t in ("deposits","securities","real_estate","debts","other_assets"))
        lst_sorted = sorted(lst, key=lambda dx: (-_cnt(dx), dx.get("issue_number") or 0))
        winner = lst_sorted[0]
        winner_id = winner["id"]
        # 異動申報 (is_change=1) 保留：那本來就是「異動清單」非主申報的子集
        non_change_losers = [dx for dx in lst_sorted[1:] if not dx.get("is_change")]
        for dx in non_change_losers:
            drop_ids.add(dx["id"])

        # 不動產去重 key：去掉空白與 1★/2★ 列號標記
        def _re_key(e):
            loc = re.sub(r"\s+", "", (e.get("location") or ""))
            loc = re.sub(r"^\d*[★☆]", "", loc)
            return (loc,
                    re.sub(r"\s+", "", (e.get("acquisition_date") or "")),
                    re.sub(r"\s+", "", (e.get("acquisition_reason") or "")))

        # 非不動產：直接 append（信託/存款/有價證券互補，不重複）
        for dx in non_change_losers:
            loser_id = dx["id"]
            for t in ("deposits", "securities", "debts", "other_assets"):
                loser_items = details_by_decl[t].get(loser_id, [])
                if loser_items:
                    details_by_decl[t].setdefault(winner_id, [])
                    details_by_decl[t][winner_id] = details_by_decl[t][winner_id] + loser_items

        # 不動產：按 issue_number 從高到低合併（補正申報的正確值優先）
        # 変動申報 的不動產是「異動清單」，列的是已變動／已售的舊資產，不應與定期申報合併
        re_losers = [dx for dx in sorted(non_change_losers, key=lambda x: -(x.get("issue_number") or 0))
                     if dx.get("decl_type") != "變動申報"]
        details_by_decl["real_estate"].setdefault(winner_id, [])
        winner_re_keys = {_re_key(e) for e in details_by_decl["real_estate"][winner_id]}
        for dx in re_losers:
            loser_id = dx["id"]
            loser_re = details_by_decl["real_estate"].get(loser_id, [])
            new_re = [e for e in loser_re if _re_key(e) not in winner_re_keys]
            if new_re:
                details_by_decl["real_estate"][winner_id] = \
                    details_by_decl["real_estate"][winner_id] + new_re
                winner_re_keys.update(_re_key(e) for e in new_re)
    if drop_ids:
        all_decls = [d for d in all_decls if d["id"] not in drop_ids]
        print(f"  merged+dropped {len(drop_ids)} 補正/重刊/信託 decls (same date → merged into larger version)")

    # ── 名字 normalize：把同一人不同寫法 (例:「巴干．巴萬Bakan Pawan」「巴干‧巴萬 Bakan Pawan」「巴干‧巴萬(Bakan Pawan)」) 合併 ──
    def _normalize_person_name(name: str) -> str:
        """產生同一人不同寫法的共通 key
        - 各種中點: ． · ‧ ・ . → 統一移除
        - 括號字元剝除 (但保留括號內的拉丁名字)
        - 全部空白移除 (避免 "Bakan Pawan" vs "BakanPawan")
        - 全部小寫
        """
        if not name:
            return ''
        n = str(name).strip()
        # 剝括號字元 (但保留內容)
        n = re.sub(r'[（）()]', '', n)
        # 去各種中點 / 點號
        n = re.sub(r'[．·‧・.]', '', n)
        # 全部空白移除
        n = re.sub(r'\s+', '', n)
        return n.lower()

    # ── Group declarations by politician (用 normalized key 合併同一人不同寫法) ─────────────
    by_name_norm = defaultdict(list)
    for d in all_decls:
        key = _normalize_person_name(d.get("name") or '')
        if not key:
            continue
        by_name_norm[key].append(d)

    # 對每個 norm key，選顯示名：優先 (1) 無括號 (2) 最新申報用的寫法 (3) 最長
    by_name = {}
    name_merge_log = []
    for key, decls in by_name_norm.items():
        variants = list({(d.get('name') or '').strip() for d in decls if d.get('name')})
        # 找最新 decl 用的 name
        decls_sorted = sorted(decls, key=lambda d: d.get('_iso_date') or '', reverse=True)
        latest_name = (decls_sorted[0].get('name') or '').strip() if decls_sorted else ''
        # 排序鍵：(無括號優先, 是最新申報的優先, 長度長優先, 字典序)
        variants.sort(key=lambda x: (
            1 if ('(' in x or '（' in x) else 0,
            0 if x == latest_name else 1,
            -len(x),
            x,
        ))
        canonical = variants[0]
        by_name[canonical] = decls
        if len(variants) > 1:
            name_merge_log.append((canonical, [v for v in variants[1:] if v]))
    if name_merge_log:
        print(f"\n=== Merged {len(name_merge_log)} politicians with name variants ===")
        for canon, others in name_merge_log[:10]:
            print(f"  '{canon}' ← {others}")
        if len(name_merge_log) > 10:
            print(f"  ... and {len(name_merge_log)-10} more")

    print(f"\n=== Building per-politician JSON ===")
    politicians_summary = []
    per_file_sizes = []

    for name, decls in sorted(by_name.items(), key=lambda x: x[0] or ""):
        if not name:
            continue
        # Sort by date desc（用 normalized ISO 字串，可正確比較民國年 vs 公元年）
        decls.sort(key=lambda x: x.get("_iso_date") or "", reverse=True)

        # Build per-politician file: includes every declaration + every detail row
        per_data = {
            "name": name,
            "num_declarations": len(decls),
            "declarations": [],
        }
        for d in decls:
            decl_id = d["id"]
            entry = {
                "id": decl_id,
                "issue_number": d.get("issue_number"),
                "source": d.get("source"),
                "page_start": d.get("page_start"),
                "organization": d.get("organization"),
                "title": d.get("title"),
                "decl_date": d.get("decl_date"),
                "decl_date_iso": d.get("_iso_date"),
                "decl_type": d.get("decl_type"),
                "is_change": d.get("is_change"),
                "spouse": d.get("spouse"),
                "total_deposits": d.get("total_deposits"),
                "total_cash": d.get("total_cash"),
                "total_securities": d.get("total_securities"),
                "total_debt": d.get("total_debt"),
                "deposits": details_by_decl["deposits"].get(decl_id, []),
                "securities": details_by_decl["securities"].get(decl_id, []),
                "real_estate": details_by_decl["real_estate"].get(decl_id, []),
                "debts": details_by_decl["debts"].get(decl_id, []),
                "other_assets": details_by_decl["other_assets"].get(decl_id, []),
            }
            per_data["declarations"].append(entry)

        # ── 計算 money_decl 淨資產（供 per-person JSON 和 politicians.json 共用）──
        # 列表金額用「最新一筆有解析出任何金額」的申報
        def latest_complete_decl():
            for d in decls:
                if (d.get("total_deposits") or d.get("total_securities")
                        or d.get("total_debt") or d.get("total_cash")):
                    return d
            return decls[0]
        money_decl = latest_complete_decl()
        money_decl_date = money_decl.get("decl_date")

        money_decl_id = money_decl.get("id")
        money_deposits = details_by_decl["deposits"].get(money_decl_id, [])
        money_securities = details_by_decl["securities"].get(money_decl_id, [])
        money_real_estate = details_by_decl["real_estate"].get(money_decl_id, [])
        money_debts = details_by_decl["debts"].get(money_decl_id, [])
        money_other = details_by_decl["other_assets"].get(money_decl_id, [])

        def _to_num(v):
            try:
                return float(v) if v is not None else 0.0
            except (ValueError, TypeError):
                return 0.0

        def parse_ratio(s):
            if s is None:
                return 1.0
            s = str(s).strip()
            if not s or s == "全部":
                return 1.0
            if s in ("公同共有", "共有"):
                return None
            m = re.match(r'^(\d+)\s*分之\s*(\d+)$', s)
            if m:
                return int(m.group(2)) / int(m.group(1))
            m = re.match(r'^(\d+)\s*/\s*(\d+)$', s)
            if m:
                return int(m.group(1)) / int(m.group(2))
            m = re.match(r'^(\d+(?:\.\d+)?)%$', s)
            if m:
                return float(m.group(1)) / 100
            return None

        # 現金/債權/保險/古董/虛擬資產也在 other_assets 裡
        cash_items     = [o for o in money_other if "現金" in (o.get("asset_type") or "")]
        claim_items    = [o for o in money_other if "債權" in (o.get("asset_type") or "")]
        insurance_items= [o for o in money_other if "保險" in (o.get("asset_type") or "")]
        antique_items  = [o for o in money_other if "古董" in (o.get("asset_type") or "") or "珠寶" in (o.get("asset_type") or "")]
        virtual_items  = [o for o in money_other if "虛擬" in (o.get("asset_type") or "")]

        # 取 fallback：total 為空時用明細加總
        total_deposits = money_decl.get("total_deposits") or sum(_to_num(d.get("twd_amount")) for d in money_deposits)
        # 有價證券：優先用 current_value（市值估算），未對應到的退回 twd_amount
        total_securities = sum(
            _to_num(s.get("current_value")) or _to_num(s.get("twd_amount"))
            for s in money_securities
        )
        total_cash = money_decl.get("total_cash") or sum(_to_num(o.get("amount")) for o in cash_items)
        total_debt = money_decl.get("total_debt") or sum(_to_num(d.get("balance")) for d in money_debts)

        # 不動產：Σ(持分比 × 取得價額)；沒填取得價額者用實價登錄估值
        # 同一群組（同 _group_id）的申報項目中，相同取得價額只計一次
        # （避免合購建物各筆因備注文字不同而 location key 不同，導致房地總價額重複累計）
        total_real_estate = 0.0
        _seen_group_price: dict = {}  # _group_id（或 location）→ set of counted prices
        for r in money_real_estate:
            # 已併入他項的不計（land included_in_build / build included_in_land）
            if (r.get("lvr_source") or "").startswith("included_in"):
                continue
            ratio = parse_ratio(r.get("ownership_ratio"))
            price = _to_num(r.get("price"))
            est = _to_num(r.get("estimated_price"))
            if ratio and price:
                gid = r.get("_group_id")
                dedup_key = gid if gid is not None else re.sub(r"\s+", "", (r.get("location") or ""))
                seen = _seen_group_price.setdefault(dedup_key, set())
                if price not in seen:
                    total_real_estate += price   # price 已是持分金額，不再乘 ratio
                    seen.add(price)
            elif est:
                total_real_estate += est

        # 交通工具：汽車 + 船舶 + 航空器
        total_vehicles = sum(_to_num(o.get("amount")) for o in money_other
                             if o.get("asset_type") in ("汽車", "船舶", "航空器"))

        # 事業投資
        total_investment = sum(_to_num(o.get("amount")) for o in money_other
                               if o.get("asset_type") == "事業投資")

        # 新增 4 類（債權 / 保險 / 古董珠寶 / 虛擬資產）
        total_claim     = sum(_to_num(o.get("amount")) for o in claim_items)
        total_insurance = sum(_to_num(o.get("amount")) for o in insurance_items)
        total_antique   = sum(_to_num(o.get("amount")) for o in antique_items)
        total_virtual   = sum(_to_num(o.get("amount")) for o in virtual_items)

        # 淨資產
        net_assets = (total_deposits + total_securities + total_cash
                      + total_real_estate + total_vehicles + total_investment
                      + total_claim + total_insurance + total_antique + total_virtual
                      - total_debt)

        # 寫入 per-person JSON（內嵌預計算的淨資產摘要，讓 profile-page.html 直接使用）
        per_data["net_worth_summary"] = {
            "money_decl_id": money_decl_id,
            "money_decl_date": money_decl_date,
            "money_decl_date_iso": money_decl.get("_iso_date"),
            "total_deposits": total_deposits or None,
            "total_securities": total_securities or None,
            "total_real_estate": total_real_estate or None,
            "total_vehicles": total_vehicles or None,
            "total_cash": total_cash or None,
            "total_investment": total_investment or None,
            "total_claim": total_claim or None,
            "total_antique": total_antique or None,
            "total_virtual": total_virtual or None,
            "total_debt": total_debt or None,
            "net_assets": net_assets,
        }
        fname = safe_filename(name)
        out_path = PER_DIR / fname
        out_path.write_text(
            json.dumps(per_data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        per_file_sizes.append(out_path.stat().st_size)

        # Build summary for politicians.json
        orgs = sorted(set(d.get("organization") for d in decls if d.get("organization")))
        latest = decls[0]
        dates = [d.get("decl_date") for d in decls if d.get("decl_date")]

        politicians_summary.append({
            "name": name,
            "num_declarations": len(decls),
            "organizations": ", ".join(orgs[:3]),
            "latest_organization": latest.get("organization"),
            "latest_title": latest.get("title"),
            "latest_org_type": categorize_org(latest.get("organization") or ""),
            "earliest_date": min(dates) if dates else None,
            "latest_date": max(dates) if dates else None,
            "total_deposits": total_deposits or None,
            "total_securities": total_securities or None,
            "total_real_estate": total_real_estate or None,
            "total_vehicles": total_vehicles or None,
            "total_cash": total_cash or None,
            "total_investment": total_investment or None,
            "total_debt": total_debt or None,
            "net_assets": net_assets,
            "money_decl_date": money_decl_date,
            "file": fname,
        })

    # Write politicians.json
    politicians_path = OUT_DIR / "politicians.json"
    politicians_path.write_text(
        json.dumps({
            "count": len(politicians_summary),
            "politicians": politicians_summary,
        }, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    # ── 建反向索引：公司 → 持股政治人物列表（使用 normalized key）──
    # 重要：使用 details_by_decl 中已 enrich 過 current_value 的 securities，
    # 並依 sec_type / 海外註記分群，避免 TWSE 2330 跟 TSM ADR 混在同一個「台積電」群組。
    print("\n=== Building securities reverse index ===")

    def normalize_company(name: str) -> str:
        if not name:
            return ''
        n = re.sub(r'[（(][^）)]*[）)]', '', name)
        n = re.sub(r'股份有限公司|有限公司|集團$', '', n)
        n = n.replace('商業銀行', '銀行')
        n = re.sub(r'^[\d\s★☆▲▼※◎●○]+', '', n)
        return n.replace(' ', '').strip()

    OVERSEAS_NOTE_KEY_RE = re.compile(r'[（(][^）)]*(?:國外|海外|ADR|未上市|未上櫃)[^）)]*[）)]')

    # 用官方 alias 表建「代號 → 簡稱」反查表 (用於以 match_code 為分組 key 時顯示用)
    code_to_short_name = {}
    try:
        ali_raw = json.loads(COMPANY_ALIASES_PATH.read_text(encoding='utf-8')) if COMPANY_ALIASES_PATH.exists() else {}
        for code, info in (ali_raw.get('by_code') or {}).items():
            short = (info.get('short') or '').strip()
            if short:
                code_to_short_name[code] = short
    except Exception:
        pass

    def company_group_key(orig_name: str, sec_type: str, match_code: str | None) -> str:
        """同 TWSE/TPEx 代號的持股應合併，不管申報時寫的是「台積電」還是「台灣積體電路製造」。

        策略：有 match_code 就用代號當分組 (顯示用官方簡稱)，否則退回用名稱分群。
        海外/基金/債券型態加後綴避免不同類型混淆。"""
        st = (sec_type or '').strip()
        is_overseas = bool(OVERSEAS_NOTE_KEY_RE.search(orig_name or ''))

        # 有 match_code 且非海外/基金/債券 (這些有分群後綴) → 用官方簡稱當 key
        if match_code and st == '股票' and not is_overseas:
            short = code_to_short_name.get(match_code) or normalize_company(orig_name) or match_code
            return short

        base = normalize_company(orig_name)
        if not base:
            return ''
        if st == '其他證券' or is_overseas:
            return f'{base} (海外)'
        if st == '基金':
            # 基金有 match_code (ETF/SITCA) 也合併以代號簡稱為 key
            if match_code:
                short = code_to_short_name.get(match_code)
                if short:
                    return f'{short} (基金)'
            return f'{base} (基金)'
        if st in ('債券', '公債', '其他公債'):
            return f'{base} (債券)'
        return base

    # 用 decl_by_id 拿政治人物 metadata（已被 HIDE_SOURCES 過濾、去重後）
    decl_meta = {d['id']: d for d in all_decls}

    # 每位政治人物的「最新申報年」(用於過濾已賣出的持股 — 若最新申報不再持有就不該在 co-holder 中顯示)
    politician_latest_year = {}
    for d in all_decls:
        y = (d.get('_iso_date') or '')[:4]
        if not y or not d.get('name'):
            continue
        if d['name'] not in politician_latest_year or y > politician_latest_year[d['name']]:
            politician_latest_year[d['name']] = y

    company_index = defaultdict(list)
    for decl_id, sec_list in details_by_decl['securities'].items():
        d = decl_meta.get(decl_id)
        if not d:
            continue  # 已被 HIDE_SOURCES 或同日去重移除
        year = (d.get('_iso_date') or '')[:4]
        for s in sec_list:
            orig = (s.get('name') or '').strip()
            key = company_group_key(orig, s.get('sec_type') or '', s.get('match_code'))
            if not key:
                continue
            cv = s.get('current_value')
            tw = s.get('twd_amount')
            company_index[key].append({
                'politician': d['name'],
                'owner': s.get('owner') or '',
                'twd_amount': tw,
                'current_value': cv,
                'quantity': s.get('quantity'),
                'year': year or None,
                'sec_type': s.get('sec_type'),
                'orig_name': orig,
                'organization': d.get('organization') or '',
                'title': d.get('title') or '',
                'match_code': s.get('match_code'),
            })

    # 每家公司：只保留每位政治人物「最新申報年」的持股 —
    # 若某人最近申報已沒這支股票 (= 賣出) 就不該再列入 co-holder。
    def _amt(h):
        return h.get('current_value') or h.get('twd_amount') or 0
    final_index = {}
    filtered_old = 0
    for company, holdings in company_index.items():
        kept = []
        for h in holdings:
            latest = politician_latest_year.get(h['politician'])
            if latest and h['year'] and h['year'] == latest:
                kept.append(h)
            else:
                filtered_old += 1
        if len(set(h['politician'] for h in kept)) < 2:
            continue
        # 同 politician + owner 去重 (PDF 把同支股票拆成多列時)
        seen = set()
        deduped = []
        for h in sorted(kept, key=lambda x: -_amt(x)):
            k = (h['politician'], h['owner'])
            if k in seen:
                continue
            seen.add(k)
            deduped.append(h)
        deduped.sort(key=lambda h: -_amt(h))
        final_index[company] = deduped
    print(f"  filtered out stale-year holdings: {filtered_old:,}")

    sec_index_path = OUT_DIR / "securities_index.json"
    sec_index_path.write_text(
        json.dumps(final_index, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"  securities_index.json: {sec_index_path.stat().st_size/1024:.1f} KB")
    print(f"  companies with >= 2 holders: {len(final_index):,}")

    # 給前端用的 code→short_name 對照表 (modal 點開時找正確的 group key)
    code_map_path = OUT_DIR / "code_to_short.json"
    code_map_path.write_text(
        json.dumps(code_to_short_name, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    # 同步到第二個位置（外層 data/）給 dev server 用
    import shutil
    for secondary in OUT_DIRS[1:]:
        sec_per = secondary / "per"
        sec_per.mkdir(parents=True, exist_ok=True)
        # 清空舊檔
        for f in sec_per.glob("*.json"):
            f.unlink()
        # 複製 politicians.json + securities_index.json
        shutil.copy2(politicians_path, secondary / "politicians.json")
        if sec_index_path.exists():
            shutil.copy2(sec_index_path, secondary / "securities_index.json")
        # 複製所有 per/*.json
        for f in PER_DIR.glob("*.json"):
            shutil.copy2(f, sec_per / f.name)
        print(f"  Mirrored to: {secondary}")

    # ── Stats ─────────────
    total_per = sum(per_file_sizes)
    print(f"\n=== Done ===")
    print(f"politicians.json: {politicians_path.stat().st_size/1024:.1f} KB")
    print(f"per/ files:       {len(per_file_sizes):,}")
    print(f"per/ total size:  {total_per/1024/1024:.1f} MB")
    print(f"per/ avg size:    {total_per/len(per_file_sizes)/1024:.1f} KB")
    print(f"per/ max size:    {max(per_file_sizes)/1024:.1f} KB")


if __name__ == "__main__":
    main()
