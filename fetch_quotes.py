"""
台股報價抓取腳本 —— 在 GitHub Actions 上執行，不在使用者電腦上執行。

流程：
  GitHub 伺服器 → 呼叫證交所即時報價 API → 寫成 latest.json → commit 回 repo

只用 Python 標準函式庫（urllib），不需要 pip install 任何套件，
讓 workflow 跑得更快、也更不容易壞。
"""

import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

TW_TZ = timezone(timedelta(hours=8))

# ── 觀察清單 ──────────────────────────────────────────────
# 格式："代碼:市場"，市場 tse=上市、otc=上櫃
# 若某檔抓不到資料，腳本會自動用另一個市場前綴重試一次，所以填錯也會自我修正。
WATCHLIST = [
    # 持股
    "2330:tse",   # 台積電
    "0050:tse",   # 元大台灣50
    "2454:tse",   # 聯發科
    # 大型觀察
    "2308:tse",   # 台達電
    "3711:tse",   # 日月光投控
    "2383:tse",   # 台光電
    "2345:tse",   # 智邦
    "3017:tse",   # 奇鋐
    "2303:tse",   # 聯電
    "2408:tse",   # 南亞科
    "2327:tse",   # 國巨
    # 低價/轉機觀察
    "1802:tse",   # 台玻
    "1597:tse",   # 直得
    "2365:tse",   # 昆盈
    "2375:tse",   # 凱美
    "6715:otc",   # 嘉基
    "6788:otc",   # 華景電
    "6182:otc",   # 合晶
    "6173:otc",   # 信昌電
]

MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://mis.twse.com.tw/stock/index.jsp",
}


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
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        print(f"[warn] 呼叫失敗 {ex_ch}: {e}")
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
    wanted = []
    for entry in WATCHLIST:
        code, _, market = entry.partition(":")
        wanted.append((code, market or "tse"))

    results = {}
    for item in call_mis(wanted):
        parsed = parse_item(item)
        if parsed["code"]:
            results[parsed["code"]] = parsed

    # 自我修正：抓不到的，用另一個市場前綴重試一次
    missing = [(c, "otc" if m == "tse" else "tse") for c, m in wanted if c not in results]
    if missing:
        print(f"[info] 用另一市場前綴重試: {[c for c, _ in missing]}")
        for item in call_mis(missing):
            parsed = parse_item(item)
            if parsed["code"]:
                results[parsed["code"]] = parsed

    now = datetime.now(TW_TZ)
    payload = {
        "queried_at": now.isoformat(),
        "queried_at_tw": now.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "TWSE MIS realtime endpoint",
        "count": len(results),
        "missing": [c for c, _ in wanted if c not in results],
        "quotes": [results[c] for c, _ in wanted if c in results],
    }

    Path("latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[ok] 寫入 {len(results)} 檔報價 @ {payload['queried_at_tw']}")

    # 收盤後（13:30 以後）順手存一份當日快照，累積歷史供技術分析用
    if now.hour >= 13 and now.minute >= 35:
        hist_dir = Path("history")
        hist_dir.mkdir(exist_ok=True)
        (hist_dir / f"{now.strftime('%Y-%m-%d')}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("[ok] 已存收盤快照")


if __name__ == "__main__":
    main()
