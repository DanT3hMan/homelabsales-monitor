#!/usr/bin/env python3
"""
r/homelabsales monitor - SATA HDD deal notifier via Discord

Filters applied (in order):
  1. Title must contain [FS]  — skips [W] Want and [PC] Price Check
  2. Flair must be US-E, US-W, or US-C  — skips EU, CAN, COMPLETE, etc.
  3. Must look like a SATA HDD  — skips SAS drives, SSDs, NVMe
  4. $/TB must be under PRICE_PER_TB_THRESHOLD (if price is parseable)

Setup:
  1. pip install requests
  2. Discord: Server Settings > Integrations > Webhooks > New Webhook, copy URL
  3. cp .env.example .env && nano .env   (paste your webhook URL)
  4. python3 monitor.py                  (test run - safe, won't re-notify)
  5. Add to cron (see bottom of this file)

Requires Python 3.10+
"""

import json
import os
import re
import sys
from pathlib import Path

import praw
import requests

# ── Config ─────────────────────────────────────────────────────────────────────

DISCORD_WEBHOOK      = os.environ.get("DISCORD_WEBHOOK_URL", "")
REDDIT_CLIENT_ID     = os.environ.get("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "")
SEEN_FILE  = Path(__file__).parent / "seen_posts.json"
SUBREDDIT  = "homelabsales"
FETCH_LIMIT = 50
USER_AGENT = "homelabsales-monitor/1.0 (by /u/DanT3hMan)"

# Maximum $/TB to trigger a notification. Set to None to disable price filtering
# and notify on all matching posts regardless of price.
PRICE_PER_TB_THRESHOLD: float | None = 25.0


# ── Post-type filter ───────────────────────────────────────────────────────────

def is_fs_post(title: str) -> bool:
    """Accept [FS] posts only. Rejects [W], [PC], [MOD], etc."""
    return bool(re.search(r'\[FS\]', title, re.IGNORECASE))

# ── Location filter ────────────────────────────────────────────────────────────

def is_us_flair(flair: str | None) -> bool:
    """
    Accept US-E, US-W, US-C.
    Reject EU, CAN, COMPLETE, INT, and anything else.
    Posts with no flair are included (may not be flaired yet).
    """
    if not flair:
        return True
    # Accept any US flair variant (US-E, US-W, US-C, US-MI, US-AUT, USA-VA, etc.)
    # Rejects EU, CAN, COMPLETE, SGP, Other, and anything else non-US
    f = flair.strip().lower()
    return f.startswith("us-") or f.startswith("usa-")

# ── SATA HDD filter ────────────────────────────────────────────────────────────

# Hard disqualifiers — no SATA redemption possible
_HARD_EXCLUDE_PATTERNS = [
    r'\bssds?\b',
    r'\b(nvme|solid[\s\-]?state|nand)\b',
    r'\bm\.2\b',
    r'\bu\.2\b',
]

# At least one of these must appear in the title
_HDD_PATTERNS = [
    r'\bhdds?\b',                        # HDD or HDDs
    r'hard[\s\-]?drives?',
    r'hard[\s\-]?disks?',
    r'\bsata\b',
    # Seagate SATA lines (Exos comes in SATA and SAS, but SAS check below catches SAS-only posts)
    r'barracuda',
    r'ironwolf',
    r'skyhawk',
    r'\bexos\b',
    # WD — both shorthand and written out ("WD Red", "Western Digital Red Pro", etc.)
    r'wd[\s\-]red',
    r'wd[\s\-]gold',
    r'wd[\s\-]purple',
    r'wd[\s\-]blue',
    r'wd[\s\-]green',
    r'wd[\s\-]se\b',
    r'western[\s\-]digital',            # catches "Western Digital Red", "Western Digital Gold", etc.
    # Other HDD manufacturers
    r'\bhgst\b',
    r'hitachi',
    r'toshiba[\s\-]+(?:n|x|p|mg)\d',   # Toshiba NAS/enterprise (N300, X300, MG series)
    # 3.5" form factor is a strong HDD signal in this subreddit
    r'3\.5\s*(?:"|in\b|inch)',
]

def is_sata_hdd(title: str) -> tuple[bool, str]:
    """
    Returns (matches, skip_reason).
    skip_reason is non-empty when matches is False.

    SAS handling:
    - Pure SAS post (SAS mentioned, no SATA indicators) → excluded
    - Mixed lot (SAS + SATA indicators both present) → included, flagged as mixed
    """
    t = title.lower()

    for pat in _HARD_EXCLUDE_PATTERNS:
        if re.search(pat, t):
            label = re.search(pat, t).group()
            return False, f"excluded keyword: {label}"

    has_sata = any(re.search(p, t) for p in _HDD_PATTERNS)
    has_sas = bool(re.search(r'\bsas\b', t))

    if has_sas and not has_sata:
        return False, "SAS drive (no SATA indicators)"

    if has_sata:
        # Mixed lot: has SAS but also SATA indicators — notify but flag it
        warning = "⚠️ mixed SAS+SATA lot" if has_sas else ""
        return True, warning

    return False, "no SATA HDD keyword matched"

# ── Price analysis ─────────────────────────────────────────────────────────────

def _to_tb(val: float, unit: str) -> float:
    match unit.upper():
        case 'GB': return val / 1000
        case 'PB': return val * 1000
        case _:    return val  # already TB

def _parse_line(line: str) -> list[dict]:
    """
    Extract (capacity, price) pairs from a single line of text.
    Returns a list of dicts with cap_tb, cap_str, price, per_tb.
    Handles quantity prefixes like '4x 8TB'.
    """
    # Capacities, with optional leading quantity multiplier (e.g. "4x 8TB")
    cap_pattern = re.compile(
        r'(?:(\d+)\s*[xX×]\s*)?(\d+(?:\.\d+)?)\s*(TB|GB)', re.IGNORECASE
    )
    price_pattern = re.compile(r'\$\s*([\d,]+(?:\.\d{1,2})?)')

    caps = []
    for m in cap_pattern.finditer(line):
        qty   = int(m.group(1)) if m.group(1) else 1
        each  = _to_tb(float(m.group(2)), m.group(3))
        total = each * qty
        if total < 0.25:          # skip cache/buffer specs
            continue
        caps.append({'pos': m.start(), 'tb': total, 'str': m.group().strip()})

    prices = []
    for m in price_pattern.finditer(line):
        prices.append({'pos': m.start(), 'val': float(m.group(1).replace(',', ''))})

    if not caps or not prices:
        return []

    # Pair positionally when counts match, otherwise nearest-neighbour
    pairs = []
    if len(caps) == len(prices):
        for cap, price in zip(caps, prices):
            pairs.append((cap, price))
    else:
        used = set()
        for cap in caps:
            best = min(
                ((i, abs(cap['pos'] - p['pos']), p) for i, p in enumerate(prices) if i not in used),
                key=lambda x: x[1],
                default=None,
            )
            if best:
                i, _, price = best
                used.add(i)
                pairs.append((cap, price))

    results = []
    for cap, price in pairs:
        per_tb = price['val'] / cap['tb']
        results.append({
            'cap_tb':  cap['tb'],
            'cap_str': cap['str'],
            'price':   price['val'],
            'per_tb':  per_tb,
        })
    return results


def extract_listings(title: str, body: str) -> list[dict]:
    """
    Walk the post line by line (body first, then title) and collect
    (capacity, price) pairs. Returns a deduplicated list sorted by capacity.
    """
    seen  = set()
    found = []

    lines = (body or '').splitlines() + [title]
    for line in lines:
        line = line.strip()
        if not line:
            continue
        for entry in _parse_line(line):
            key = (round(entry['cap_tb'], 2), entry['price'])
            if key not in seen:
                seen.add(key)
                found.append(entry)

    return sorted(found, key=lambda x: x['cap_tb'])


def analyze_price(title: str, body: str) -> tuple[float | None, str]:
    """
    Returns (best_per_tb, display_label).
    best_per_tb is the lowest $/TB among all listings (used for threshold check).
    display_label is formatted for the Discord embed — one line per listing.
    """
    listings = extract_listings(title, body)

    if not listings:
        return None, "price/capacity unclear — check post"

    if len(listings) == 1:
        l = listings[0]
        return l['per_tb'], f"${l['price']:.0f} / {l['cap_str']} = **${l['per_tb']:.2f}/TB**"

    lines = [
        f"{l['cap_str']}: ${l['price']:.0f} = **${l['per_tb']:.2f}/TB**"
        for l in listings
    ]
    best_per_tb = min(l['per_tb'] for l in listings)
    return best_per_tb, "\n".join(lines)

# ── Seen-post tracking ─────────────────────────────────────────────────────────

def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except (json.JSONDecodeError, ValueError):
            return set()
    return set()

def save_seen(seen: set):
    ids = list(seen)[-5000:]  # cap so the file doesn't grow forever
    SEEN_FILE.write_text(json.dumps(ids))

# ── Reddit ─────────────────────────────────────────────────────────────────────

def fetch_new_posts() -> list[dict]:
    """
    Fetch new posts via the official Reddit OAuth API (PRAW).
    Returns a list of plain dicts so the rest of the script is unchanged.
    Uses client_credentials (no user login needed for public subreddits).
    """
    reddit = praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        user_agent=USER_AGENT,
    )
    posts = []
    for post in reddit.subreddit(SUBREDDIT).new(limit=FETCH_LIMIT):
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

