"""判定ロジックと状態管理（外部通信なし。テスト可能な純粋ロジック）。"""
from __future__ import annotations

import hashlib
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlparse

KEYWORD = "超かぐや姫"
JST = timezone(timedelta(hours=9))

# AWS(Lambda)のIPからはブロックされやすく、自宅回線(ラズパイ等)から見るショップ
LOCAL_ONLY = ["ゲーマーズ", "遊々亭", "アニメイト(検索)", "アニメイト(商品ページ)",
              "あみあみ(商品ページ)", "ミントモール", "ヨドバシ", "ビックカメラ"]

# 商品タイトルに含まれていたら対象外にする語（NFKC+小文字化後で比較）
NG_TITLE = [
    "カートン", "carton", "30box", "30ボックス", "シングル", "中古", "買取",
    "スリーブ", "サプライ", "デッキホルダー", "プレイマット", "ラバーマット",
    "ストレージ", "ボックス特典", "box特典", "トライアルデッキ", "バラ売り", "コンプ",
]

TRACKING_KEYS = {
    "sc_i", "scid", "ref", "ref_", "tag", "utm_source", "utm_medium", "utm_campaign",
    "utm_content", "utm_term", "gclid", "fbclid", "affiliate", "aff", "cm_sp",
}


def norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "").lower()


@dataclass
class Offer:
    site: str
    title: str
    url: str
    price: Optional[int]
    available: Optional[bool]  # True=購入/予約可能 False=不可 None=判定不能
    kind: str = "購入"  # 購入 / 予約 / 抽選
    note: str = ""
    pinned: bool = False  # 商品ページを直接指定した監視（キーワード判定を省略）


@dataclass
class SourceResult:
    name: str
    ok: bool = True
    blocked: bool = False
    error: str = ""
    offers: list = field(default_factory=list)
    links: Optional[int] = None  # ページ構造取得の健全性チェック用（リンク本数）


@dataclass
class Config:
    price_max: int = 3960
    price_min: int = 2500
    remind_minutes: int = 30
    max_reminders: int = 4
    fail_threshold: int = 6
    line_token: str = ""
    line_user_id: str = ""
    extra_urls: str = ""
    only_sources: str = ""   # 指定した名前のショップだけ監視（カンマ区切り）
    skip_local: bool = False  # True: 自宅担当(LOCAL_ONLY)をこちらでは見ない
    label: str = ""           # 通知の見出しに付ける識別子（例: (自宅)）
    table: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ
        return cls(
            price_max=int(e.get("PRICE_MAX", "3960")),
            price_min=int(e.get("PRICE_MIN", "2500")),
            remind_minutes=int(e.get("REMIND_MINUTES", "30")),
            max_reminders=int(e.get("MAX_REMINDERS", "4")),
            fail_threshold=int(e.get("FAIL_THRESHOLD", "6")),
            line_token=e.get("LINE_CHANNEL_TOKEN", ""),
            line_user_id=e.get("LINE_USER_ID", ""),
            extra_urls=e.get("EXTRA_URLS", ""),
            only_sources=e.get("ONLY_SOURCES", ""),
            skip_local=e.get("SKIP_LOCAL", "").lower() in ("1", "true", "yes"),
            label=e.get("LABEL", ""),
            table=e.get("TABLE_NAME", ""),
        )

    def secrets(self) -> list:
        return [s for s in (self.line_token,) if s]


def scrub(text: str, cfg: Config) -> str:
    for s in cfg.secrets():
        text = text.replace(s, "***")
    return text


# ---------------------------------------------------------------- 判定

def classify(o: Offer, price_min: int, price_max: int) -> Optional[str]:
    """BOX候補か判定。 'ok'=価格条件OK / 'over'=定価超え / 'unknown'=価格不明 / None=対象外"""
    p = o.price
    if not o.pinned:
        t = norm(o.title)
        if KEYWORD not in t or "ブースター" not in t:
            return None
        if any(k in t for k in NG_TITLE):
            return None
        has_box = ("box" in t) or ("ボックス" in t) or ("6パック" in t)
        if not has_box and (p is None or not (3000 <= p <= price_max)):
            return None
    if p is None:
        return "unknown"
    if p < price_min:
        return None
    if p > price_max:
        return "over"
    return "ok"


