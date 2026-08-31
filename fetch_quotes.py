"""
台股報價抓取腳本 v2 —— 在 GitHub Actions 上執行，不在使用者電腦上執行。

v2 改動：觀察清單改從 watchlist.txt 讀取，不再寫死在程式裡。
        要增減標的只需要改那個文字檔，不會動到程式邏輯。

流程：
  GitHub 伺服器 → 呼叫證交所即時報價 API → 寫成 latest.json → commit 回 repo

只用 Python 標準函式庫（urllib），不需要 pip install 任何套件。
"""

import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

TW_TZ = timezone(timedelta(hours=8))
WATCHLIST_FILE = Path("watchlist.txt")

# 萬一 watchlist.txt 不見了，至少還能抓持股，不會整個掛掉
FALLBACK = [("2330", "tse"), ("0050", "tse"), ("2454", "tse")]

MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://mis.twse.com.tw/stock/index.jsp",
}


def load_watchlist():
    """讀取 watchlist.txt，回傳 [(代碼, 市場), ...]。

    容錯設計：忽略空行、註解、行尾註解、多餘空白；
    重複的代碼只留第一次出現的，順序保持檔案裡的順序。
    """
    if not WATCHLIST_FILE.exists():
        print("[warn] 找不到 watchlist.txt，改用內建預設清單")
        return FALLBACK

    pairs, seen = [], set()
    for lineno, raw in enumerate(WATCHLIST_FILE.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()      # 砍掉註解
        if not line:
            continue
        code, _, market = line.partition(":")
        code, market = code.strip(), (market.strip().lower() or "tse")
        if not code.replace(".", "").isalnum():
            print(f"[warn] 第 {lineno} 行格式怪怪的，跳過: {raw.strip()!r}")
            continue
        if market not in ("tse", "otc"):
            print(f"[warn] 第 {lineno} 行市場只能是 tse/otc，當成 tse 處理: {raw.strip()!r}")
            market = "tse"
        if code in seen:
            print(f"[info] 第 {lineno} 行 {code} 重複，略過")
            continue
        seen.add(code)
        pairs.append((code, market))

    if not pairs:
        print("[warn] watchlist.txt 沒有任何有效代碼，改用內建預設清單")
        return FALLBACK
    print(f"[info] 從 watchlist.txt 讀入 {len(pairs)} 檔")
    return pairs


def to_float(v):
    """證交所在沒有成交時會回傳 '-'，要當成 None 而不是報錯。"""
    try:
        if v in (None, "-", "", "0.00"):
            return None
        return float(v)
    except (ValueError, TypeError):
        return None


def call_mis(pairs):
    """pairs 是 [(code, market), ...]，回傳 msgArray。"""
    if not pairs:
        return []
    ex_ch = "|".join(f"{market}_{code}.tw" for code, market in pairs)
    url = f"{MIS_URL}?ex_ch={ex_ch}&_={int(time.time() * 1000)}"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8")).get("msgArray", [])
    except Exception as e:
        print(f"[warn] 呼叫失敗: {e}")
        return []


def parse_item(item):
    prev_close = to_float(item.get("y"))
    open_p = to_float(item.get("o"))

    # 現價備援：成交價 → 最佳買賣價中值 → 開盤價
    price = to_float(item.get("z"))
    source = "trade"
    if price is None:
        bid = to_float((item.get("b") or "").split("_")[0])
        ask = to_float((item.get("a") or "").split("_")[0])
        if bid and ask:
            price, source = round((bid + ask) / 2, 2), "bid_ask_mid"
        elif bid:
            price, source = bid, "best_bid"
        elif ask:
            price, source = ask, "best_ask"
    if price is None and open_p is not None:
        price, source = open_p, "open_fallback"

    change = change_pct = None
    if price is not None and prev_close:
        change = round(price - prev_close, 2)
        change_pct = round(change / prev_close * 100, 2)

    return {
        "code": item.get("c"),
        "name": item.get("n"),
        "price": price,
        "price_source": source,
        "prev_close": prev_close,
        "open": open_p,
        "high": to_float(item.get("h")),
        "low": to_float(item.get("l")),
        "change": change,
        "change_pct": change_pct,
        "volume": item.get("v"),
        "quote_time": item.get("t"),
    }


def main():
    wanted = load_watchlist()

    # 檔數多的時候分批送，避免單一請求太長被上游拒絕
    results = {}
    BATCH = 25
    for i in range(0, len(wanted), BATCH):
        for item in call_mis(wanted[i:i + BATCH]):
            parsed = parse_item(item)
            if parsed["code"]:
                results[parsed["code"]] = parsed
        if i + BATCH < len(wanted):
            time.sleep(1)   # 對上游客氣一點

    # 自我修正：抓不到的，用另一個市場前綴重試一次
    missing = [(c, "otc" if m == "tse" else "tse") for c, m in wanted if c not in results]
    if missing:
        print(f"[info] 用另一市場前綴重試: {[c for c, _ in missing]}")
        for item in call_mis(missing):
            parsed = parse_item(item)
            if parsed["code"]:
                results[parsed["code"]] = parsed

    now = datetime.now(TW_TZ)
    still_missing = [c for c, _ in wanted if c not in results]
    payload = {
        "queried_at": now.isoformat(),
        "queried_at_tw": now.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "TWSE MIS realtime endpoint",
        "requested": len(wanted),
        "count": len(results),
        "missing": still_missing,
        "quotes": [results[c] for c, _ in wanted if c in results],
    }

    Path("latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[ok] 寫入 {len(results)}/{len(wanted)} 檔 @ {payload['queried_at_tw']}")
    if still_missing:
        print(f"[warn] 這幾檔查無資料，代碼可能有誤或已停止交易: {still_missing}")

    # 收盤後（13:35 以後）順手存一份當日快照，累積歷史供技術分析用
    if (now.hour, now.minute) >= (13, 35):
        hist_dir = Path("history")
        hist_dir.mkdir(exist_ok=True)
        (hist_dir / f"{now.strftime('%Y-%m-%d')}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("[ok] 已存收盤快照")


if __name__ == "__main__":
    main()
