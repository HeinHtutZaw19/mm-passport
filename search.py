"""Usage: python search.py "HEIN HTUT ZAW" MF46**97 ...  (names fuzzy-matched, IDs exact)"""
import re, sqlite3, sys
from difflib import SequenceMatcher

def id_compatible(q, stored):
    """Masks differ per post (MF46**97, MF4***97, MF4**97), so compare only the visible digits."""
    if m := re.fullmatch(r"(M[A-Z]?)(\d{6})", q):  # full number: every digit is visible on both sides
        q = f"{m[1]}{m[2]}*{m[2]}"
    a, b = re.fullmatch(r"(M[A-Z]?)(\d*)\*+(\d*)", q), re.fullmatch(r"(M[A-Z]?)(\d*)\*+(\d*)", stored)
    return bool(a and b) and a[1] == b[1] and (a[2].startswith(b[2]) or b[2].startswith(a[2])) \
        and (a[3].endswith(b[3]) or b[3].endswith(a[3]))


assert id_compatible("MF46**97", "MF4***97") and id_compatible("MF46**97", "MF4**97") and not id_compatible("MF46**97", "MF3****7")
assert id_compatible("MF469457", "MF46**57") and id_compatible("MF469457", "MF4****7") and not id_compatible("MF469457", "MF46**97")

db = sqlite3.connect("passports.db")
rows = db.execute("SELECT fb_post_link, date, name, id FROM entries").fetchall()
for q in sys.argv[1:]:
    q = q.upper().strip()
    if "**" in q or re.fullmatch(r"M[A-Z]?\d{6}", q):
        hits = [r for r in rows if id_compatible(q, r[3])]
    else:  # ponytail: difflib ratio >= 0.8 tolerates 1-2 OCR char errors; tune if too noisy
        hits = [r for r in rows if SequenceMatcher(None, q, r[2]).ratio() >= 0.8 or q in r[2]]
    print(f"== {q}: {len(hits)} match(es)")
    for r in hits:
        print("  ", r[1], r[2], r[3], r[0])
