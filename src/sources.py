"""各ショップからの取得（直営ショップのHTMLのみ。Yahoo!/楽天は使わない）。"""
from __future__ import annotations

import re
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from typing import Optional
from urllib.parse import quote_plus, urljoin, urlparse

from core import KEYWORD, LOCAL_ONLY, Config, Offer, SourceResult, norm, scrub

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0.0.0 Safari/537.36")
TIMEOUT = 20
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Cache-Control": "max-age=0",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
    "Sec-Ch-Ua": '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0", "Sec-Ch-Ua-Platform": '"Windows"',
}
JAN = "4573616016018"  # 公式/あみあみ掲載のJANコード
QUERY = "超かぐや姫 プレミアムブースター"

# ---- 在庫判定ワード（NFKC+小文字化後の部分一致）。NEGが優先。
NEG = [
    "売り切れ", "売切れ", "売切", "品切れ", "品切", "在庫なし", "在庫無し", "在庫切れ", "入荷待ち", "再入荷",
    "予約停止", "予約受付終了", "予約終了", "受付終了", "販売終了", "完売", "sold out", "soldout",
    "取扱終了", "受付停止", "予定数終了", "予定枚数終了", "抽選終了", "販売休止", "一時停止",
    "予約開始前", "予約受付前", "予約開始予定", "近日予約", "近日販売", "近日入荷", "お取り寄せ不可",
    "currently unavailable", "在庫がありません",
]
POS = [
    "カートに入れる", "カートに追加", "カートへ", "予約する", "予約注文", "予約カート", "購入する",
    "今すぐ買う", "在庫あり", "在庫:○", "在庫: ○", "残りわずか", "予約受付中", "ご予約受付中",
    "抽選受付中", "抽選販売受付中", "応募受付中", "エントリー受付中", "add to cart", "buy now", "in stock",
]
# 「売り切れ次第終了」などはNEGではなく販売中を示す定型句なので判定前に除去する
BOILER = re.compile(r"(売り切れ次第|売切れ次第|品切れ次第|なくなり次第|在庫がなくなり|※[^。]{0,100})")

A_RE = re.compile(r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.S | re.I)
IMG_RE = re.compile(r'<img\b[^>]*?alt=["\']([^"\']*)["\'][^>]*>', re.I)
TOKEN_RE = re.compile(r"\[\[([^|\]]*)\|([^\]]*)\]\]")
PRICE_RE = re.compile(r"(?:[¥￥]\s*([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{3,6}))|(?:([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{3,6})\s*円)")
BLOCK_MARKERS = ["captcha", "robot check", "access denied", "are you a human", "bot detection",
                 "アクセスが集中", "ご利用を制限", "不正なアクセス"]


# ---------------------------------------------------------------- HTTP

