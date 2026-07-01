"""Quick filter sanity check — run with: python test_filters.py"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

# Stub credentials so monitor.py imports without erroring
os.environ.setdefault("DISCORD_WEBHOOK_URL",  "stub")
os.environ.setdefault("REDDIT_CLIENT_ID",     "stub")
os.environ.setdefault("REDDIT_CLIENT_SECRET", "stub")

from monitor import filter_homelabsales, filter_buildapcsales

def check(label, post, expected_notify):
    if label == "HDD":
        notify, *_ = filter_homelabsales(post)
    else:
        notify, *_ = filter_buildapcsales(post)
    ok = notify == expected_notify
    status = "OK  " if ok else "FAIL"
    result = "NOTIFY" if notify else "skip"
    print(f"  [{status}] {result}  {post['title'][:70]}")
    return ok

def post(title, flair="", body=""):
    return {"title": title, "link_flair_text": flair, "selftext": body, "author": "x", "permalink": "/"}

all_ok = True
print("\n-- r/homelabsales (HDD) ------------------------------------------")
cases = [
    (post("[FS][US-MI] Western Digital Red Pros 8 TB",                   "US-MI"), True),
    (post("[FS][USA-UT] 4TB, 8TB, 18TB HDD's, 960GB SSD's. Clear-out",  "US-W"),  True),
    (post("[FS][US-SE] HGST 8TB SATA HDD",                               "US-SE"), True),
    (post("[FS][US-E] 4x 8TB Seagate Exos SATA $280",                    "US-E"),  True),
    (post("[FS][US-C] 4x 8TB SAS + 4x 8TB SATA HDDs",                   "US-C"),  True),
    (post("[FS][US-W] 8TB WD Red $300",                                   "US-W"),  False),  # $37.50/TB > $25
    (post("[W] WD Red 4TB",                                               "US-W"),  False),  # not [FS]
    (post("[FS][CAN] 8TB HDD",                                            "CAN"),   False),  # not US
    (post("[FS][EU] Seagate Barracuda 4TB",                               "EU"),    False),  # not US
    (post("[FS][US-W] Samsung 870 EVO 1TB SSD",                          "US-W"),  False),  # SSD only
    (post("[FS][US-W] Samsung 980 Pro 2TB NVMe M.2",                     "US-W"),  False),  # NVMe
    (post("[FS][US-C] 10x 4TB SAS drives",                               "US-C"),  False),  # SAS only
]
for p, expected in cases:
    if not check("HDD", p, expected):
        all_ok = False

print("\n-- r/buildapcsales (9070 XT) -------------------------------------")
cases = [
    (post("[GPU] XFX RX 9070 XT 16GB - $549.99 - Amazon",              "GPU"),       True),
    (post("[GPU] Sapphire PULSE Radeon RX 9070 XT $599 Microcenter",   "GPU"),       True),
    (post("[GPU] [RESTOCK] AMD RX 9070XT - $599 - Best Buy",           "GPU"),       True),
    (post("[GPU] RX 9070 XT $499 - Newegg",                            "GPU"),       True),
    (post("[GPU] XFX RX 9070 XT - $549",                               "Expired"),   False),  # expired flair
    (post("[Expired] [GPU] RX 9070 XT - $549 - Amazon",                "GPU"),       False),  # expired in title
    (post("[GPU] CyberPowerPC Prebuilt with RX 9070 XT - $1299",       "Prebuilt"),  False),  # prebuilt flair
    (post("[GPU] Prebuilt PC RX 9070 XT $1199 - BestBuy",             "GPU"),       False),  # prebuilt in title
    (post("[GPU] RX 9070 (non-XT) $449 - Amazon",                      "GPU"),       False),  # not XT
    (post("[CPU] AMD Ryzen 9 9900X $399",                              "CPU"),       False),  # wrong item
    (post("[GPU] RTX 5080 $999 - Amazon",                              "GPU"),       False),  # wrong GPU
]
for p, expected in cases:
    if not check("GPU", p, expected):
        all_ok = False

print()
print("All tests passed!" if all_ok else "SOME TESTS FAILED")
