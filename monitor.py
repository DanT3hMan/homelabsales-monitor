#!/usr/bin/env python3
"""
Deal monitor — watches multiple subreddits and notifies via Discord.

Subreddits watched:
  r/homelabsales  — SATA HDD deals under $25/TB, US only
  r/buildapcsales — AMD RX 9070 XT posts

Requires Python 3.10+
"""

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import praw
import requests

# ── Credentials / global config ────────────────────────────────────────────────

DISCORD_WEBHOOK      = os.environ.get("DISCORD_WEBHOOK_URL", "")
REDDIT_CLIENT_ID     = os.environ.get("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "")
SEEN_FILE   = Path(__file__).parent / "seen_posts.json"
FETCH_LIMIT = 50
USER_AGENT  = "deal-monitor/1.0 (by /u/DanT3hMan)"

# ══════════════════════════════════════════════════════════════════════════════
# r/homelabsales — SATA HDD filter
# ══════════════════════════════════════════════════════════════════════════════

# Maximum $/TB to trigger a notification. Set to None to disable price filtering.
HDD_PRICE_PER_TB_MAX: float | None = 25.0

_HDD_HARD_EXCLUDE = [
    r'\bnvme\b',
    r'\bnand\b',
    r'\bm\.2\b',
    r'\bu\.2\b',
]

_HDD_SOFT_EXCLUDE = [          # skip unless HDD indicators are also present
    r'\bssds?\b',
    r'solid[\s\-]?state',
]

_HDD_INCLUDE = [
    r'\bhdds?\b',
    r'hard[\s\-]?drives?',
    r'hard[\s\-]?disks?',
    r'\bsata\b',
    r'barracuda', r'ironwolf', r'skyhawk', r'\bexos\b',
    r'wd[\s\-]red', r'wd[\s\-]gold', r'wd[\s\-]purple',
    r'wd[\s\-]blue', r'wd[\s\-]green', r'wd[\s\-]se\b',
    r'western[\s\-]digital',
    r'\bhgst\b', r'hitachi',
    r'toshiba[\s\-]+(?:n|x|p|mg)\d',
    r'3\.5\s*(?:"|in\b|inch)',
]

def _is_fs_post(title: str) -> bool:
    return bool(re.search(r'\[FS\]', title, re.IGNORECASE))

def _is_us_flair(flair: str | None) -> bool:
    if not flair:
        return True
    f = flair.strip().lower()
    return f.startswith("us-") or f.startswith("usa-")

def _is_sata_hdd(title: str) -> tuple[bool, str]:
    t = title.lower()
    for pat in _HDD_HARD_EXCLUDE:
        if re.search(pat, t):
            return False, ""
    has_hdd = any(re.search(p, t) for p in _HDD_INCLUDE)
    has_sas = bool(re.search(r'\bsas\b', t))
    has_ssd = any(re.search(p, t) for p in _HDD_SOFT_EXCLUDE)
    if (has_sas or has_ssd) and not has_hdd:
        return False, ""
    if has_hdd:
        mixed = [x for x, y in [("SAS", has_sas), ("SSD", has_ssd)] if y]
        warning = f"⚠️ mixed lot — also contains: {', '.join(mixed)}" if mixed else ""
        return True, warning
    return False, ""

# ── HDD price analysis ─────────────────────────────────────────────────────────

def _to_tb(val: float, unit: str) -> float:
    match unit.upper():
        case 'GB': return val / 1000
        case 'PB': return val * 1000
        case _:    return val

def _parse_line_for_hdd(line: str) -> list[dict]:
    cap_re   = re.compile(r'(?:(\d+)\s*[xX×]\s*)?(\d+(?:\.\d+)?)\s*(TB|GB)', re.IGNORECASE)
    price_re = re.compile(r'\$\s*([\d,]+(?:\.\d{1,2})?)')

    caps = []
    for m in cap_re.finditer(line):
        qty = int(m.group(1)) if m.group(1) else 1
        tb  = _to_tb(float(m.group(2)), m.group(3)) * qty
        if tb >= 0.25:
            caps.append({'pos': m.start(), 'tb': tb, 'str': m.group().strip()})

    prices = [{'pos': m.start(), 'val': float(m.group(1).replace(',', ''))}
              for m in price_re.finditer(line)]

    if not caps or not prices:
        return []

    pairs = []
    if len(caps) == len(prices):
        pairs = list(zip(caps, prices))
    else:
        used = set()
        for cap in caps:
            best = min(
                ((i, abs(cap['pos'] - p['pos']), p) for i, p in enumerate(prices) if i not in used),
                key=lambda x: x[1], default=None,
            )
            if best:
                used.add(best[0])
                pairs.append((cap, best[2]))

    return [{'cap_tb': c['tb'], 'cap_str': c['str'], 'price': p['val'],
             'per_tb': p['val'] / c['tb']} for c, p in pairs]

def _hdd_price_label(title: str, body: str) -> tuple[float | None, str]:
    seen, found = set(), []
    for line in (body or '').splitlines() + [title]:
        for e in _parse_line_for_hdd(line.strip()):
            key = (round(e['cap_tb'], 2), e['price'])
            if key not in seen:
                seen.add(key)
                found.append(e)
    found.sort(key=lambda x: x['cap_tb'])

    if not found:
        return None, "price/capacity unclear — check post"
    if len(found) == 1:
        e = found[0]
        return e['per_tb'], f"${e['price']:.0f} / {e['cap_str']} = **${e['per_tb']:.2f}/TB**"
    lines = [f"{e['cap_str']}: ${e['price']:.0f} = **${e['per_tb']:.2f}/TB**" for e in found]
    return min(e['per_tb'] for e in found), "\n".join(lines)

# ── homelabsales filter entry point ───────────────────────────────────────────

def filter_homelabsales(post: dict) -> tuple[bool, float | None, str, str]:
    """Returns (notify, sort_value, price_label, warning)."""
    title = post["title"]
    flair = post.get("link_flair_text", "")
    body  = post.get("selftext", "")

    if not _is_fs_post(title):
        return False, None, "", ""
    if not _is_us_flair(flair):
        return False, None, "", ""

    match, warning = _is_sata_hdd(title)
    if not match:
        return False, None, "", ""

    price_per_tb, price_label = _hdd_price_label(title, body)

    if HDD_PRICE_PER_TB_MAX is not None and price_per_tb is not None:
        if price_per_tb > HDD_PRICE_PER_TB_MAX:
            return False, None, "", ""

    return True, price_per_tb, price_label, warning

def hdd_embed_color(price_per_tb: float | None) -> int:
    if price_per_tb is None: return 0x808080
    if price_per_tb <= 12:   return 0x00cc44   # green — great
    if price_per_tb <= 20:   return 0xf5a623   # orange — decent
    return 0x00b0f4                             # blue — ok

# ══════════════════════════════════════════════════════════════════════════════
# r/buildapcsales — RX 9070 XT filter
# ══════════════════════════════════════════════════════════════════════════════

# Notify on any 9070 XT post at or below this price. Set to None for no limit.
GPU_PRICE_MAX: float | None = None

_9070XT_PATTERNS = [
    r'9070\s*xt',           # "9070 XT", "9070XT"
    r'rx\s*9070\s*xt',      # "RX 9070 XT"
    r'radeon\s*9070\s*xt',  # "Radeon 9070 XT"
]

def _is_9070xt(title: str) -> bool:
    t = title.lower()
    return any(re.search(p, t) for p in _9070XT_PATTERNS)

def _is_active_deal(flair: str | None) -> bool:
    if not flair:
        return True
    return flair.strip().lower() not in {"expired", "complete"}

def _gpu_price(title: str) -> tuple[float | None, str]:
    m = re.search(r'\$\s*([\d,]+(?:\.\d{1,2})?)', title)
    if m:
        price = float(m.group(1).replace(',', ''))
        return price, f"**${price:.2f}**"
    return None, "check post for price"

def filter_buildapcsales(post: dict) -> tuple[bool, float | None, str, str]:
    """Returns (notify, sort_value, price_label, warning)."""
    title = post["title"]
    flair = post.get("link_flair_text", "")

    if not _is_9070xt(title):
        return False, None, "", ""
    if not _is_active_deal(flair):
        return False, None, "", ""

    price, price_label = _gpu_price(title)

    if GPU_PRICE_MAX is not None and price is not None:
        if price > GPU_PRICE_MAX:
            return False, None, "", ""

    return True, price, price_label, ""

def gpu_embed_color(price: float | None) -> int:
    if price is None:    return 0x808080
    if price <= 549:     return 0x00cc44   # green — below MSRP
    if price <= 649:     return 0xf5a623   # orange — around MSRP
    return 0xe74c3c                        # red — above MSRP

# ══════════════════════════════════════════════════════════════════════════════
# Monitor registry — add/remove subreddits here
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Monitor:
    subreddit:  str
    label:      str
    filter_fn:  Callable[[dict], tuple[bool, float | None, str, str]]
    color_fn:   Callable[[float | None], int]

MONITORS: list[Monitor] = [
    Monitor(
        subreddit = "homelabsales",
        label     = "HDD Deal",
        filter_fn = filter_homelabsales,
        color_fn  = hdd_embed_color,
    ),
    Monitor(
        subreddit = "buildapcsales",
        label     = "GPU Deal",
        filter_fn = filter_buildapcsales,
        color_fn  = gpu_embed_color,
    ),
]

# ── Seen-post tracking ─────────────────────────────────────────────────────────

def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except (json.JSONDecodeError, ValueError):
            return set()
    return set()

def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(list(seen)[-5000:]))

