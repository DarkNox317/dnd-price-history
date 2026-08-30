# -*- coding: utf-8 -*-
"""Dark and Darker 5-minute market archiver.

Sweeps ALL sold listings of one UTC day (hour by hour, 50/page) and stores:
  data5m/YYYY-MM-DD.json : per item, per 5-minute bucket [idx, med, min, max, cnt]
  data2/YYYY-MM.json     : per item daily rollup [med, min, max, cnt] (exact, full data)

Usage:
  DARKERDB_API_KEY=... python collect5m.py [--date YYYY-MM-DD] [--auto]
                                           [--item-id ID] [--max-pages N]
  --auto      : collect newest missing day within the retention window (one day per run)
  --item-id   : smoke test — sweep only this item (cheap, few requests)
  --max-pages : smoke test — stop each sub-sweep after N pages
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
REQUEST_GAP = 0.68          # Epic 플랜: 90 req/min → 분당 ~88로 한도 바로 아래 페이싱
PAGE_LIMIT = 50
BACKFILL_WINDOW_DAYS = 15
KEY = os.environ.get("DARKERDB_API_KEY", "").strip()


_last_req = [0.0]

def api_get(path, params):
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": "dnd-price-history 5m archiver",
        "X-Api-Key": KEY,
    })
    for attempt in range(5):
        # 직전 요청 시작으로부터 REQUEST_GAP 이 지나도록만 대기 (왕복 시간을 낭비하지 않게)
        wait = _last_req[0] + REQUEST_GAP - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_req[0] = time.time()
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 402:
                # 크레딧 소진: 부분 데이터를 저장하지 않도록 즉시 중단. 리셋 후 --auto 가 같은 날을 다시 수집한다.
                print("API credit exhausted (402) — run aborted, day will be retried after reset", flush=True)
                raise SystemExit(1)
            if e.code == 429:
                # 서버가 알려주는 대기 시간(Retry-After)을 따르고, 없으면 보수적으로 후퇴
                try:
                    ra = float(e.headers.get("Retry-After") or 0)
                except ValueError:
                    ra = 0
                time.sleep(min(60, max(ra, 2 * (attempt + 1))))
                continue
            if e.code >= 500:
                time.sleep(5 * (attempt + 1))
                continue
            raise
        except Exception:
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("retries exhausted: " + path)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def sweep_window(frm, to, acc, item_id=None, max_pages=None):
    """[frm, to) 구간의 판매 전량을 페이지로 훑어 acc[(item, bucket)] 에 가격 누적."""
    day_start = frm.replace(hour=0, minute=0, second=0)
    page, fetched = 1, 0
    while True:
        params = {"has_sold": "true", "from": iso(frm), "to": iso(to),
                  "limit": PAGE_LIMIT, "page": page}
        if item_id:
            params["item_id"] = item_id
        d = api_get("/v2/market", params)
        rows = d.get("body") or []
        for r in rows:
            iid = r.get("item_id")
            p = r.get("price_per_unit") or r.get("price")
            ts = r.get("created_at", "")
            if not iid or not isinstance(p, (int, float)) or p <= 0 or len(ts) < 16:
                continue
            hh, mm = int(ts[11:13]), int(ts[14:16])
            bucket = (hh * 60 + mm) // 5
            acc.setdefault(iid, {}).setdefault(bucket, []).append(float(p))
            fetched += 1
        pg = d.get("pagination") or {}
        total = pg.get("total") or 0
        if not rows or page * PAGE_LIMIT >= total or (max_pages and page >= max_pages):
            return fetched, total
        page += 1


def collect_day(day, item_id=None, max_pages=None):
    acc = {}
    t0 = time.time()
    base = dt.datetime(day.year, day.month, day.day)
    grabbed = 0
    if item_id:
        # 단일 아이템은 거래량이 적어 하루를 통째로 페이지네이션해도 얕다
        frm = base
        to = base + dt.timedelta(days=1, seconds=-1)
        grabbed, total = sweep_window(frm, to, acc, item_id, max_pages)
        print("  %s %s: %d건 수집 (전체 %d건)" % (day, item_id, grabbed, total), flush=True)
        return acc, grabbed
    for h in range(24):
        frm = base + dt.timedelta(hours=h)
        # API 의 from/to 는 둘 다 경계 포함 → to 를 1초 앞당겨 정각 거래의 이중 집계를 막는다
        to = base + dt.timedelta(hours=h + 1, seconds=-1)
        n, total = sweep_window(frm, to, acc, item_id, max_pages)
        grabbed += n
        elapsed = (time.time() - t0) / 60
        eta = elapsed / (h + 1) * (23 - h)
        print("  [%s UTC] %02d시 구간: %d건 수집 (구간 전체 %d건, 누적 %d건, 남은 시간 약 %d분)"
              % (dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S"),
                 h, n, total, grabbed, int(eta)), flush=True)
    return acc, grabbed


def summarize(acc):
    """acc → (5분 버킷 파일용, 일 집계용)"""
    detail, daily = {}, {}
    for iid, buckets in acc.items():
        rows = []
        all_prices = []
        cnt = 0
        for b in sorted(buckets):
            ps = buckets[b]
            rows.append([b, round(statistics.median(ps), 1),
                         round(min(ps), 1), round(max(ps), 1), len(ps)])
            all_prices.extend(ps)
            cnt += len(ps)
        detail[iid] = rows
        daily[iid] = [round(statistics.median(all_prices), 1),
                      round(min(all_prices), 1), round(max(all_prices), 1), cnt]
    return detail, daily


def save(day, detail, daily, merge=False):
    d5 = os.path.join(ROOT, "data5m")
    d2 = os.path.join(ROOT, "data2")
    os.makedirs(d5, exist_ok=True)
    os.makedirs(d2, exist_ok=True)

    daykey = day.strftime("%Y-%m-%d")
    p5 = os.path.join(d5, daykey + ".json")
    if merge and os.path.exists(p5):
        prev = json.load(open(p5, encoding="utf-8")).get("items") or {}
        prev.update(detail)
        detail = prev
    json.dump({"date": daykey, "items": detail}, open(p5, "w", encoding="utf-8"),
              separators=(",", ":"))

    p2 = os.path.join(d2, day.strftime("%Y-%m") + ".json")
    month = json.load(open(p2, encoding="utf-8")) if os.path.exists(p2) else {}
    if merge and daykey in month:
        month[daykey].update(daily)
    else:
        month[daykey] = daily
    json.dump(month, open(p2, "w", encoding="utf-8"), separators=(",", ":"))

    days = sorted(f[:-5] for f in os.listdir(d5)
                  if f.endswith(".json") and f != "index.json")
    json.dump(days, open(os.path.join(d5, "index.json"), "w"), separators=(",", ":"))
    months = sorted(f[:-5] for f in os.listdir(d2)
                    if f.endswith(".json") and f != "months.json")
    json.dump(months, open(os.path.join(d2, "months.json"), "w"), separators=(",", ":"))

    print("saved -> %s (%.2f MB), %s (%.2f MB)"
          % (os.path.basename(p5), os.path.getsize(p5) / 1e6,
             os.path.basename(p2), os.path.getsize(p2) / 1e6), flush=True)


def collected_days():
    d5 = os.path.join(ROOT, "data5m")
    if not os.path.isdir(d5):
        return set()
    return {f[:-5] for f in os.listdir(d5) if f.endswith(".json") and f != "index.json"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--item-id")
    ap.add_argument("--max-pages", type=int)
    ap.add_argument("--save", action="store_true",
                    help="--item-id 모드에서도 결과를 저장 (기존 날짜 데이터에 병합)")
    args = ap.parse_args()

    if not KEY:
        print("DARKERDB_API_KEY missing", file=sys.stderr)
        sys.exit(1)

    today = dt.datetime.now(dt.timezone.utc).date()
    if args.date:
        day = dt.datetime.strptime(args.date, "%Y-%m-%d").date()
    elif args.auto:
        have = collected_days()
        day = None
        # 가장 오래된 미수집 날짜부터 — API 보존 기간이 끝나 소멸되기 전의 날짜를 먼저 구한다
        for back in range(BACKFILL_WINDOW_DAYS, 0, -1):
            d = today - dt.timedelta(days=back)
            if d.strftime("%Y-%m-%d") not in have:
                day = d
                break
        if day is None:
            print("nothing to collect (all days present)", flush=True)
            return
    else:
        day = today - dt.timedelta(days=1)

    # 마켓 잠금(시즌 초기화) 감지: 전역 판매 0이면 빈 날로 기록하고 종료
    probe = api_get("/v2/market", {"has_sold": "true",
                                   "from": day.strftime("%Y-%m-%dT00:00:00Z"),
                                   "to": (day + dt.timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z"),
                                   "limit": 1})
    if ((probe.get("pagination") or {}).get("total") or 0) < 10:
        print("market locked/empty on %s — recorded as empty day" % day, flush=True)
        if not args.item_id and not args.max_pages:
            save(day, {}, {})
        return

    print("collecting UTC day %s%s" % (day, " (smoke: %s)" % args.item_id if args.item_id else ""),
          flush=True)
    acc, grabbed = collect_day(day, args.item_id, args.max_pages)
    detail, daily = summarize(acc)
    print("done: %d건 수집, 아이템 %d종, 버킷 %d개"
          % (grabbed, len(detail), sum(len(v) for v in detail.values())), flush=True)

    if (args.item_id or args.max_pages) and not args.save:
        # 스모크 테스트: 저장하지 않고 결과만 출력
        print(json.dumps({"detail_sample": {k: v[:6] for k, v in list(detail.items())[:3]},
                          "daily": daily}, ensure_ascii=False, indent=1))
        return

    save(day, detail, daily, merge=bool(args.item_id))

    if not args.item_id:
        try:
            import collect as legacy
            legacy.refresh_patches()
        except Exception as e:
            print("patches refresh skipped: %s" % str(e)[:80], flush=True)


if __name__ == "__main__":
    main()
