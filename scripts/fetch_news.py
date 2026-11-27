#!/usr/bin/env python3
"""Fetch recent GPU / AI-compute headlines from public RSS/Atom feeds.

Writes docs/data/news.json with title, source, date (PT) and a link only —
no article body or images. On any fetch or parse failure the last existing
news.json is left unchanged and the process exits 0, so the daily workflow
is not failed by a down feed.

Stdlib only. Does not bypass HTTP 403 / other access blocks: those feeds
are skipped.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import ssl
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "docs" / "data" / "news.json"
TZ = ZoneInfo("America/Los_Angeles")
UA = (
    "ComputeNAV/0.1 (high-school research project; headline links only; "
    "+https://computenav.github.io/computenav/)"
)
TIMEOUT = 15
MAX_ITEMS = 8
MAX_AGE_DAYS = 14

# Free public RSS/Atom feeds. A non-200 or non-XML response is skipped.
FEEDS = [
    {
        "name": "Google News",
        "url": (
            "https://news.google.com/rss/search?q="
            + urllib.parse.quote(
                "when:7d (H100 OR H200 OR B200 OR GB200 OR B300 OR GB300 "
                "OR MI300X OR MI325 OR MI350 OR Blackwell OR "
                '"data center GPU" OR "AI GPU" OR "AI accelerator") '
                "(NVIDIA OR AMD OR GPU)"
            )
            + "&hl=en-US&gl=US&ceid=US:en"
        ),
    },
    {
        "name": "NVIDIA",
        "url": "https://nvidianews.nvidia.com/releases.xml",
    },
    {
        "name": "AMD",
        "url": "https://ir.amd.com/rss/news-releases.xml",
    },
    {
        "name": "ServeTheHome",
        "url": "https://www.servethehome.com/feed/",
    },
    {
        "name": "HPCwire",
        "url": "https://www.hpcwire.com/feed/",
    },
    {
        "name": "Data Center Dynamics",
        "url": "https://www.datacenterdynamics.com/en/rss/",
    },
    {
        "name": "TechCrunch",
        "url": "https://techcrunch.com/tag/nvidia/feed/",
    },
    {
        "name": "The Register",
        "url": "https://www.theregister.com/headlines.atom",
    },
]

# Same domains as pipeline/denylist.py — do not link to competitor sites.
DENY_DOMAINS = frozenset({
    "silicondata.com", "ornn.com", "ornnai.com",
    "semianalysis.com", "compute.exchange",
})
# Do not surface competitor names in public headlines either.
DENY_NAME_RE = re.compile(
    r"silicon[\s._-]?data|\bsdh100(?:rt)?\b|\bornn(?:ai)?\b|"
    r"semi[\s._-]?analysis|compute[\s._-]?exchange",
    re.I,
)

# Mirror pipeline/check_public.py so a headline cannot fail the public check.
CODES = r"(?:EUR|GBP|CNY|RMB|JPY|CAD|AUD|CHF|HKD|SGD|INR|KRW|SEK|NOK|DKK|PLN|MXN|BRL|TWD|NZD|ZAR|RUB|TRY)"
SYMBOLS = "€£¥₹₩₽₺₪₫₱"
NUM = r"[0-9][0-9,.]*"
PUBLIC_BLOCK = [
    re.compile(f"[{SYMBOLS}]"),
    re.compile(rf"\b{CODES}\s*{NUM}"),
    re.compile(rf"{NUM}\s*{CODES}\b"),
    re.compile(rf"\b(?:C|A|HK|S|NZ)\$\s*{NUM}"),
]
SOSNOFF = re.compile(r"sosnoff", re.I)

# Relevance: data-center / AI-compute GPU pricing, supply, launches.
STRONG = re.compile(
    r"\b(?:H100|H200|B100|B200|B300|GB200|GB300|A100|A800|H800|"
    r"MI300X|MI300|MI325X|MI325|MI350X|MI350|MI355|MI400|"
    r"Blackwell|Hopper|DGX|HGX|NVL72|Vera Rubin|"
    r"AMD Instinct|Instinct MI|"
    r"data[\s-]?cent(?:er|re) GPUs?|datacenter GPUs?|"
    r"AI GPUs?|AI accelerators?|GPU-hours?|per[\s-]?GPU)\b",
    re.I,
)
MEDIUM = re.compile(
    r"\b(?:GPU clusters?|GPU servers?|GPU supply|GPU shortage|"
    r"GPU pric(?:e|es|ing)|GPU racks?|GPU capacity|"
    r"Nvidia GPUs?|NVIDIA GPUs?|Nvidia chips?|AMD GPUs?|"
    r"AI chips?|AI factory|AI factories|accelerator cards?|"
    r"Radeon AI|neoclouds?)\b",
    re.I,
)
WEAK = re.compile(
    r"\b(?:NVIDIA|Nvidia|AMD|supercomputer|AI data[\s-]?cent(?:er|re)s?|"
    r"AI datacenters?|GPUs?)\b",
)
CONSUMER = re.compile(
    r"\b(?:GeForce(?:\s+NOW)?|RTX(?:\s+PRO)?(?:\s*\d+)?|gaming|"
    r"Fortnite|War Thunder|laptop|notebook|graphics card review)\b",
    re.I,
)
DATACENTER_ANCHOR = re.compile(
    r"\b(?:H100|H200|B100|B200|B300|GB200|GB300|A100|DGX|HGX|"
    r"MI300|MI325|MI350|data[\s-]?cent(?:er|re)|datacenter|"
    r"GPU cluster)\b",
    re.I,
)
# Low-signal finance/video blurbs, not useful as a GPU headline.
NOISE = re.compile(
    r"\b(?:Stock Movers|Watch Nvidia|a trader just placed|perps? with leverage|"
    r"Polymarket|perpetual trading)\b",
    re.I,
)

# Prefer original reporting over syndication of the same story.
SOURCE_RANK = {
    "bloomberg": 5, "reuters": 5, "cnbc": 5, "financial times": 5, "ft": 5,
    "wsj": 5, "wall street journal": 5, "associated press": 5, "ap news": 5,
    "bbc": 5, "bbc news": 5, "nikkei": 5, "nikkei asia": 5,
    "nvidia": 5, "nvidia newsroom": 5, "amd": 5, "federal register": 5,
    "tom's hardware": 4, "the register": 4, "ars technica": 4, "techcrunch": 4,
    "engadget": 4,
    "hpcwire": 4, "servethehome": 4, "data center dynamics": 4, "the verge": 4,
    "wired": 4, "wccftech": 3, "yahoo finance": 3, "the motley fool": 3,
    "quartz": 3, "gizmodo": 2, "the next web": 2,
}

KNOWN_OUTLETS = tuple(sorted((
    "Bloomberg", "Bloomberg.com", "Reuters", "CNBC", "TechCrunch",
    "Federal Register", "Engadget",
    "Tom's Hardware", "The Register", "HPCwire", "ServeTheHome",
    "Data Center Dynamics", "NVIDIA", "NVIDIA Developer", "NVIDIA Newsroom",
    "NVIDIA Technical Blog", "AMD", "Ars Technica", "Wired", "WIRED",
    "BBC", "BBC News", "Financial Times", "WSJ", "Wall Street Journal",
    "The Verge", "Yahoo Finance", "Investor's Business Daily",
    "Business Insider", "Forbes", "AP News", "Associated Press", "CNN",
    "South China Morning Post", "SCMP", "Nikkei", "Nikkei Asia",
    "The Motley Fool", "Wccftech", "Quartz", "Gizmodo", "The Next Web",
    "Benzinga", "Seeking Alpha", "24/7 Wall St.", "Superpower Daily",
    "Digg", "MLQ.ai", "WinBuzzer", "marketscreener.com", "ThePrint",
    "India Today", "The Edge Malaysia", "Coinpedia", "Crypto Briefing",
    "Businesskorea", "The Times of India", "The Defiant", "Traders Union",
    "TradingKey", "Quiver Quantitative", "Dataconomy", "SRN News",
    "Tech Times", "TweakTown", "HotHardware", "Longbridge", "DQ",
    "Notebookcheck", "TradingView", "The Daily Star", "MarkTechPost",
    "CoinDesk", "Cryptonews.net", "Blocks & Files", "W.Media",
    "Kotak Neo", "The Standard (HK)", "BigGo Finance", "Oninvest",
    "grafa.com", "Investing.com UK", "Investing UK", "explainx.ai",
    "Finance",
), key=len, reverse=True))

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
TICKER_RE = re.compile(r"\s*[-–—|]\s*NVIDIA\s*\((?:NASDAQ|NYSE):NVDA\)", re.I)
TICKER_BARE = re.compile(r"\s*\((?:NASDAQ|NYSE|AMEX):[A-Z.]+\)")
STOP = frozenset("""
    a an the and or but of to for in on at as by from with vs into over
    after before amid despite than about its his her their they this that
    these those just up out off not no new may will says say said report
    reports reportedly found turns turn tied linked using used
    nvidia nvidias amd gpu gpus chip chips ai
