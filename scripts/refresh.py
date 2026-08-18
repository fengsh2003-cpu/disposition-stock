#!/usr/bin/env python3
"""Standalone, dependency-free (stdlib only) refresh script for the
disposition-stock GitHub Pages site. Trimmed port of the `disposal` module
in the main stock-project repo's stock_cache.py -- kept in sync by hand,
since this repo intentionally does not depend on the main project (no
pandas, no positions.json, nothing personal).

Run from the repo root: python scripts/refresh.py
Writes disposal_export.json at the repo root (next to index.html) and
updates data/disposal_history.json / data/disposal.snapshot.json / the
per-stock data/<id>.csv price caches used for the price-context charts.
"""
import csv
import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
FIELDNAMES = ["date", "open", "max", "min", "close", "volume", "spread"]
EARLIEST_DATE = "2015-01-01"

TWSE_PUNISH_URL = "https://www.twse.com.tw/rwd/zh/announcement/punish"
TWSE_NOTICE_URL = "https://www.twse.com.tw/rwd/zh/announcement/notice"
TPEX_DISPOSAL_URL = "https://www.tpex.org.tw/openapi/v1/tpex_disposal_information"
TPEX_WARNING_URL = "https://www.tpex.org.tw/openapi/v1/tpex_trading_warning_information"


# ---------------------------------------------------------------------------
# Generic helpers (ported from stock_cache.py)
# ---------------------------------------------------------------------------

def load_json(path, default):
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def csv_path(stock_id):
    return DATA_DIR / f"{stock_id}.csv"


def read_cached(stock_id):
    path = csv_path(stock_id)
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as f:
        return {row["date"]: row for row in csv.DictReader(f)}


def write_cached(stock_id, rows_by_date):
    DATA_DIR.mkdir(exist_ok=True)
    path = csv_path(stock_id)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for d in sorted(rows_by_date):
            writer.writerow(rows_by_date[d])


def cache_path(stock_id, tag):
    return DATA_DIR / f"{stock_id}.{tag}.json"


def finmind_token():
    return os.environ.get("FINMIND_API_TOKEN") or os.environ.get("FINMIND_TOKEN") or ""