def canonical_url(url: str) -> str:
    u = urlparse(url)
    q = sorted((k, v) for k, v in parse_qsl(u.query) if k.lower() not in TRACKING_KEYS)
    return f"{u.scheme}://{u.netloc}{u.path}" + (("?" + urlencode(q)) if q else "")


def offer_key(o: Offer) -> str:
    return hashlib.sha1((o.site + "|" + canonical_url(o.url or o.title)).encode()).hexdigest()[:20]


# ---------------------------------------------------------------- 通知文

def _yen(p: Optional[int]) -> str:
    return f"{p:,}円" if p is not None else "価格取得不可(要確認)"


def format_alert(items: list, cfg: Config) -> str:
    lines = [f"🔔【超かぐや姫 BOX 購入/予約可能】（{cfg.price_max:,}円以下）"]
    for o, verdict, mode, n in items:
        tag = "" if mode == "new" else f"🔁再通知{n}/{cfg.max_reminders} "
        lines.append("")
        lines.append(f"{tag}■ {o.site}［{o.kind}］")
        lines.append(_yen(o.price) + ("" if verdict == "ok" else "  ⚠価格が取れていません。ページで確認を"))
        lines.append(o.title[:70])
        lines.append(o.url)
    return "\n".join(lines)


# ---------------------------------------------------------------- 状態機械

def summarize(results: list, cfg: Config) -> list:
    out = []
    for r in results:
        found = len(r.offers)
        boxes = [o for o in r.offers if classify(o, cfg.price_min, cfg.price_max)]
        priced = [o for o in boxes if o.price is not None]
        cheapest = min(priced, key=lambda o: o.price) if priced else None
        out.append({
            "name": r.name, "ok": r.ok, "blocked": r.blocked, "error": r.error,
            "found": found, "boxes": len(boxes), "links": r.links,
            "available": sum(1 for o in boxes
                             if o.available is True and classify(o, cfg.price_min, cfg.price_max) in ("ok", "unknown")),
            "cheapest": ({"price": cheapest.price, "available": cheapest.available, "site": cheapest.site}
                         if cheapest else None),
        })
    return out


def process_offers(results: list, store, notify: Callable[[str], None], cfg: Config,
                   now: Optional[float] = None) -> dict:
    """在庫判定→通知。通知成功後にだけ状態を更新する（失敗時は次回再試行）。"""
    now = time.time() if now is None else now
    to_notify, pending, errors = [], [], []
    live = []
    for r in results:
        for o in r.offers:
            v = classify(o, cfg.price_min, cfg.price_max)
            if v is None:
                continue
            key = "OFFER#" + offer_key(o)
            st = store.get(key) or {}
            was = bool(st.get("available"))
            alertable = o.available is True and v in ("ok", "unknown")
            if alertable:
                live.append(o)
                if not was:
                    to_notify.append((o, v, "new", 0))
                    pending.append((key, {"available": True, "last_notified": now, "notify_count": 1,
                                          "price": o.price, "title": o.title, "site": o.site, "url": o.url,
                                          "since": now}))
                else:
                    cnt = int(st.get("notify_count", 0))
                    if cnt < cfg.max_reminders and now - float(st.get("last_notified", 0)) >= cfg.remind_minutes * 60:
                        to_notify.append((o, v, "remind", cnt))
                        ns = dict(st)
                        ns.update({"last_notified": now, "notify_count": cnt + 1, "price": o.price})
                        pending.append((key, ns))
            elif was:
                ns = dict(st)
                ns.update({"available": False, "notify_count": 0, "ended": now})
                store.put(key, ns)

    notified = 0
    if to_notify:
        try:
            notify(format_alert(to_notify, cfg))
            for key, ns in pending:
                store.put(key, ns)
            notified = len(to_notify)
        except Exception as e:  # LINE障害など。状態は更新しない＝次回また通知を試みる
            errors.append("通知失敗: " + scrub(repr(e), cfg)[:200])
    return {"available_now": len(live), "notified": notified, "errors": errors}