# ── Reddit ─────────────────────────────────────────────────────────────────────

def make_reddit() -> praw.Reddit:
    return praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        user_agent=USER_AGENT,
    )

def fetch_posts(reddit: praw.Reddit, subreddit: str) -> list[dict]:
    posts = []
    for post in reddit.subreddit(subreddit).new(limit=FETCH_LIMIT):
        posts.append({
            "id":              post.id,
            "title":           post.title,
            "link_flair_text": post.link_flair_text,
            "selftext":        post.selftext,
            "author":          post.author.name if post.author else "[deleted]",
            "permalink":       post.permalink,
        })
    return posts

# ── Discord ────────────────────────────────────────────────────────────────────

def send_discord(post: dict, monitor: Monitor, price_val: float | None,
                 price_label: str, warning: str):
    title  = post["title"]
    url    = f"https://www.reddit.com{post['permalink']}"
    author = post.get("author", "[deleted]")
    flair  = post.get("link_flair_text") or "—"
    body   = post.get("selftext", "")

    fields = [{"name": "Flair",  "value": flair,           "inline": True},
              {"name": "Posted", "value": f"u/{author}",   "inline": True}]
    if price_label:
        fields.append({"name": "Price", "value": price_label, "inline": False})
    if warning:
        fields.append({"name": "Note",  "value": warning,    "inline": False})

    embed: dict = {
        "title":  title[:256],
        "url":    url,
        "color":  monitor.color_fn(price_val),
        "fields": fields,
        "footer": {"text": f"r/{monitor.subreddit} • {monitor.label}"},
    }
    if body.strip():
        snippet = body.strip()[:350]
        embed["description"] = snippet + ("…" if len(body) > 350 else "")

    requests.post(DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=10).raise_for_status()

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    missing = [k for k, v in {
        "DISCORD_WEBHOOK_URL":  DISCORD_WEBHOOK,
        "REDDIT_CLIENT_ID":     REDDIT_CLIENT_ID,
        "REDDIT_CLIENT_SECRET": REDDIT_CLIENT_SECRET,
    }.items() if not v]
    if missing:
        for k in missing:
            print(f"ERROR: {k} is not set.", file=sys.stderr)
        sys.exit(1)

    seen     = load_seen()
    new_seen = set(seen)
    reddit   = make_reddit()
    total_notified = 0

    for monitor in MONITORS:
        print(f"\nr/{monitor.subreddit}:")
        try:
            posts = fetch_posts(reddit, monitor.subreddit)
        except Exception as e:
            print(f"  ERROR fetching r/{monitor.subreddit}: {e}", file=sys.stderr)
            continue

        notified = 0
        for post in posts:
            post_id = f"{monitor.subreddit}:{post['id']}"
            if post_id in seen:
                continue
            new_seen.add(post_id)

            notify, price_val, price_label, warning = monitor.filter_fn(post)
            if not notify:
                continue

            try:
                send_discord(post, monitor, price_val, price_label, warning)
                notified += 1
                print(f"  Notified: {post['title'][:75]}")
            except requests.RequestException as e:
                print(f"  ERROR sending webhook: {e}", file=sys.stderr)

        print(f"  Checked {len(posts)} posts, sent {notified} notification(s).")
        total_notified += notified

    save_seen(new_seen)
    print(f"\nDone. {total_notified} total notification(s) sent.")


if __name__ == "__main__":
    main()
