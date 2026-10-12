"""
台股報價抓取腳本 v3 —— 在 GitHub Actions 上執行，不在使用者電腦上執行。

v3 修掉三個在 2026/09/30 – 10/05 實際踩到的資料正確性缺陷：

  【缺陷一】休市日仍寫出 history 快照（幽靈交易日）
      症狀：9/25 中秋、9/28 教師節休市，證交所 API 照樣回傳 9/24 的舊資料，
            腳本把它存成 2026-09-25.json 與 2026-09-28.json，兩檔內容一字不差。
            結果均線與 RSI 被兩筆重複收盤價汙染。
      修法：改用 API 自己回傳的成交日期欄位 d（YYYYMMDD）。只有當 API 說
            「這筆資料的成交日 == 今天」時才寫 history。休市日 d 會是上一個
            交易日，直接擋掉。
            萬一上游哪天把 d 拿掉，會自動降級為「與最新一份 history 快照比
            指紋」的備援判準，而不是從此停止寫檔 —— 失效要往安全的方向倒。

  【缺陷二】整批抓取失敗（count = 0）仍寫出 history 檔
      症狀：10/2 收盤那輪 28 檔全部抓不到，腳本寫出一個 quotes 為空陣列的
            2026-10-02.json，比沒有檔案更糟 —— 看起來像有資料。
      修法：成功筆數未達門檻（預設 80%）就不寫 history，latest.json 照寫
            （保留失敗紀錄供診斷）。

  【缺陷三】漲跌停鎖死時備援鏈退到開盤價，價格大幅失真
      症狀：9/14 大立光鎖跌停，腳本回報開盤價 6,395（-3.98%），實際是
            5,995（-9.98%），誤差 6 個百分點。
            10/2 穩懋鎖漲停 591，腳本回報開盤價 574（+6.69% vs 實際 +9.85%）。
      原因：鎖死時成交價 z 在兩筆撮合之間會短暫為空，且單邊無掛單導致
            b1/a1 也空，備援鏈一路掉到「今日開盤價」。
      修法：在退回開盤價之前，先檢查今日最高價是否等於漲停價、
            最低價是否等於跌停價。若是，就用該漲/跌停價，並把 price_source
            標成 limit_up_locked / limit_down_locked，讓下游一眼看出來。

流程：
  GitHub 伺服器 → 呼叫證交所即時報價 API → 寫成 latest.json → commit 回 repo

只用 Python 標準函式庫（urllib），不需要 pip install 任何套件。
"""

import json
import math
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

TW_TZ = timezone(timedelta(hours=8))
WATCHLIST_FILE = Path("watchlist.txt")
HIST_DIR = Path("history")

# 萬一 watchlist.txt 不見了，至少還能抓持股，不會整個掛掉
FALLBACK = [("2330", "tse"), ("0050", "tse"), ("2454", "tse")]

# history 快照的最低成功率門檻。抓到的檔數低於這個比例就不寫檔，
# 寧可當天沒有快照，也不要留下一份殘缺的歷史資料汙染技術分析。
HIST_MIN_RATIO = 0.8

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


# ── 缺陷三的輔助工具：台股升降單位與漲跌停價 ─────────────────────────
def tick_size(price):
    """台股股票的最小升降單位（元）。ETF 另有規則，此處用股票規則近似。"""
    if price < 10:
        return 0.01
    if price < 50:
        return 0.05
    if price < 100:
        return 0.1
    if price < 500:
        return 0.5
    if price < 1000:
        return 1.0
    return 5.0


def limit_price(prev_close, direction):
    """漲停價（direction=+1）或跌停價（direction=-1）。

    漲跌幅上限 10%，再依升降單位取整：漲停往下取、跌停往上取，
    使結果落在合法的報價檔位上。
    """
    if not prev_close:
        return None
    raw = prev_close * (1.1 if direction > 0 else 0.9)
    t = tick_size(raw)
    # 漲停往「下」取檔位、跌停往「上」取檔位，兩者都讓實際漲跌幅略小於 10%，
    # 不會超出法定上下限。浮點誤差用 epsilon 吸收。
    if direction > 0:
        n = math.floor((raw + 1e-9) / t)
    else:
        n = math.ceil((raw - 1e-9) / t)
    return round(n * t, 2)


