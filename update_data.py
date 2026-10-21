"""FCN 機會雷達：每日資料更新腳本（由 GitHub Actions 執行）。

讀取 tickers.txt（美股代號，或日股 4 碼加 .T），對每個代號用 yfinance 抓：
- 近一年日線 → 收盤價、52 週高低、近 20/49/120 日年化歷史波動、近 50 日最大回檔、RSI(14)、50/200 日均線
- 公司資料 → 名稱、產業、市值、營收、成長率、利潤率、ROE、負債權益比、本益比、Beta、分析師目標價
- 選擇權 → 最接近 180 天到期的價平 Put 隱含波動（iv180）
- 財報日

輸出 data/quotes.json 與 data/meta.json。單一代號失敗不影響其他代號；
若本次抓不到、但舊資料存在，保留舊資料並標記 stale。

環境變數 MARKETS=US 或 MARKETS=JP 時只更新該市場，其餘沿用上一次資料
（手動測試用；GitHub Actions 每天台北清晨兩個市場一起更新）。
今天盤中（交易所尚未收盤）的日線會被略過，只採用已收盤的交易日。
"""
from __future__ import annotations

import os

import datetime as dt
import json
import math
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
TICKERS_FILE = ROOT / "tickers.txt"
QUOTES_FILE = ROOT / "data" / "quotes.json"
META_FILE = ROOT / "data" / "meta.json"
TPE = dt.timezone(dt.timedelta(hours=8))
# Below this an implied vol is a bad quote (Yahoo zeroes bid/ask between midnight ET and the open).
MIN_IV = 0.05


def iv_window_open() -> bool:
    """Yahoo option IVs are only reliable on weekday evenings in New York (after the close, before
    midnight). Overnight bid/ask are zeroed, and right after the open quotes give IVs 2-3x too high."""
    ny = dt.datetime.now(ZoneInfo("America/New_York"))
    return ny.weekday() < 5 and dt.time(16, 30) <= ny.time()
# Exchange time zone and local close time; a daily bar for today before this is still intraday.
SESSIONS = {"US": (ZoneInfo("America/New_York"), dt.time(16, 15)), "JP": (ZoneInfo("Asia/Tokyo"), dt.time(15, 45))}


def norm(t: str) -> str:
    """'8035', '8035 JT', '8035.T' -> '8035.T'; US tickers unchanged."""
    t = t.strip().upper().replace(" JT", "").replace(" JP", "")
    if t.endswith(".T"):
        return t
    if len(t) == 4 and t[0].isdigit():
        return t + ".T"
    return t


def market_of(t: str) -> str:
    return "JP" if t.endswith(".T") else "US"


def read_tickers() -> tuple[list[str], dict[str, str]]:
    """Return tickers and {ticker: label}; text after # on a ticker line is its Chinese label."""
    out, labels = [], {}
    for line in TICKERS_FILE.read_text(encoding="utf-8").splitlines():
        code, _, comment = line.partition("#")
        if not code.strip():
            continue
        t = norm(code)
        if t not in out:
            out.append(t)
            if comment.strip():
                labels[t] = comment.strip()
    return out, labels


def r(x, n=2):
    """Round floats, turn NaN/inf/None into None."""
    try:
        if x is None:
            return None
        x = float(x)
        if math.isnan(x) or math.isinf(x):
            return None
        return round(x, n)
    except (TypeError, ValueError):
        return None


def pct(x, n=2):
    v = r(x, 6)
    return None if v is None else round(v * 100, n)


def big(x):
    v = r(x, 0)
    if v is None:
        return None
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
        if abs(v) >= div:
            return f"{v / div:.2f}{unit}"
    return f"{v:.0f}"


def hv(closes: list[float], n: int) -> float | None:
    if len(closes) < n + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - n, len(closes))]
    m = sum(rets) / len(rets)
    var = sum((x - m) ** 2 for x in rets) / (len(rets) - 1)
    return round(math.sqrt(var * 252) * 100, 1)


def rsi14(closes: list[float]) -> float | None:
    if len(closes) < 15:
        return None
    s = pd.Series(closes)
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    last_dn = dn.iloc[-1]
    if last_dn == 0:
        return 100.0
    return round(100 - 100 / (1 + up.iloc[-1] / last_dn), 2)