def _decode(raw: bytes, ctype: str) -> str:
    m = re.search(r"charset=([\w-]+)", ctype or "", re.I) or re.search(rb"charset=[\"']?([\w-]+)", raw[:3000], re.I)
    enc = "utf-8"
    if m:
        g = m.group(1)
        enc = g.decode() if isinstance(g, bytes) else g
    try:
        return raw.decode(enc, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def http_get(url: str, headers: Optional[dict] = None, timeout: int = TIMEOUT):
    req = urllib.request.Request(url, headers={**BROWSER_HEADERS, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, _decode(r.read(2_000_000), r.headers.get("Content-Type", ""))
    except urllib.error.HTTPError as e:
        raw = b""
        try:
            raw = e.read(200_000)
        except Exception:
            pass
        return e.code, _decode(raw, e.headers.get("Content-Type", "") if e.headers else "")


def looks_blocked(status: int, body: str) -> bool:
    if status in (401, 403, 429, 503):
        return True
    low = body.lower()
    if "api-services-support@amazon.com" in low or "validatecaptcha" in low:
        return True
    return len(body) < 30000 and any(k in low for k in BLOCK_MARKERS)


# ---------------------------------------------------------------- テキスト化・判定

def textize(html: str, base: str):
    """HTML→テキスト。リンクは [[URL|表示文字]] として残す。戻り値=(text, リンク本数)"""
    html = re.sub(r"(?is)<(script|style|noscript|svg)\b.*?</\1>", " ", html)
    html = re.sub(r"(?s)<!--.*?-->", " ", html)
    html = re.sub(r"(?is)<head\b.*?</head>", " ", html)
    html = IMG_RE.sub(r" \1 ", html)
    links = 0

    def sub_a(m):
        nonlocal links
        links += 1
        inner = re.sub(r"<[^>]+>", " ", m.group(2))
        inner = re.sub(r"\s+", " ", unescape(inner)).strip().replace("]", "］").replace("|", "｜")
        return f" [[{urljoin(base, unescape(m.group(1)))}|{inner}]] "

    html = A_RE.sub(sub_a, html)
    text = re.sub(r"<[^>]+>", " ", html)
    text = unicodedata.normalize("NFKC", unescape(text))
    return re.sub(r"\s+", " ", text), links


def judge(window: str):
    """(available, 根拠) available: True/False/None(判定不能)"""
    w = norm(BOILER.sub(" ", TOKEN_RE.sub(lambda m: m.group(2), window)))
    neg = [k for k in NEG if k in w]
    if neg:
        return False, neg
    pos = [k for k in POS if k in w]
    if pos:
        return True, pos
    return None, []


def extract_price(window: str) -> Optional[int]:
    w = TOKEN_RE.sub(lambda m: m.group(2), window)
    start = 0
    m = re.search(r"販売価格", w)
    if m:
        start = m.end()
    for pm in PRICE_RE.finditer(w, start):
        v = int((pm.group(1) or pm.group(2)).replace(",", ""))
        ctx_before = w[max(0, pm.start() - 6):pm.start()]
        ctx_after = w[pm.end():pm.end() + 6]
        if "送料" in ctx_before or "ポイント" in ctx_after or "pt" in ctx_after.lower():
            continue
        if 500 <= v <= 500000:
            return v
    return None


def _kind(title: str, window: str) -> str:
    if "抽選" in window and "受付中" in window:
        return "抽選"
    return "予約" if "予約" in norm(title + window) else "購入"


# ---------------------------------------------------------------- HTML: 検索結果/一覧ページ

def parse_search(site: str, text: str, assume_in_stock: bool = False) -> list:
    """assume_in_stock: 「在庫ありのみ」で絞り込んだURLを見ている場合、表示されている=在庫ありとみなす
    （NEGワードがなく、POSワードも無いときだけ。判定不能で見逃すのを防ぐ）"""
    toks = list(TOKEN_RE.finditer(text))
    hits = [m for m in toks if KEYWORD in norm(m.group(2))]
    best = {}
    for i, m in enumerate(hits):
        href = m.group(1)
        end = len(text)
        for nxt in hits[i + 1:]:
            if nxt.group(1) != href:
                end = nxt.start()
                break
        window = text[m.end(): min(end, m.end() + 700)]
        cur = best.get(href)
        title = max([m.group(2), cur[0] if cur else ""], key=len)
        if cur is None or len(window.strip()) > len(cur[1].strip()):
            best[href] = (title, window)
        else:
            best[href] = (title, cur[1])
    offers = []
    for href, (title, window) in best.items():
        avail, why = judge(window)
        note = ",".join(why)
        if avail is None and assume_in_stock:
            avail, note = True, "在庫ありフィルタ結果に表示"
        offers.append(Offer(site=site, title=title.strip(), url=href, price=extract_price(window),
                            available=avail, kind=_kind(title, window), note=note))
    return offers


def fetch_search_html(name: str, url_tpl: str, assume_in_stock: bool = False) -> SourceResult:
    url = url_tpl.replace("{q}", quote_plus(QUERY))
    status, body = http_get(url)
    if looks_blocked(status, body):
        return SourceResult(name, ok=False, blocked=True, error=f"HTTP {status} ブロックの可能性")
    if status >= 400:
        return SourceResult(name, ok=False, error=f"HTTP {status}")
    text, links = textize(body, url)
    return SourceResult(name, offers=parse_search(name, text, assume_in_stock), links=links)


# ---------------------------------------------------------------- HTML: 商品ページ直接指定

def parse_page(site: str, url: str, html: str) -> Offer:
    tm = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    title = re.sub(r"\s+", " ", unescape(tm.group(1))).strip() if tm else site
    text, _ = textize(html, url)
    idx = text.find(KEYWORD)
    if idx < 0:
        return Offer(site=site, title=title, url=url, price=None, available=None, pinned=True,
                     note="ページ内にキーワードなし(構造変更/JS描画/ページ削除の可能性)")
    window = text[max(0, idx - 200): idx + 2500]
    avail, why = judge(window)
    return Offer(site=site, title=title, url=url, price=extract_price(window), available=avail,
                 kind=_kind(title, window), note=",".join(why), pinned=True)


def fetch_page(name: str, url: str) -> SourceResult:
    status, body = http_get(url)
    if looks_blocked(status, body):
        return SourceResult(name, ok=False, blocked=True, error=f"HTTP {status} ブロックの可能性")
    if status >= 400:
        return SourceResult(name, ok=False, error=f"HTTP {status}")
    _, links = textize(body, url)
    return SourceResult(name, offers=[parse_page(name, url, body)], links=links)


# ---------------------------------------------------------------- ソース定義と一括取得
# (名前, URL, 在庫ありフィルタ済みか)  {q} は「超かぐや姫 プレミアムブースター」に置換
SEARCH_SITES = [
    ("Amazon", "https://www.amazon.co.jp/s?k=" + JAN, False),
    # JAN検索＋在庫ありのみ。表示された=買える、という前提で判定する
    ("駿河屋", "https://www.suruga-ya.jp/search?category=&search_word=" + JAN +
     "&ck=true&inStock=On&rankBy=release_date%28int%29%3Adescending", True),
    ("ゲーマーズ", "https://www.gamers.co.jp/keyword/index.php?kid=1893&nf=1&smt=&ss=2&sl=2&kw=&spc=&scc=&ss=5&sl=2&al=0", False),
    ("遊々亭", "https://yuyu-tei.jp/sell/ws/m/extrabox", False),
    ("アニメイト(検索)", "https://www.animate-onlineshop.jp/products/list.php?smt={q}", False),
    ("ヨドバシ", "https://www.yodobashi.com/?word={q}", False),
    ("ビックカメラ", "https://www.biccamera.com/bc/category/?q={q}", False),
]
# 商品ページを直接見張る（検索結果などで確認済みのURL）
PAGE_SITES = [
    ("アニメイト(商品ページ)", "https://www.animate-onlineshop.jp/pd/3580738/"),
    ("あみあみ(商品ページ)", "https://www.amiami.jp/top/detail/detail?gcode=CARD-00034685"),
    ("TCG通販アドバンテージ", "https://www.advantagetcg.jp/phone/product/134621"),
    ("ミントモール", "https://www.mint-mall.net/products/detail.php?product_id=1328820"),
]


def build_sources(cfg: Config) -> list:
    jobs = []
    for name, tpl, in_stock in SEARCH_SITES:
        jobs.append((name, lambda n=name, t=tpl, s=in_stock: fetch_search_html(n, t, s)))
    pages = list(PAGE_SITES)
    for i, item in enumerate(x.strip() for x in cfg.extra_urls.split(",") if x.strip()):
        nm, _, u = item.partition("=") if "=" in item.split("://")[0] else ("", "", item)
        pages.append((nm or urlparse(u).netloc or f"追加{i + 1}", u))
    for name, url in pages:
        jobs.append((name, lambda n=name, u=url: fetch_page(n, u)))
    names = [n for n, _ in jobs]
    if cfg.only_sources:
        wanted = [x.strip() for x in cfg.only_sources.split(",") if x.strip()]
        unknown = [w for w in wanted if w not in names]
        if unknown:
            raise ValueError("unknown source name: " + ", ".join(unknown))
        jobs = [j for j in jobs if j[0] in wanted]
    elif cfg.skip_local:
        jobs = [j for j in jobs if j[0] not in LOCAL_ONLY]
    return jobs


def fetch_all(cfg: Config) -> list:
    jobs = build_sources(cfg)

    def run(job):
        name, fn = job
        try:
            return fn()
        except Exception as e:
            return SourceResult(name, ok=False, error=scrub(f"{type(e).__name__}: {e}", cfg)[:200])

    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(run, jobs))
