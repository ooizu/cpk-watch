"""自宅の常時起動マシン(ラズパイ等)用ランナー。Lambdaからブロックされる店だけを巡回する。

  python3 local_runner.py                 通常巡回（cronから5分ごと）
  python3 local_runner.py --auto-heartbeat  通常巡回 + 毎朝9時(JST)以降に1日1回生存確認
  python3 local_runner.py --test          LINEテスト通知
  python3 local_runner.py --dryrun        通知せず各店の取得状況だけ表示
  python3 local_runner.py --heartbeat     生存確認を今すぐ送る
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from core import JST, LOCAL_ONLY, Config, build_heartbeat, classify, process_offers, summarize, update_health  # noqa: E402
from notify import FileStore, LineNotifier  # noqa: E402
import sources  # noqa: E402

log = logging.getLogger("cpk-local")


def load_env(path: str) -> None:
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


def heartbeat_due(store, now: datetime):
    """JST 9時以降で、今日まだ送っていなければ (True, 今日の日付文字列)。"""
    today = now.astimezone(JST).strftime("%Y-%m-%d")
    last = (store.get("local_hb") or {}).get("date")
    return (now.astimezone(JST).hour >= 9 and last != today), today


def setup_logging() -> None:
    log.setLevel(logging.INFO)
    h = RotatingFileHandler(os.path.join(HERE, "run.log"), maxBytes=500_000, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log.addHandler(h)
    log.addHandler(logging.StreamHandler(sys.stdout))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--test", action="store_true")
    g.add_argument("--dryrun", action="store_true")
    g.add_argument("--heartbeat", action="store_true")
    ap.add_argument("--auto-heartbeat", action="store_true")
    a = ap.parse_args(argv)

    load_env(os.path.join(HERE, ".env"))
    os.environ.setdefault("ONLY_SOURCES", ",".join(LOCAL_ONLY))
    os.environ.setdefault("LABEL", "(自宅)")
    cfg = Config.from_env()
    setup_logging()

    if a.test:
        LineNotifier(cfg.line_token, cfg.line_user_id).push(
            "✅ 超かぐや姫BOX監視(自宅ラズパイ)のテスト通知です。届けば連携は正常です。")
        log.info("test sent")
        return 0

    if a.dryrun:
        results = sources.fetch_all(cfg)
        for s in summarize(results, cfg):
            print(f'{s["name"]:<22} ok={s["ok"]} blocked={s["blocked"]} found={s["found"]} boxes={s["boxes"]} {s.get("error") or ""}')
        for r in results:
            for o in r.offers:
                print("  ", r.name, classify(o, cfg.price_min, cfg.price_max), o.available, o.price, o.title[:40], o.url)
        return 0

    try:
        import fcntl
        lock = open(os.path.join(HERE, ".lock"), "w")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("previous run still active; skip")
        return 0
    except ImportError:
        pass

    store = FileStore(os.environ.get("STATE_FILE") or os.path.join(HERE, "state.json"))
    notifier = LineNotifier(cfg.line_token, cfg.line_user_id)
    results = sources.fetch_all(cfg)
    stats = summarize(results, cfg)
    res = process_offers(results, store, notifier.push, cfg)
    errors = list(res["errors"]) + list(update_health(results, store, notifier.push, cfg))

    now = datetime.now(JST)
    due, today = heartbeat_due(store, now)
    if a.heartbeat or (a.auto_heartbeat and due):
        try:
            notifier.push(build_heartbeat(stats, res["available_now"], cfg))
            store.put("local_hb", {"date": today})
        except Exception as e:
            errors.append("生存通知失敗: " + repr(e)[:150])

    log.info(json.dumps({"available_now": res["available_now"], "notified": res["notified"], "errors": errors,
                         "sources": [(s["name"], s["ok"], s["boxes"]) for s in stats]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