def max_drawdown(closes: list[float]) -> float | None:
    if not closes:
        return None
    peak, mdd = closes[0], 0.0
    for x in closes:
        peak = max(peak, x)
        mdd = min(mdd, x / peak - 1)
    return round(mdd * 100, 1)


def atm_put_iv(tk: yf.Ticker, exp: str, spot: float) -> float | None:
    """Mean implied vol (decimal) of the two put strikes closest to spot."""
    try:
        puts = tk.option_chain(exp).puts
    except Exception:
        return None
    puts = puts[(puts["bid"] > 0) & (puts["ask"] > 0)
                & (puts["impliedVolatility"] >= MIN_IV) & (puts["impliedVolatility"] < 5)]
    if puts.empty:
        return None
    puts = puts.assign(dist=(puts["strike"] - spot).abs()).sort_values("dist").head(2)
    return float(puts["impliedVolatility"].mean())


def iv_180(tk: yf.Ticker, spot: float) -> tuple[float | None, str | None]:
    """Constant-maturity 180-day ATM put implied vol.

    Interpolates total variance (sigma^2 * T) between the listed expiries just
    before and just after 180 calendar days; if only one side exists, uses that
    expiry as is. Returns (iv %, description of the expiries used).
    """
    try:
        expiries = tk.options
    except Exception:
        return None, None
    if not expiries:
        return None, None
    today = dt.date.today()
    days = {e: (dt.date.fromisoformat(e) - today).days for e in expiries}
    before = [e for e in expiries if 60 <= days[e] <= 180]  # <60 days is not comparable to a 6-month FCN
    after = [e for e in expiries if days[e] >= 180]
    lo = max(before, key=days.get) if before else None
    hi = min(after, key=days.get) if after else None
    if lo == hi:
        hi = None
    iv_lo = atm_put_iv(tk, lo, spot) if lo else None
    iv_hi = atm_put_iv(tk, hi, spot) if hi else None
    if iv_lo is not None and iv_hi is not None:
        t1, t2 = days[lo], days[hi]
        w = iv_lo ** 2 * t1 + (iv_hi ** 2 * t2 - iv_lo ** 2 * t1) * (180 - t1) / (t2 - t1)
        if w > 0:
            return round(math.sqrt(w / 180) * 100, 1), f"{t1}/{t2} 天內插"
    for e, iv in ((hi, iv_hi), (lo, iv_lo)):
        if iv is not None:
            return round(iv * 100, 1), e
    return None, hi or lo


def earnings_date(tk: yf.Ticker) -> str | None:
    today = dt.date.today()
    try:
        cal = tk.calendar
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if dates:
            future = [d for d in dates if isinstance(d, dt.date) and d >= today]
            if future:
                return min(future).isoformat()
    except Exception:
        pass
    return None


def eps_beats(tk: yf.Ticker) -> tuple[int | None, int | None]:
    """(quarters where reported EPS >= estimate, quarters with data) over the last 4 quarters."""
    try:
        h = tk.earnings_history
        h = h.dropna(subset=["epsActual", "epsEstimate"]).sort_index().tail(4)
    except Exception:
        return None, None
    if h.empty:
        return None, None
    return int((h["epsActual"] >= h["epsEstimate"]).sum()), len(h)


def last_earnings(info: dict, tz) -> str | None:
    """Most recent earnings report date if Yahoo's earningsTimestamp is in the past."""
    ts = info.get("earningsTimestamp")
    if not isinstance(ts, (int, float)):
        return None
    d = dt.datetime.fromtimestamp(ts, tz)
    return d.date().isoformat() if d <= dt.datetime.now(tz) else None


def bollinger_pct_b(closes: list[float], n: int = 20) -> float | None:
    if len(closes) < n:
        return None
    w = closes[-n:]
    m = sum(w) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in w) / n)
    return round((w[-1] - (m - 2 * sd)) / (4 * sd), 2) if sd else None


def ma_slope(closes: list[float], n: int = 50, lag: int = 20) -> float | None:
    """% change of the n-day moving average over the last `lag` trading days."""
    if len(closes) < n + lag:
        return None
    now = sum(closes[-n:]) / n
    before = sum(closes[-n - lag:-lag]) / n
    return round((now / before - 1) * 100, 2)