def finmind_request(dataset, stock_id, start_date, end_date=None, retries=3):
    params = {"dataset": dataset, "start_date": start_date}
    if stock_id:
        params["data_id"] = stock_id
    token = finmind_token()
    if token:
        params["token"] = token
    if end_date:
        params["end_date"] = end_date
    url = f"{FINMIND_URL}?{urlencode(params)}"
    label = f"{stock_id or '-'}/{dataset}"
    for attempt in range(retries + 1):
        try:
            with urlopen(url, timeout=15) as resp:
                payload = json.load(resp)
        except (URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as exc:
            if attempt >= retries:
                raise RuntimeError(f"network error for {label}: {exc}") from exc
            wait = 2 * 2 ** attempt
            print(f"{label}: network error ({exc}), retry {attempt + 1}/{retries} in {wait}s")
            time.sleep(wait)
            continue
        status = payload.get("status")
        if status == 200:
            return payload["data"]
        if status == 402 and attempt < retries:
            wait = 60
            print(f"{label}: FinMind rate limited, retry {attempt + 1}/{retries} in {wait}s")
            time.sleep(wait)
            continue
        raise RuntimeError(f"FinMind error for {label}: {payload.get('msg')}")
    raise RuntimeError(f"FinMind fetch failed for {label} after {retries} retries")


def fetch(stock_id, start_date, end_date, retries=3):
    return finmind_request("TaiwanStockPrice", stock_id, start_date, end_date, retries)


def update_stock(stock_id, start_date=None, end_date=None):
    end_date = end_date or date.today().isoformat()
    cached = read_cached(stock_id)
    if start_date is None:
        start_date = ((date.fromisoformat(max(cached.keys())) + timedelta(days=1)).isoformat()
                      if cached else EARLIEST_DATE)
    if start_date > end_date:
        return
    print(f"{stock_id}: fetching {start_date} .. {end_date}")
    try:
        rows = fetch(stock_id, start_date, end_date)
    except Exception as exc:
        print(f"{stock_id}: FAILED - {exc}")
        return
    for row in rows:
        cached[row["date"]] = {
            "date": row["date"], "open": row["open"], "max": row["max"],
            "min": row["min"], "close": row["close"],
            "volume": row["Trading_Volume"], "spread": row["spread"],
        }
    write_cached(stock_id, cached)
    print(f"{stock_id}: +{len(rows)} rows ({len(cached)} total cached)")


def ensure_price_range(stock_id, start_date, end_date):
    cached = read_cached(stock_id)
    if cached:
        dates = sorted(cached.keys())
        if dates[0] <= start_date and dates[-1] >= min(end_date, date.today().isoformat()):
            return
    update_stock(stock_id, start_date, end_date)


# ---------------------------------------------------------------------------
# 代號→名稱／產業別對照（mlouielu/twstock 靜態快照，MIT 授權，見 data/*_equities.csv）
# ---------------------------------------------------------------------------
_CODE_MAP = None


def code_map():
    global _CODE_MAP
    if _CODE_MAP is not None:
        return _CODE_MAP
    m = {}
    for fn in ("twse_equities.csv", "tpex_equities.csv"):
        path = DATA_DIR / fn
        if not path.exists():
            continue
        with path.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                code = (r.get("code") or "").strip()
                if code and code not in m:
                    m[code] = {
                        "name": (r.get("name") or "").strip(),
                        "group": (r.get("group") or "").strip(),
                        "market": (r.get("market") or "").strip(),
                    }
    _CODE_MAP = m
    return m


def stock_label(stock_id, with_group=True):
    info = code_map().get(stock_id)
    if not info or not info["name"]:
        return stock_id
    if with_group and info["group"]:
        return f"{stock_id} {info['name']}/{info['group']}"
    return f"{stock_id} {info['name']}"


# ---------------------------------------------------------------------------
# 處置股／注意股票（ported from stock_cache.py's disposal module 2026-08-16;
# see that file's comments for the full rationale / API quirks writeup）
# ---------------------------------------------------------------------------

def fetch_json_url(url, params=None, retries=3, label=None):
    full_url = url + ("?" + urlencode(params) if params else "")
    label = label or url
    for attempt in range(retries + 1):
        try:
            req = Request(full_url, headers={"User-Agent": "Mozilla/5.0 (disposition-stock)"})
            with urlopen(req, timeout=20) as resp:
                text = resp.read().decode("utf-8")
        except (URLError, TimeoutError, ConnectionError) as exc:
            if attempt >= retries:
                raise RuntimeError(f"{label} 抓取失敗: {exc}") from exc
            wait = 2 * 2 ** attempt
            print(f"{label}: 網路錯誤（{exc}），{wait}秒後重試 {attempt + 1}/{retries}")
            time.sleep(wait)
            continue
        return json.loads(text)
    raise RuntimeError(f"{label} 抓取失敗（已重試 {retries} 次）")


def _roc_to_iso(s):
    digits = "".join(ch for ch in str(s) if ch.isdigit())
    if len(digits) != 7:
        return str(s)
    return f"{int(digits[:3]) + 1911}-{digits[3:5]}-{digits[5:7]}"


def _parse_period(s):
    digits = "".join(ch for ch in str(s) if ch.isdigit())
    if len(digits) != 14:
        return None, None
    return _roc_to_iso(digits[:7]), _roc_to_iso(digits[7:])


def fetch_twse_disposal(start_iso, end_iso, stock_id=None):
    params = {"startDate": start_iso.replace("-", ""), "endDate": end_iso.replace("-", ""),
              "response": "json"}
    if stock_id:
        params["stockNo"] = stock_id
    data = fetch_json_url(TWSE_PUNISH_URL, params, label="TWSE/處置(punish)")
    fields = data.get("fields", [])
    out = []
    for row in data.get("data", []):
        rec = dict(zip(fields, row))
        period_start, period_end = _parse_period(rec.get("處置起迄時間", ""))
        out.append({
            "market": "TWSE", "type": "disposal",
            "stock_id": str(rec.get("證券代號", "")).strip(),
            "name": str(rec.get("證券名稱", "")).strip(),
            "announce_date": _roc_to_iso(rec.get("公布日期", "")),
            "period_start": period_start, "period_end": period_end,
            "measure": str(rec.get("處置措施", "")).strip(),
            "reason": str(rec.get("處置條件", "")).strip(),
        })
    if stock_id:
        out = [r for r in out if r["stock_id"] == stock_id]
    return out


def fetch_twse_notice(start_iso, end_iso, stock_id=None):
    params = {"startDate": start_iso.replace("-", ""), "endDate": end_iso.replace("-", ""),
              "response": "json"}
    if stock_id:
        params["stockNo"] = stock_id
    data = fetch_json_url(TWSE_NOTICE_URL, params, label="TWSE/注意(notice)")
    fields = data.get("fields", [])
    out = []
    for row in data.get("data", []):
        rec = dict(zip(fields, row))
        out.append({
            "market": "TWSE", "type": "attention",
            "stock_id": str(rec.get("證券代號", "")).strip(),
            "name": str(rec.get("證券名稱", "")).strip(),
            "announce_date": _roc_to_iso(rec.get("日期", "")),
            "count": rec.get("累計次數"),
            "reason": str(rec.get("注意交易資訊", "")).strip(),
            "close": rec.get("收盤價"),
        })
    if stock_id:
        out = [r for r in out if r["stock_id"] == stock_id]
    return out


def fetch_tpex_disposal():
    rows = fetch_json_url(TPEX_DISPOSAL_URL, label="TPEx/處置")
    out = []
    for rec in rows:
        period_start, period_end = _parse_period(rec.get("DispositionPeriod", ""))
        out.append({
            "market": "TPEx", "type": "disposal",
            "stock_id": str(rec.get("SecuritiesCompanyCode", "")).strip(),
            "name": str(rec.get("CompanyName", "")).strip(),
            "announce_date": _roc_to_iso(rec.get("Date", "")),
            "period_start": period_start, "period_end": period_end,
            "measure": "", "reason": str(rec.get("DispositionReasons", "")).strip(),
        })
    return out


def fetch_tpex_warning():
    rows = fetch_json_url(TPEX_WARNING_URL, label="TPEx/注意")
    out = []
    for rec in rows:
        out.append({
            "market": "TPEx", "type": "attention",
            "stock_id": str(rec.get("SecuritiesCompanyCode", "")).strip(),
            "name": str(rec.get("CompanyName", "")).strip(),
            "announce_date": _roc_to_iso(rec.get("Date", "")),
            "count": None, "reason": str(rec.get("TradingInformation", "")).strip(),
            "close": rec.get("ClosePrice"),
        })
    return out


def _record_key(rec):
    return "|".join([rec.get("market", ""), rec.get("type", ""), rec.get("stock_id", ""),
                      rec.get("announce_date", "") or "", rec.get("period_start") or ""])


def cached_disposal_snapshot(lookback_days=90):
    today_iso = date.today().isoformat()
    hist_path = DATA_DIR / "disposal_history.json"
    end_iso = today_iso
    start_iso = (date.today() - timedelta(days=lookback_days)).isoformat()
    records = []
    try:
        records += fetch_twse_disposal(start_iso, end_iso)
        records += fetch_twse_notice(start_iso, end_iso)
    except Exception as exc:
        print(f"TWSE 處置/注意抓取失敗，本次結果不含上市資料 - {exc}")
    try:
        records += fetch_tpex_disposal()
        records += fetch_tpex_warning()
    except Exception as exc:
        print(f"TPEx 處置/注意抓取失敗，本次結果不含上櫃資料 - {exc}")
    if not records:
        raise RuntimeError("處置/注意資料抓取全部失敗")
    history = load_json(hist_path, {"records": {}})
    for rec in records:
        history["records"][_record_key(rec)] = rec
    DATA_DIR.mkdir(exist_ok=True)
    hist_path.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
    return records


def disposal_history_all():
    history = load_json(DATA_DIR / "disposal_history.json", {"records": {}})
    return list(history["records"].values())


def disposal_price_context(stock_id, period_start, period_end, before_days=8, after_days=8):
    try:
        start_buf = (date.fromisoformat(period_start) - timedelta(days=before_days * 2 + 5)).isoformat()
        end_buf = (date.fromisoformat(period_end) + timedelta(days=after_days * 2 + 5)).isoformat()
        end_buf = min(end_buf, date.today().isoformat())
        ensure_price_range(stock_id, start_buf, end_buf)
    except Exception as exc:
        return {"error": f"價格資料抓取失敗: {exc}"}
    cached = read_cached(stock_id)
    if not cached:
        return {"error": "無日K資料（可能是權證/TDR等FinMind未收錄的代號）"}
    dates = sorted(cached.keys())
    idx_start = next((i for i, d in enumerate(dates) if d >= period_start), None)
    if idx_start is None:
        return {"error": "資料不足以涵蓋處置起始日"}
    idx_end = next((i for i, d in enumerate(dates) if d >= period_end), len(dates) - 1)
    lo, hi = max(0, idx_start - before_days), min(len(dates) - 1, idx_end + after_days)
    series = []
    for d in dates[lo:hi + 1]:
        r = cached[d]
        try:
            close = float(r["close"])
            if close <= 0:
                continue
            series.append({
                "date": d, "open": float(r["open"]), "high": float(r["max"]),
                "low": float(r["min"]), "close": close,
                "in_disposal": period_start <= d <= period_end,
            })
        except (TypeError, ValueError):
            continue
    if not series:
        return {"error": "區間內無有效日K資料"}
    return {"series": series, "period_start": period_start, "period_end": period_end}


def build_export(records, today_iso):
    active_disposal = [r for r in records if r["type"] == "disposal" and r.get("period_end")
                        and r["period_end"] >= today_iso]
    release_schedule = {}
    for r in sorted(active_disposal, key=lambda x: (x["period_end"], x["stock_id"])):
        release_schedule.setdefault(r["period_end"], []).append({
            "stock_id": r["stock_id"], "name": r["name"], "market": r["market"],
            "period_start": r["period_start"], "period_end": r["period_end"],
        })
    today_new_disposal = [r for r in records if r["type"] == "disposal" and r.get("period_start") == today_iso]
    today_new_attention = [r for r in records if r["type"] == "attention" and r.get("announce_date") == today_iso]
    recent_cutoff = (date.fromisoformat(today_iso) - timedelta(days=7)).isoformat()
    active_ids = set()
    for r in records:
        if r["type"] == "disposal" and r.get("period_start") and r.get("period_end") \
                and r["period_start"] <= today_iso <= r["period_end"]:
            active_ids.add(r["stock_id"])
        elif r["type"] == "attention" and (r.get("announce_date") or "") >= recent_cutoff:
            active_ids.add(r["stock_id"])
    name_from_records = {r["stock_id"]: r["name"] for r in records if r.get("name")}
    groups = {}
    for sid in active_ids:
        info = code_map().get(sid, {})
        g = info.get("group") or "未知"
        name = name_from_records.get(sid) or info.get("name") or sid
        groups.setdefault(g, []).append({"stock_id": sid, "name": name})

    all_records = list({_record_key(r): r for r in records + disposal_history_all()}.values())
    for r in all_records:
        r["label"] = stock_label(r["stock_id"], with_group=False)

    price_context = {}
    active_disposal_by_id = {r["stock_id"]: r for r in active_disposal}
    for sid, r in active_disposal_by_id.items():
        ctx = disposal_price_context(sid, r["period_start"], r["period_end"])
        if "error" not in ctx:
            price_context[sid] = ctx
        time.sleep(0.1)

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of": today_iso,
        "source": "TWSE/TPEx 官方公開 API，非即時（依 GitHub Actions 排程執行時間為準）",
        "today_new_disposal": today_new_disposal,
        "today_new_attention": today_new_attention,
        "release_schedule": release_schedule,
        "groups": {g: v for g, v in groups.items() if len(v) >= 2},
        "all_records": all_records,
        "price_context": price_context,
        "disclaimer": "本資料為公開制度資訊之機械化彙整，非即時報價，非選股建議，僅供研究參考。"
                       "不含官方注意/處置認定標準的預測（不做「明日是否會被處置」的預測）。"
                       "上櫃(TPEx)歷史僅涵蓋本工具啟用後累積的資料，非完整歷史。"
                       "price_context 的報酬為原始價差，未扣手續費/證交稅/滑價，非投資建議。",
    }


def main():
    today_iso = date.today().isoformat()
    records = cached_disposal_snapshot()
    export = build_export(records, today_iso)
    out_path = ROOT / "disposal_export.json"
    out_path.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[已輸出] {out_path}")


if __name__ == "__main__":
    main()