def parse_item(item):
    prev_close = to_float(item.get("y"))
    open_p = to_float(item.get("o"))
    high_p = to_float(item.get("h"))
    low_p = to_float(item.get("l"))

    # 現價備援鏈：
    #   1. z   成交價（正常情況最準）
    #   2. b1/a1 最佳買賣價中值（誤差通常在一個檔位內）
    #   3. 漲跌停鎖死判定（v3 新增，擋掉最嚴重的失真來源）
    #   4. 開盤價（最後手段，一定標記出來）
    price = to_float(item.get("z"))
    source = "trade"

    if price is None:
        bid = to_float((item.get("b") or "").split("_")[0])
        ask = to_float((item.get("a") or "").split("_")[0])
        if bid and ask:
            price, source = round((bid + ask) / 2, 2), "bid_ask_mid"
        elif bid:
            # 有買無賣 → 鎖漲停，最佳買價就是漲停價
            price, source = bid, "best_bid"
        elif ask:
            # 有賣無買 → 鎖跌停
            price, source = ask, "best_ask"

    # ── 缺陷三修正 ──
    # 到這裡還是 None，表示 z / b1 / a1 全空。這幾乎只會發生在漲跌停鎖死、
    # 且剛好卡在兩筆撮合之間的瞬間。此時今日最高（最低）價就是漲（跌）停價。
    if price is None and prev_close:
        up, down = limit_price(prev_close, +1), limit_price(prev_close, -1)
        if high_p is not None and up is not None and abs(high_p - up) < 1e-6:
            price, source = up, "limit_up_locked"
        elif low_p is not None and down is not None and abs(low_p - down) < 1e-6:
            price, source = down, "limit_down_locked"

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
        "high": high_p,
        "low": low_p,
        "change": change,
        "change_pct": change_pct,
        "volume": item.get("v"),
        "quote_time": item.get("t"),
        "trade_date": item.get("d"),      # v3 新增：API 自己說的成交日 YYYYMMDD
    }


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


def should_write_history(payload, now):
    """決定今天這筆資料該不該存成 history 快照。

    三道關卡，任何一道不過就不寫 —— 缺一天快照可以事後用隔天的昨收回推，
    但寫進一筆錯的收盤價會汙染之後所有的均線與 RSI，代價高得多。
    """
    # 關卡一：時間要過收盤
    if (now.hour, now.minute) < (13, 35):
        return False, "尚未收盤"

    # 關卡二（缺陷二）：成功率要夠
    requested, count = payload["requested"], payload["count"]
    if count == 0:
        return False, "本輪抓取全數失敗，不寫出空白快照"
    if requested and count / requested < HIST_MIN_RATIO:
        return False, f"成功率僅 {count}/{requested}，低於門檻 {HIST_MIN_RATIO:.0%}"

    # 關卡三（缺陷一）：這批資料必須真的是今天的
    #
    # 首選判準：API 自己回傳的成交日欄位 d。
    # 但萬一上游哪天把 d 拿掉（或改名），這道關卡若直接回 False，
    # 就會從此再也不寫 history —— 那是比原本的 bug 更糟的退化。
    # 所以 d 不存在時改用備援判準：跟最近一份既有的 history 快照比指紋。
    today = now.strftime("%Y%m%d")
    dates = [q.get("trade_date") for q in payload["quotes"] if q.get("trade_date")]

    if dates:
        stale = [d for d in dates if d != today]
        if len(stale) > len(dates) * 0.5:
            seen = sorted(set(stale))[:3]
            return False, f"API 回傳的成交日是 {seen}，不是今天（{today}）—— 應為休市日"
        return True, "ok"

    # ── 備援判準：與最近一份 history 快照的指紋比對 ──
    print("[warn] 回傳資料沒有成交日欄位(d)，改用指紋比對判斷是否為休市日")
    same, why = _same_as_last_history(payload)
    if same:
        return False, why
    return True, "ok（指紋備援判準）"


def _fingerprint(quotes):
    """以 (代碼, 價格, 成交量) 當作一個交易日的指紋。

    休市日 API 會原封不動回傳上一個交易日的資料，連成交量都一樣，
    所以指紋相同就代表這不是新的交易日。
    """
    return tuple(sorted(
        (q.get("code"), q.get("price"), q.get("volume")) for q in quotes
    ))


def _same_as_last_history(payload):
    """跟 history/ 裡最新那一份比較。回傳 (是否相同, 原因說明)。"""
    try:
        files = sorted(HIST_DIR.glob("*.json"))
    except Exception as e:
        return False, f"無法讀取 history 目錄（{e}），保守起見照寫"
    if not files:
        return False, "history 目錄為空，這是第一筆"

    last = files[-1]
    try:
        prev = json.loads(last.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"無法解析 {last.name}（{e}），保守起見照寫"

    if not prev.get("quotes"):
        return False, f"{last.name} 沒有報價內容，不列入比對"

    if _fingerprint(prev["quotes"]) == _fingerprint(payload["quotes"]):
        return True, f"與 {last.name} 的報價完全相同（含成交量）—— 應為休市日"
    return False, "與前一份快照不同，為新的交易日"


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
        "script_version": 3,
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

    # 把需要人工留意的價格來源列出來，方便在 Actions log 裡一眼看到
    odd = [
        f"{q['code']}({q['price_source']})"
        for q in payload["quotes"]
        if q["price_source"] not in ("trade", "bid_ask_mid")
    ]
    if odd:
        print(f"[note] 非成交價計價的標的: {', '.join(odd)}")

    # 收盤後存一份當日快照，累積歷史供技術分析用 —— 但要先過三道關卡
    ok, why = should_write_history(payload, now)
    if ok:
        HIST_DIR.mkdir(exist_ok=True)
        (HIST_DIR / f"{now.strftime('%Y-%m-%d')}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("[ok] 已存收盤快照")
    else:
        print(f"[skip] 不寫 history 快照：{why}")


if __name__ == "__main__":
    main()