def fetch(t: str, label: str | None = None) -> dict:
    tk = yf.Ticker(t)
    mk = market_of(t)
    hist = tk.history(period="1y", interval="1d", auto_adjust=False)
    if hist is None or hist.empty or "Close" not in hist:
        raise ValueError("找不到這個代號的價格資料，" + ("請確認是否為東證代號（4 碼加 .T）" if mk == "JP" else "請確認是否為美股代號"))
    hist = hist.dropna(subset=["Close"])
    tz, close_at = SESSIONS[mk]
    now_local = dt.datetime.now(tz)
    if len(hist) and hist.index[-1].date() == now_local.date() and now_local.time() < close_at:
        hist = hist.iloc[:-1]  # market still open: keep only completed sessions
    try:
        info = tk.info or {}
    except Exception:
        info = {}
    closes_all = [float(x) for x in hist["Close"].dropna().tolist()]
    last_day = hist.index[-1].date() if len(hist) else None
    # Yahoo sometimes publishes the daily bar hours after the close (US bars can be missing until
    # ~21:00-23:00 ET); if the quote shows a completed session newer than the last bar, use its price.
    q_time, q_price = info.get("regularMarketTime"), r(info.get("regularMarketPrice"))
    if isinstance(q_time, (int, float)) and q_price:
        q_at = dt.datetime.fromtimestamp(q_time, tz)
        session_done = now_local.date() > q_at.date() or now_local.time() >= close_at
        if last_day and q_at.date() > last_day and q_at.date() <= now_local.date() and session_done and q_at.weekday() < 5:
            closes_all.append(q_price)
            last_day = q_at.date()
    if len(closes_all) < 25:
        raise ValueError("上市時間太短，歷史資料不足")
    price = closes_all[-1]
    closes = closes_all[-50:]
    last_date = last_day.isoformat()

    rev = info.get("totalRevenue")
    fcf = info.get("freeCashflow")
    de = info.get("debtToEquity")  # Yahoo reports this in percent (e.g. 17.2)
    iv, iv_exp = iv_180(tk, price) if mk == "US" and iv_window_open() else (None, None)  # Yahoo has no Japanese option chains
    is_etf = (info.get("quoteType") or "").upper() in ("ETF", "MUTUALFUND")

    q = {
        "ticker": t,
        "market": mk,
        "currency": info.get("currency") or ("JPY" if mk == "JP" else "USD"),
        "type": "ETF" if is_etf else "stock",
        "name": info.get("shortName") or info.get("longName") or t,
        "industry": info.get("industry") or info.get("sector") or (info.get("category") or ("ETF" if is_etf else "")),
        "price": r(price),
        "priceDate": last_date,
        "low52": r(min(closes_all[-252:])),
        "high52": r(max(closes_all[-252:])),
        "marketCap": big(info.get("marketCap")),
        "revenueTtm": big(rev),
        "revGrowth": pct(info.get("revenueGrowth"), 1),
        "pe": r(info.get("trailingPE")),
        "forwardPe": r(info.get("forwardPE")),
        "beta": r(info.get("beta")),
        "earningsDate": earnings_date(tk),
        "rating": (info.get("recommendationKey") or "").replace("_", " ").title() or None,
        "target": r(info.get("targetMeanPrice")),
        "grossMargin": pct(info.get("grossMargins")),
        "opMargin": pct(info.get("operatingMargins")),
        "fcfMargin": round(fcf / rev * 100, 2) if fcf and rev else None,
        "roe": pct(info.get("returnOnEquity")),
        "debtEquity": round(de / 100, 2) if isinstance(de, (int, float)) else None,
        "forwardEps": r(info.get("forwardEps")),
        "trailingEps": r(info.get("trailingEps")),
        "peg": r(info.get("pegRatio") or info.get("trailingPegRatio")),
        "totalCash": r(info.get("totalCash"), 0),
        "totalDebt": r(info.get("totalDebt"), 0),
        "ebitda": r(info.get("ebitda"), 0),
        "shortPctFloat": pct(info.get("shortPercentOfFloat"), 1),
        "lastEarnings": last_earnings(info, tz),
        "rsi": rsi14(closes_all),
        "pctB": bollinger_pct_b(closes_all),
        "ma50Slope": ma_slope(closes_all),
        "ma50": r(sum(closes_all[-50:]) / min(50, len(closes_all))),
        "ma200": r(sum(closes_all[-200:]) / min(200, len(closes_all))),
        "closes": [r(x) for x in closes],
        "hv20": hv(closes, 20),
        "hv49": hv(closes, len(closes) - 1),
        "hv120": hv(closes_all, 120),  # ~6 months, matches typical FCN tenor
        "maxDd50": max_drawdown(closes),
        "iv180": iv,
        "ivExpiry": iv_exp,
        "ivDate": last_date if iv is not None else None,
        "ivOk": True if iv is not None else None,  # fetched inside the reliable window
        "source": "Yahoo Finance (yfinance)",
        "updatedAt": dt.datetime.now(TPE).isoformat(timespec="seconds"),
        "status": "ok",
    }
    q["epsBeats"], q["epsQuarters"] = eps_beats(tk) if not is_etf else (None, None)
    if mk == "JP":
        q["code"] = t[:-2]
    if label:
        q["zhName"] = label
    return q