def update_health(results: list, store, notify: Callable[[str], None], cfg: Config,
                  now: Optional[float] = None) -> list:
    """ショップごとの連続失敗を数え、閾値に達したら通知（同時に複数あれば1通にまとめる）。回復時も通知。"""
    now = time.time() if now is None else now
    msgs, updates, errors = [], [], []
    for r in results:
        key = "SRC#" + r.name
        st = store.get(key) or {}
        fails = (int(st.get("fails", 0)) + 1) if not r.ok else 0
        alerted = bool(st.get("alerted"))
        new_alerted = alerted
        if not r.ok and fails >= cfg.fail_threshold and not alerted:
            why = "ブロックされています（このショップは手動確認が必要）" if r.blocked else (r.error[:150] or "不明")
            msgs.append(f"⚠ {r.name}の取得が{fails}回連続で失敗\n原因: {why}")
            new_alerted = True
        if r.ok and alerted:
            msgs.append(f"✅ {r.name}の取得が復旧しました")
            new_alerted = False
        updates.append((key, {"fails": fails, "alerted": new_alerted, "last_ok": now if r.ok else st.get("last_ok"),
                              "last_error": r.error if not r.ok else ""}))
    if msgs:
        try:
            notify(f"【超かぐや姫BOX監視{cfg.label} 障害情報】\n" + "\n\n".join(msgs))
        except Exception as e:
            errors.append("障害通知失敗: " + scrub(repr(e), cfg)[:200])
            # 通知に失敗したら alerted を立てずに次回再試行
            updates = [(k, dict(v, alerted=False if v.get("alerted") else v.get("alerted"))) for k, v in updates]
    for k, v in updates:
        store.put(k, v)
    return errors


def build_heartbeat(stats: list, available_now: int, cfg: Config, now: Optional[float] = None) -> str:
    dt = datetime.fromtimestamp(time.time() if now is None else now, JST)
    lines = [f"🟢 超かぐや姫BOX監視{cfg.label} 生存確認 {dt:%m/%d %H:%M}",
             f"条件: 1BOX {cfg.price_max:,}円以下で購入/予約可能",
             f"現在の購入/予約可能: {'あり(' + str(available_now) + '件) ← 別途通知済み' if available_now else 'なし'}",
             ""]
    healthy = 0
    for s in stats:
        if s["blocked"]:
            icon, tail = "🚫", "ブロックされています"
        elif not s["ok"]:
            icon, tail = "❌", f"取得失敗 {s['error'][:60]}"
        elif s["links"] is not None and s["links"] < 5:
            icon, tail = "⚠", f"ページ構造を取得できていない可能性(リンク{s['links']}本)"
        else:
            icon = "✅"
            healthy += 1
            tail = f"BOX候補{s['boxes']}件"
            c = s["cheapest"]
            if c:
                st = {True: "在庫あり", False: "在庫なし"}.get(c["available"], "判定不能")
                tail += f" / 最安{c['price']:,}円({st})"
        lines.append(f"{icon} {s['name']}: {tail}")
    if cfg.skip_local:
        lines.append(f"※{LOCAL_ONLY[0]}など{len(LOCAL_ONLY)}店は自宅のラズパイが担当です。そちらの9時の通知も確認してください")
    lines.append("")
    lines.append(f"監視できているショップ: {healthy}/{len(stats)}"
                 + ("  ⚠ 全滅です。要確認" if stats and healthy == 0 else ""))
    lines += ["※毎朝9時にこの通知が届いていれば監視は稼働中です。届かない日は停止を疑ってください。"]
    return "\n".join(lines)[:4800]
