"""FCN 機會雷達：每日資料更新腳本（由 GitHub Actions 執行）。

讀取 tickers.txt，對每個代號用 yfinance 抓：
- 近一年日線 → 收盤價、52 週高低、近 20/49 日年化歷史波動、近 50 日最大回檔、RSI(14)、50/200 日均線
- 公司資料 → 名稱、產業、市值、營收、成長率、利潤率、ROE、負債權益比、本益比、Beta、分析師目標價
- 選擇權 → 最接近 180 天到期的價平 Put 隱含波動（iv180）
- 財報日

輸出 data/quotes.json 與 data/meta.json。單一代號失敗不影響其他代號；
若本次抓不到、但舊資料存在，保留舊資料並標記 stale。
"""
from __future__ import annotations

import datetime as dt
import json
import math
import sys
import time
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
TICKERS_FILE = ROOT / "tickers.txt"
QUOTES_FILE = ROOT / "data" / "quotes.json"
META_FILE = ROOT / "data" / "meta.json"
TPE = dt.timezone(dt.timedelta(hours=8))


def read_tickers() -> list[str]:
    out = []
    for line in TICKERS_FILE.read_text(encoding="utf-8").splitlines():
        t = line.split("#", 1)[0].strip().upper()
        if t and t not in out:
            out.append(t)
    return out


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


def iv_180(tk: yf.Ticker, spot: float) -> tuple[float | None, str | None]:
    """ATM put implied vol at the listed expiry closest to 180 calendar days."""
    try:
        expiries = tk.options
    except Exception:
        return None, None
    if not expiries:
        return None, None
    today = dt.date.today()
    target = today + dt.timedelta(days=180)
    exp = min(expiries, key=lambda e: abs((dt.date.fromisoformat(e) - target).days))
    days = (dt.date.fromisoformat(exp) - today).days
    if days < 60:  # only short-dated options listed; not comparable to a 6-month FCN
        return None, exp
    try:
        puts = tk.option_chain(exp).puts
    except Exception:
        return None, exp
    puts = puts[(puts["impliedVolatility"] > 0.01) & (puts["impliedVolatility"] < 5)]
    if puts.empty:
        return None, exp
    puts = puts.assign(dist=(puts["strike"] - spot).abs()).sort_values("dist").head(2)
    return round(float(puts["impliedVolatility"].mean()) * 100, 1), exp


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


def fetch(t: str) -> dict:
    tk = yf.Ticker(t)
    hist = tk.history(period="1y", interval="1d", auto_adjust=False)
    if hist is None or hist.empty or "Close" not in hist:
        raise ValueError("找不到這個代號的價格資料，請確認是否為美股代號")
    closes_all = [float(x) for x in hist["Close"].dropna().tolist()]
    if len(closes_all) < 25:
        raise ValueError("上市時間太短，歷史資料不足")
    price = closes_all[-1]
    closes = closes_all[-50:]
    last_date = hist["Close"].dropna().index[-1].date().isoformat()

    try:
        info = tk.info or {}
    except Exception:
        info = {}
    rev = info.get("totalRevenue")
    fcf = info.get("freeCashflow")
    de = info.get("debtToEquity")  # Yahoo reports this in percent (e.g. 17.2)
    iv, iv_exp = iv_180(tk, price)

    return {
        "ticker": t,
        "name": info.get("shortName") or info.get("longName") or t,
        "industry": info.get("industry") or info.get("sector") or "",
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
        "rsi": rsi14(closes_all),
        "ma50": r(sum(closes_all[-50:]) / min(50, len(closes_all))),
        "ma200": r(sum(closes_all[-200:]) / min(200, len(closes_all))),
        "closes": [r(x) for x in closes],
        "hv20": hv(closes, 20),
        "hv49": hv(closes, len(closes) - 1),
        "maxDd50": max_drawdown(closes),
        "iv180": iv,
        "ivExpiry": iv_exp,
        "source": "Yahoo Finance (yfinance)",
        "updatedAt": dt.datetime.now(TPE).isoformat(timespec="seconds"),
        "status": "ok",
    }


def main() -> int:
    tickers = read_tickers()
    old = {}
    if QUOTES_FILE.exists():
        try:
            old = {q["ticker"]: q for q in json.loads(QUOTES_FILE.read_text(encoding="utf-8"))}
        except Exception:
            old = {}

    out, ok, failed = [], [], []
    for t in tickers:
        for attempt in range(3):
            try:
                out.append(fetch(t))
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

    dates = [q.get("priceDate") for q in out if q.get("status") == "ok" and q.get("priceDate")]
    market_date = max(set(dates), key=dates.count) if dates else None
    QUOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUOTES_FILE.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    META_FILE.write_text(json.dumps({
        "updatedAt": dt.datetime.now(TPE).isoformat(timespec="seconds"),
        "marketDate": market_date,
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