def _embed_color(price_per_tb: float | None) -> int:
    """Color-code by deal quality."""
    if price_per_tb is None:
        return 0x808080   # gray  — unknown
    if price_per_tb <= 12:
        return 0x00cc44   # green — great deal
    if price_per_tb <= 20:
        return 0xf5a623   # orange — decent
    return 0x00b0f4       # blue — borderline / ok

def send_discord(post: dict, price_per_tb: float | None, price_label: str, warning: str = ""):
    title = post["title"]
    url = f"https://www.reddit.com{post['permalink']}"
    author = post.get("author", "[deleted]")
    flair = post.get("link_flair_text") or "no flair"
    body = post.get("selftext", "")

    fields = [
        {"name": "Location", "value": flair, "inline": True},
        {"name": "Seller", "value": f"u/{author}", "inline": True},
    ]
    if price_label:
        fields.append({"name": "Price", "value": price_label, "inline": False})
    if warning:
        fields.append({"name": "Note", "value": warning, "inline": False})

    embed: dict = {
        "title": title[:256],
        "url": url,
        "color": _embed_color(price_per_tb),
        "fields": fields,
        "footer": {"text": f"r/{SUBREDDIT}"},
    }
    if body.strip():
        snippet = body.strip()[:350]
        if len(body) > 350:
            snippet += "…"
        embed["description"] = snippet

    requests.post(DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=10).raise_for_status()

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    missing = [k for k, v in {
        "DISCORD_WEBHOOK_URL": DISCORD_WEBHOOK,
        "REDDIT_CLIENT_ID":    REDDIT_CLIENT_ID,
        "REDDIT_CLIENT_SECRET": REDDIT_CLIENT_SECRET,
    }.items() if not v]
    if missing:
        for k in missing:
            print(f"ERROR: {k} is not set.", file=sys.stderr)
        sys.exit(1)

    seen = load_seen()

    try:
        posts = fetch_new_posts()
    except requests.RequestException as e:
        print(f"ERROR fetching Reddit: {e}", file=sys.stderr)
        sys.exit(1)

    new_seen = set(seen)
    notified = 0

    for item in posts:
        post = item
        post_id = post["id"]

        if post_id in seen:
            continue
        new_seen.add(post_id)

        title = post["title"]
        flair = post.get("link_flair_text", "")
        body = post.get("selftext", "")

        # ── Filter chain ──────────────────────────────────────────────────────
        if not is_fs_post(title):
            continue

        if not is_us_flair(flair):
            print(f"  Skip (flair={flair!r}): {title[:60]}")
            continue

        match, warning = is_sata_hdd(title)
        if not match:
            continue  # silent skip — most posts won't be HDDs

        price_per_tb, price_label = analyze_price(title, body)

        if PRICE_PER_TB_THRESHOLD is not None and price_per_tb is not None:
            if price_per_tb > PRICE_PER_TB_THRESHOLD:
                print(f"  Skip (${price_per_tb:.2f}/TB > ${PRICE_PER_TB_THRESHOLD}/TB): {title[:60]}")
                continue

        # ── Send ──────────────────────────────────────────────────────────────
        try:
            send_discord(post, price_per_tb, price_label, warning)
            notified += 1
            tag = price_label if price_label else "price unknown"
            print(f"  Notified [{tag}]: {title[:70]}")
        except requests.RequestException as e:
            print(f"  ERROR sending Discord webhook: {e}", file=sys.stderr)

    save_seen(new_seen)
    print(f"Done. Checked {len(posts)} posts, sent {notified} notification(s).")


if __name__ == "__main__":
    main()

# ── Cron setup ─────────────────────────────────────────────────────────────────
#
# Option A — inline env var in crontab (simplest):
#   */5 * * * * DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/...' /usr/bin/python3 /opt/homelabsales-monitor/monitor.py 2>&1 | logger -t homelabsales
#
# Option B — source .env file:
#   */5 * * * * cd /opt/homelabsales-monitor && export $(cat .env | xargs) && /usr/bin/python3 monitor.py 2>&1 | logger -t homelabsales
#
# Option C — systemd timer (use included .service + .timer files):
#   sudo cp homelabsales.{service,timer} /etc/systemd/system/
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now homelabsales.timer
#   sudo journalctl -u homelabsales.service -f