def main() -> int:
    tickers, labels = read_tickers()
    only = {m.strip().upper() for m in os.environ.get("MARKETS", "").split(",") if m.strip()}
    old = {}
    if QUOTES_FILE.exists():
        try:
            old = {q["ticker"]: q for q in json.loads(QUOTES_FILE.read_text(encoding="utf-8"))}
        except Exception:
            old = {}

    out, ok, failed = [], [], []
    for t in tickers:
        if only and market_of(t) not in only and t in old:
            q = dict(old[t])
            if labels.get(t):
                q["zhName"] = labels[t]
            out.append(q)  # other market: keep last data
            continue
        for attempt in range(3):
            try:
                q = fetch(t, labels.get(t))
                prev = old.get(t, {})
                if q["market"] == "US" and q["iv180"] is None and prev.get("iv180") and prev.get("ivOk"):
                    # no usable option quotes this run: keep the last valid IV
                    q.update(iv180=prev["iv180"], ivExpiry=prev.get("ivExpiry"), ivDate=prev.get("ivDate"), ivOk=True)
                out.append(q)
                ok.append(t)
                break
            except Exception as e:  # noqa: BLE001
                if attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    continue
                msg = str(e)[:120]
                if t in old and old[t].get("status") in ("ok", "stale"):
                    q = dict(old[t])
                    q["status"] = "stale"
                    q["error"] = "本次更新失敗，顯示上一次資料"
                    out.append(q)
                else:
                    out.append({"ticker": t, "status": "error", "error": msg,
                                "updatedAt": dt.datetime.now(TPE).isoformat(timespec="seconds")})
                failed.append(f"{t}: {msg}")
        time.sleep(0.5)

    def mode_date(mk):
        ds = [q.get("priceDate") for q in out if q.get("status") == "ok" and q.get("priceDate") and market_of(q["ticker"]) == mk]
        return max(set(ds), key=ds.count) if ds else None
    old_meta = {}
    if META_FILE.exists():
        try:
            old_meta = json.loads(META_FILE.read_text(encoding="utf-8"))
        except Exception:
            old_meta = {}
    now = dt.datetime.now(TPE).isoformat(timespec="seconds")
    QUOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUOTES_FILE.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    META_FILE.write_text(json.dumps({
        "updatedAt": now if (not only or "US" in only) else old_meta.get("updatedAt", now),
        "updatedAtJP": now if (not only or "JP" in only) else old_meta.get("updatedAtJP"),
        "marketDate": mode_date("US"),
        "marketDateJP": mode_date("JP"),
        "source": "Yahoo Finance (yfinance)",
        "ok": len(ok),
        "failed": failed,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"updated {len(ok)}/{len(tickers)}")
    for f in failed:
        print("FAILED", f)
    # Fail the workflow only if nothing at all came back (likely a network/Yahoo outage).
    return 0 if ok or not tickers else 1


if __name__ == "__main__":
    sys.exit(main())
