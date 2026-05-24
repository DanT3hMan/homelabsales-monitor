import re

_HARD_EXCLUDE_PATTERNS = [
    r'\bssds?\b',
    r'\b(nvme|solid[\s\-]?state|nand)\b',
    r'\bm\.2\b',
    r'\bu\.2\b',
]
_HDD_PATTERNS = [
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

def is_fs_post(title):
    return bool(re.search(r'\[FS\]', title, re.IGNORECASE))

def is_us_flair(flair):
    if not flair:
        return True
    f = flair.strip().lower()
    return f.startswith("us-") or f.startswith("usa-")

def is_sata_hdd(title):
    t = title.lower()
    for pat in _HARD_EXCLUDE_PATTERNS:
        if re.search(pat, t):
            return False, re.search(pat, t).group()
    has_sata = any(re.search(p, t) for p in _HDD_PATTERNS)
    has_sas = bool(re.search(r'\bsas\b', t))
    if has_sas and not has_sata:
        return False, "SAS only"
    if has_sata:
        return True, "mixed" if has_sas else ""
    return False, "no HDD keyword"

tests = [
    # (title, flair, expected)
    ("[FS][US-MI] Western Digital Red Pros 8 TB",               "US-MI",    "NOTIFY"),
    ("[FS][US-AUT] 4TB 8TB 18TB HDDs, 16TB, 1TB, 960GB, 800GB", "US-W",    "NOTIFY"),
    ("[FS][US-SE] HGST 8TB SATA HDD",                           "US-SE",    "NOTIFY"),
    ("[FS][US-E] 4x 8TB Seagate Exos SATA $280",                "US-E",     "NOTIFY"),
    ("[W] WD Red 4TB",                                           "US-W",     "skip"),
    ("[FS][CAN] 8TB HDD",                                        "CAN",      "skip"),
    ("[FS][EU] Seagate Barracuda 4TB",                           "EU",       "skip"),
    ("[FS][US-W] Samsung 870 EVO 1TB SSD",                       "US-W",     "skip"),
    ("[FS][US-C] 10x 4TB SAS drives",                            "US-C",     "skip"),
]

all_pass = True
for title, flair, expected in tests:
    fs = is_fs_post(title)
    us = is_us_flair(flair)
    hdd, reason = is_sata_hdd(title)
    result = "NOTIFY" if (fs and us and hdd) else "skip"
    ok = result == expected
    if not ok:
        all_pass = False
    why = ""
    if result == "skip":
        why = f"  ({'not [FS]' if not fs else 'not US flair' if not us else reason})"
    status = "OK" if ok else "FAIL"
    print(f"  [{status}] {result}{why}")
    print(f"        {title[:70]}")

print()
print("All tests passed!" if all_pass else "SOME TESTS FAILED")
