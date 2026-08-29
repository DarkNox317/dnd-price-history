# -*- coding: utf-8 -*-
"""Dark and Darker daily price collector.

For every item in catalog.json, queries DarkerDB sold listings for one UTC day
and stores per-item [median, min, max, count] into data/YYYY-MM.json.
Items with zero sales that day are omitted to keep files small.

Usage:
  DARKERDB_API_KEY=... python collect.py [--date YYYY-MM-DD] [--only N]
  --date : UTC day to collect (default: yesterday)
  --only : limit to first N items (smoke test)
"""
import argparse
import datetime as dt
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.darkerdb.com"
ROOT = os.path.dirname(os.path.abspath(__file__))
REQUEST_GAP = 1.05          # seconds between requests (limit is 60/min)
KEY = os.environ.get("DARKERDB_API_KEY", "").strip()


def api_get(path, params):
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": "dnd-price-history collector",
        "X-Api-Key": KEY,
    })
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(15 * (attempt + 1))
                continue
            if e.code >= 500:
                time.sleep(5 * (attempt + 1))
                continue
            raise
        except Exception:
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("retries exhausted: " + path)


def collect_day(day, only=None):
    catalog = json.load(open(os.path.join(ROOT, "catalog.json"), encoding="utf-8"))
    ids = sorted(catalog)
    if only:
        ids = ids[:only]

    frm = day.strftime("%Y-%m-%dT00:00:00Z")
    to = (day + dt.timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")

    # 시즌 종료 후 마켓이 ~2주 잠기는 기간엔 전역 판매가 0이 된다.
    # 요청 1개로 먼저 확인해서, 잠겨 있으면 2,422개 스윕을 통째로 건너뛴다.
    # 마켓이 다시 열리면 자동으로 정상 수집이 재개된다.
    probe = api_get("/v2/market", {"has_sold": "true", "from": frm, "to": to, "limit": 1})
    global_total = (probe.get("pagination") or {}).get("total") or 0
    if global_total < 10:
        print("market locked/empty on %s (global sales=%d) — sweep skipped"
              % (day, global_total), flush=True)
        return {}

    result = {}
    errors = 0

    for i, item_id in enumerate(ids):
        try:
            d = api_get("/v2/market", {
                "item_id": item_id, "has_sold": "true",
                "from": frm, "to": to, "limit": 25,
            })
            rows = d.get("body") or []
            total = (d.get("pagination") or {}).get("total") or 0
            prices = []
            for x in rows:
                p = x.get("price_per_unit") or x.get("price")
                if isinstance(p, (int, float)) and p > 0:
                    prices.append(float(p))
            if prices and total:
                result[item_id] = [
                    round(statistics.median(prices), 1),
                    round(min(prices), 1),
                    round(max(prices), 1),
                    int(total),
                ]
        except Exception as e:
            errors += 1
            if errors <= 5:
                print("  err %s: %s" % (item_id, str(e)[:80]), flush=True)
        if (i + 1) % 200 == 0:
            print("  %d/%d (recorded %d)" % (i + 1, len(ids), len(result)), flush=True)
        time.sleep(REQUEST_GAP)

    print("done: %d items with sales, %d errors" % (len(result), errors), flush=True)
    return result


def save(day, day_data):
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    month_path = os.path.join(ROOT, "data", day.strftime("%Y-%m") + ".json")
    month = {}
    if os.path.exists(month_path):
        month = json.load(open(month_path, encoding="utf-8"))
    month[day.strftime("%Y-%m-%d")] = day_data
    json.dump(month, open(month_path, "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))

    months = sorted(f[:-5] for f in os.listdir(os.path.join(ROOT, "data"))
                    if f.endswith(".json") and f != "months.json")
    json.dump(months, open(os.path.join(ROOT, "data", "months.json"), "w"),
              separators=(",", ":"))
    print("saved -> %s (%.1f KB)" % (os.path.basename(month_path),
                                     os.path.getsize(month_path) / 1024), flush=True)


def collected_days():
    """data/ 에 이미 기록된 날짜 집합 (빈 결과로 수집된 날 포함)."""
    days = set()
    data_dir = os.path.join(ROOT, "data")
    if not os.path.isdir(data_dir):
        return days
    for f in os.listdir(data_dir):
        if f.endswith(".json") and f != "months.json":
            try:
                days.update(json.load(open(os.path.join(data_dir, f), encoding="utf-8")).keys())
            except Exception:
                pass
    return days


# DarkerDB 는 판매 기록을 대략 2~3주만 보관한다. 그보다 오래된 날은 조회해도 비어 있음.
BACKFILL_WINDOW_DAYS = 15
MAX_DAYS_PER_RUN = 4        # 하루 스윕이 약 43분 — 4일이면 Actions 6시간 제한 안에 안전


def refresh_patches():
    """패치 목록(이름·배포일)을 patches.json 으로 갱신. 차트의 패치 마커에 쓰인다."""
    rows = []
    try:
        page = 1
        while page <= 20:
            d = api_get("/v2/patches", {"limit": 50, "page": page})
            batch = d.get("body") or []
            rows.extend(batch)
            total = (d.get("pagination") or {}).get("total") or 0
            if not batch or len(rows) >= total:
                break
            page += 1
            time.sleep(0.5)
        out = [
            {"slug": r.get("slug"), "title": r.get("title"),
             "date": r.get("released_at"), "kind": r.get("kind")}
            for r in rows if r.get("released_at")
        ]
        out.sort(key=lambda r: r["date"], reverse=True)
        json.dump(out, open(os.path.join(ROOT, "patches.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, separators=(",", ":"))
        print("patches.json refreshed: %d entries" % len(out), flush=True)
    except Exception as e:
        print("patches refresh failed: %s" % str(e)[:100], flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="특정 UTC 날짜 하루만 수집")
    ap.add_argument("--auto", action="store_true",
                    help="지난 %d일 중 미수집 날짜를 최신부터 최대 %d일 수집"
                    % (BACKFILL_WINDOW_DAYS, MAX_DAYS_PER_RUN))
    ap.add_argument("--only", type=int)
    args = ap.parse_args()

    if not KEY:
        print("DARKERDB_API_KEY missing", file=sys.stderr)
        sys.exit(1)

    today = dt.datetime.now(dt.timezone.utc).date()

    if args.date:
        targets = [dt.datetime.strptime(args.date, "%Y-%m-%d").date()]
    elif args.auto:
        have = collected_days()
        targets = []
        for back in range(1, BACKFILL_WINDOW_DAYS + 1):     # 어제부터 과거로
            d = today - dt.timedelta(days=back)
            if d.strftime("%Y-%m-%d") not in have:
                targets.append(d)
            if len(targets) >= MAX_DAYS_PER_RUN:
                break
        if not targets:
            print("nothing to collect (all days present)", flush=True)
            return
    else:
        targets = [today - dt.timedelta(days=1)]

    for day in targets:
        print("collecting UTC day: %s" % day, flush=True)
        data = collect_day(day, args.only)
        save(day, data)

    refresh_patches()


if __name__ == "__main__":
    main()