""".split())


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def host_of(url: str) -> str:
    try:
        h = (urllib.parse.urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def denied_host(url: str) -> bool:
    h = host_of(url)
    return any(h == d or h.endswith("." + d) for d in DENY_DOMAINS)


def clean_text(raw: str | None) -> str:
    s = html.unescape(raw or "")
    s = TAG_RE.sub(" ", s)
    s = WS_RE.sub(" ", s).strip()
    return s


def latin_letters_only(text: str) -> bool:
    """English-only public page: reject Hangul, CJK, Cyrillic, etc."""
    for ch in text:
        if not ch.isalpha():
            continue
        if not unicodedata.name(ch, "").startswith("LATIN"):
            return False
    return True


def public_ok(text: str) -> bool:
    if not text or not latin_letters_only(text):
        return False
    if SOSNOFF.search(text) or DENY_NAME_RE.search(text):
        return False
    return not any(p.search(text) for p in PUBLIC_BLOCK)


def strip_source_suffix(title: str, source: str) -> str:
    title = TICKER_RE.sub("", title)
    title = TICKER_BARE.sub("", title)
    if source:
        title = re.sub(
            rf"\s*[-–—|]\s*{re.escape(source)}\s*$",
            "",
            title,
            flags=re.I,
        )
    for outlet in KNOWN_OUTLETS:
        title = re.sub(
            rf"\s*[-–—|]\s*{re.escape(outlet)}\s*$",
            "",
            title,
            flags=re.I,
        )
    return WS_RE.sub(" ", title).strip(" -–—|")


def score_title(title: str) -> int:
    if NOISE.search(title):
        return 0
    if CONSUMER.search(title) and not DATACENTER_ANCHOR.search(title):
        return 0
    n = 0
    if STRONG.search(title):
        n += 3
    if MEDIUM.search(title):
        n += 2
    if WEAK.search(title):
        n += 1
    return n


def source_rank(source: str) -> int:
    key = re.sub(r"\.(com|co\.uk|ai|net|org)$", "", source.lower().strip())
    key = key.replace("www.", "")
    return SOURCE_RANK.get(key, 1)


def parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    s = raw.strip()
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def age_ok(dt: datetime, now: datetime) -> bool:
    return (now - dt).days <= MAX_AGE_DAYS


def child_text(el: ET.Element, *names: str) -> str:
    want = set(names)
    for child in list(el):
        local = child.tag.rsplit("}", 1)[-1]
        if local in want:
            href = child.get("href")
            if href and local == "link":
                return href.strip()
            if child.text and child.text.strip():
                return child.text.strip()
    return ""


def iter_entries(root: ET.Element):
    if root.tag.rsplit("}", 1)[-1] == "rss" or root.find("channel") is not None:
        channel = root.find("channel") if root.find("channel") is not None else root
        for item in channel.findall("item"):
            yield item
        return
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "entry":
            yield el


def parse_feed(xml_bytes: bytes, fallback_source: str) -> list[dict]:
    out = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        log(f"  parse error: {e}")
        return out
    for el in iter_entries(root):
        title = clean_text(child_text(el, "title"))
        link = child_text(el, "link")
        if not link:
            for child in list(el):
                if child.tag.rsplit("}", 1)[-1] == "link":
                    link = (child.get("href") or child.text or "").strip()
                    if link:
                        break
        pub = child_text(el, "pubDate", "published", "updated", "date")
        source = fallback_source
        for child in list(el):
            if child.tag.rsplit("}", 1)[-1] == "source" and child.text:
                source = clean_text(child.text)
                break
        source = re.sub(r"\.(com|co\.uk)$", "", source, flags=re.I).strip() or fallback_source
        if title and link:
            out.append({"title": title, "url": link, "source": source, "pub": pub})
    return out


def fetch(url: str) -> bytes | None:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        },
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            status = getattr(resp, "status", 200)
            data = resp.read()
            if status != 200:
                log(f"  HTTP {status}; skipped")
                return None
            head = data.lstrip()[:200].lower()
            if b"<rss" not in head and b"<feed" not in head and b"<?xml" not in head:
                log("  not XML (looks like HTML); skipped")
                return None
            return data
    except urllib.error.HTTPError as e:
        log(f"  HTTP {e.code}; skipped")
        return None
    except Exception as e:
        log(f"  {type(e).__name__}: {e}; skipped")
        return None


# Distinctive story entities: same place/org/event + a GPU model => one story.
STORY_ENTITIES = (
    "shanghai", "shenzhen", "huaqiangbei", "alice huang", "megaspeed",
    "spacex", "anthropic", "southeast asia", "singapore", "malaysia",
    "indonesia", "thailand", "polymarket",
    "vera rubin", "sharon ai", "chennai", "ltn compute", "oak ridge",
    "lead time", "40-week", "40 week", "for sale", "black market",
    "markup", "resale",
)
STORY_MODELS = (
    "h100", "h200", "b100", "b200", "b300", "gb200", "gb300", "a100",
    "blackwell", "mi300", "mi325", "mi350", "hopper", "instinct",
)


# Aliases for one event, so rewrites that name different details still cluster.
ENTITY_GROUPS = (
    frozenset({"shanghai", "alice huang"}),
    frozenset({"shenzhen", "huaqiangbei", "for sale", "black market", "markup", "resale"}),
    frozenset({"spacex", "anthropic"}),
    frozenset({"southeast asia", "singapore", "malaysia", "indonesia", "thailand"}),
    frozenset({"lead time", "40-week", "40 week"}),
    frozenset({"chennai", "ltn compute", "sharon ai"}),
)


def story_entities(title: str) -> frozenset[str]:
    t = title.lower()
    found = {e for e in STORY_ENTITIES if e in t}
    # "Huang" alone is ambiguous (Jensen vs Alice).
    if "alice" in t:
        found.add("alice huang")
    if any(s in t for s in ("southeast asia", "malaysia", "indonesia", "thailand")):
        found.add("southeast asia")
    for group in ENTITY_GROUPS:
        if found & group:
            found.add("group:" + sorted(group)[0])
    return frozenset(found)


def story_models(title: str) -> frozenset[str]:
    t = title.lower()
    return frozenset(m for m in STORY_MODELS if re.search(rf"\b{re.escape(m)}\b", t))


def tokens(title: str) -> frozenset[str]:
    words = re.findall(r"[a-z0-9]+", title.lower())
    return frozenset(w for w in words if len(w) >= 4 and w not in STOP)


def similar(a: str, b: str) -> bool:
    ea, eb = story_entities(a), story_entities(b)
    ma, mb = story_models(a), story_models(b)
    if ea and eb and (ea & eb) and (ma & mb or not ma or not mb):
        return True
    # Rewrites of the same "model + China" story often omit the place/person.
    def mentions_china(s: str) -> bool:
        return bool(re.search(r"\b(?:china|chinese)\b", s, re.I))
    if (ma & mb) and mentions_china(a) and mentions_china(b):
        if (not ea or not eb) or (ea & eb):
            return True
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return False
    inter = len(ta & tb)
    union = len(ta | tb)
    if inter >= 3 and union and inter / union >= 0.4:
        return True
    if inter >= 3 and (inter / len(ta) >= 0.7 or inter / len(tb) >= 0.7):
        return True
    return False


def pick_best(cluster: list[dict]) -> dict:
    def key(it: dict):
        return (it.get("pin", 0), source_rank(it["source"]), it["dt"], it["score"])
    return max(cluster, key=key)


def cluster_items(items: list[dict]) -> list[dict]:
    # Pinned official / specified items always keep their own slot; RSS
    # duplicates of a pin may be absorbed, but two pins are never merged.
    clusters: list[list[dict]] = [[it] for it in items if it.get("pin")]
    for it in items:
        if it.get("pin"):
            continue
        placed = False
        for c in clusters:
            if similar(it["title"], c[0]["title"]):
                c.append(it)
                placed = True
                break
        if not placed:
            clusters.append([it])
    picked = [pick_best(c) for c in clusters]
    picked.sort(key=lambda it: it["dt"], reverse=True)
    return picked


def collect(now: datetime) -> list[dict]:
    seen_title: set[str] = set()
    seen_url: set[str] = set()
    items: list[dict] = []
    for feed in FEEDS:
        name, url = feed["name"], feed["url"]
        log(f"feed: {name}")
        raw = fetch(url)
        if not raw:
            continue
        entries = parse_feed(raw, name)
        log(f"  {len(entries)} entries")
        for e in entries:
            source = e["source"]
            title = strip_source_suffix(e["title"], source)
            if not title or not public_ok(title) or not public_ok(source):
                continue
            link = e["url"]
            if not re.match(r"^https?://", link, re.I):
                continue
            if denied_host(link):
                continue
            sc = score_title(title)
            if sc < 3:
                continue
            dt = parse_date(e.get("pub"))
            if dt is None or not age_ok(dt, now):
                continue
            nt = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
            nu = host_of(link) + urllib.parse.urlparse(link).path.rstrip("/")
            if nt in seen_title or nu in seen_url:
                continue
            seen_title.add(nt)
            seen_url.add(nu)
            items.append({
                "title": title,
                "source": source,
                "date_pt": dt.astimezone(TZ).date().isoformat(),
                "url": link,
                "score": sc,
                "dt": dt,
            })
    items.extend(fetch_pinned(now))
    unique = cluster_items(items)
    unique.sort(key=lambda it: (it.get("pin", 0), it["dt"].timestamp()), reverse=True)
    preferred = [it for it in unique if it.get("pin") or source_rank(it["source"]) >= 3]
    if len(preferred) >= 5:
        unique = preferred
        unique.sort(key=lambda it: (it.get("pin", 0), it["dt"].timestamp()), reverse=True)
    out = []
    for it in unique[:MAX_ITEMS]:
        out.append({
            "title": shorten(it["title"]),
            "source": it["source"],
            "date_pt": it["date_pt"],
            "url": it["url"],
        })
    return out


def shorten(title: str, limit: int = 140) -> str:
    if len(title) <= limit:
        return title
    cut = title[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:.--") + "..."


def http_get(url: str, accept: str) -> tuple[int, bytes] | tuple[None, None]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": accept},
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            status = getattr(resp, "status", 200)
            data = resp.read()
            if status != 200:
                log(f"  HTTP {status} for {url}; skipped")
                return None, None
            return status, data
    except urllib.error.HTTPError as e:
        log(f"  HTTP {e.code} for {url}; skipped")
        return None, None
    except Exception as e:
        log(f"  {type(e).__name__}: {e} for {url}; skipped")
        return None, None


def html_attr(html: str, *patterns: str) -> str:
    for pat in patterns:
        m = re.search(pat, html, re.I | re.S)
        if m:
            return clean_text(m.group(1))
    return ""


def fetch_pinned(now: datetime) -> list[dict]:
    """Official / specified items, with headlines taken from the source pages."""
    out: list[dict] = []

    # Official Federal Register documents. Titles and publication dates come
    # from the public FR API only (never invented). Newest official first.
    # 2026-23999: Entity List additions.
    # 2026-23529: Addressing Remote Access to Advanced Semiconductors.
    for doc_number in ("2026-23999", "2026-23529"):
        item = fetch_fr_document(doc_number, now)
        if item:
            out.append(item)

    # Other specified pages: real <h1>/og:title only; skip if blocked or empty.
    for source, url, pin in (
        (
            "Engadget",
            "https://www.engadget.com/2329200/nvidia-reportedly-halts-chip-shipments-to-southeast-asia/",
            1,
        ),
        (
            "CNBC",
            "https://www.cnbc.com/2026/11/24/us-adds-megaspeed-to-trade-blacklist-over-export-control-concerns-.html",
            1,
        ),
    ):
        item = fetch_pinned_page(source, url, pin, now)
        if item:
            out.append(item)
    return out


def fetch_fr_document(doc_number: str, now: datetime) -> dict | None:
    log(f"pinned: Federal Register {doc_number}")
    api = (
        f"https://www.federalregister.gov/api/v1/documents/{doc_number}.json"
        "?fields[]=title&fields[]=publication_date&fields[]=html_url"
        "&fields[]=document_number"
    )
    _, raw = http_get(api, "application/json")
    if not raw:
        return None
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        log(f"  FR API parse error: {e}; skipped")
        return None
    title = clean_text(doc.get("title") or "")
    link = doc.get("html_url") or ""
    pub = doc.get("publication_date") or ""
    if not (title and public_ok(title) and re.match(r"^https?://", link, re.I)):
        log("  FR item missing a usable official title or URL; skipped")
        return None
    # Official FR calendar date; do not shift it by UTC-midnight conversion.
    try:
        dt = datetime.strptime(pub, "%Y-%m-%d").replace(hour=12, tzinfo=TZ)
    except ValueError:
        log("  FR item missing a usable publication date; skipped")
        return None
    if not age_ok(dt, now):
        log(f"  FR {doc_number} is older than {MAX_AGE_DAYS} days; skipped")
        return None
    log(f"  {pub} PT | Federal Register | {title}")
    return {
        "title": title,
        "source": "Federal Register",
        "date_pt": pub,
        "url": link,
        "score": 5,
        "dt": dt,
        "pin": 3,
    }


def fetch_pinned_page(source: str, url: str, pin: int, now: datetime) -> dict | None:
    log(f"pinned: {source}")
    _, raw = http_get(url, "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8")
    if not raw:
        return None
    html = raw.decode("utf-8", errors="replace")
    title = html_attr(
        html,
        r"<h1[^>]*>(.*?)</h1>",
        r'property="og:title" content="([^"]+)"',
        r"<title[^>]*>(.*?)</title>",
    )
    title = strip_source_suffix(title, source)
    pub = html_attr(
        html,
        r'property="article:published_time" content="([^"]+)"',
        r'"datePublished"\s*:\s*"([^"]+)"',
        r'<time[^>]*datetime="([^"]+)"',
    )
    dt = parse_date(pub)
    if not (title and public_ok(title) and dt and age_ok(dt, now)):
        log(f"  {source} headline/date not found; skipped")
        return None
    date_pt = dt.astimezone(TZ).date().isoformat()
    log(f"  {date_pt} PT | {source} | {title}")
    return {
        "title": title,
        "source": source,
        "date_pt": date_pt,
        "url": url,
        "score": 5,
        "dt": dt,
        "pin": pin,
    }


def write_news(path: Path, items: list[dict], now: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone(TZ).strftime("%Y-%m-%dT%H:%M:%S%z")
    if len(stamp) >= 2 and stamp[-5] in "+-" and ":" not in stamp[-5:]:
        stamp = stamp[:-2] + ":" + stamp[-2:]
    payload = {
        "schema_version": 1,
        "updated_at_pt": stamp,
        "items": items,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", default=str(DEFAULT_OUT), help="path to news.json")
    args = ap.parse_args(argv)
    out = Path(args.output)
    now = datetime.now(timezone.utc)
    try:
        items = collect(now)
    except Exception as e:
        log(f"fetch_news failed: {type(e).__name__}: {e}; keeping existing file")
        return 0
    if not items:
        log("no qualifying headlines; keeping existing news.json")
        return 0
    try:
        write_news(out, items, now)
    except Exception as e:
        log(f"write failed: {type(e).__name__}: {e}; keeping existing file")
        return 0
    log(f"wrote {out} ({len(items)} headlines)")
    for it in items:
        log(f"  {it['date_pt']} PT | {it['source']} | {it['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
