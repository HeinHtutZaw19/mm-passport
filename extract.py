"""OCR photos/*.jpg -> passports.db table entries(fb_post_link, date, name, id)."""
import csv, json, re, sqlite3, subprocess, sys, tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Masks vary per post: MF46**92, MF4***92, MF3****2, MF 4**09. Stored as printed.
# OCR may mangle stars ('*k', '"***') or add a letter ("MFS56**09").
ID_RE = re.compile(r"^(M[A-Z]?)[A-Z]?([0-9O]{1,2})([^0-9\s]{1,5})([0-9O]{1,2})\W*$")
SCALES = (2000, 1500, 1750, 2250, 2500, 2750, 3000)  # first one doubles as the "any IDs at all?" probe
DIGITS = "MF0123456789*"  # whitelist pass: stops 6/5/8 being read as G/S/a in small IDs
NAME_RE = re.compile(r"^[A-Z][A-Z.'-]*$")


def norm_token(t):
    return t.strip("|[](),.!:;").upper().replace("0", "O")  # names never contain digits; "00" is "OO"


def ocr_words(path, dpi=None, whitelist=None):
    """Returns (image width, [(left, top, width, height, text)])."""
    opts = (["--dpi", str(dpi)] if dpi else []) + (["-c", f"tessedit_char_whitelist={whitelist}"] if whitelist else [])
    tsv = subprocess.run(["tesseract", str(path), "-", *opts, "tsv"], capture_output=True, text=True).stdout
    rows = list(csv.DictReader(tsv.splitlines(), delimiter="\t", quoting=csv.QUOTE_NONE))
    width = max((int(r["left"]) + int(r["width"]) for r in rows), default=1)
    return width, [tuple(int(r[k]) for k in ("left", "top", "width", "height")) + (r["text"].strip(),)
                   for r in rows if r["level"] == "5" and r["text"].strip()]


def rows_by_id(words):
    """For each passport-ID word, the words on the same visual row to its left (tesseract may put
    the ID column in a separate block, so its own line grouping can't be trusted)."""
    cy = lambda w: w[1] + w[3] / 2
    same_row = lambda a, b: abs(cy(a) - cy(b)) < 0.6 * max(a[3], b[3])
    # join IDs split as "MF" + "45**43"
    for a in [w for w in words if re.fullmatch(r"M[A-Z]", w[4])]:
        nxt = [w for w in words if same_row(a, w) and 0 < w[0] - a[0] - a[2] < 3 * a[3] and re.match(r"[0-9O]{1,2}\*", w[4])]
        if nxt:
            b = nxt[0]; words.remove(a); words.remove(b)
            words.append((a[0], min(a[1], b[1]), b[0] + b[2] - a[0], max(a[3], b[3]), a[4] + b[4]))
    for idw in sorted((w for w in words if ID_RE.match(w[4])), key=lambda w: w[1]):
        row = sorted(w for w in words if w[0] + w[2] <= idw[0] and same_row(w, idw))
        yield idw[1], [(w[0], w[2], w[4]) for w in row] + [(idw[0], idw[2], idw[4])]


def parse_line(words, width):
    """words: [(left, width, text)] sorted by x. Returns (name, id) or None."""
    for i, (_, _, t) in enumerate(words):
        if m := ID_RE.match(t):
            before = words[:i]
            pipes = [k for k, w in enumerate(before) if "|" in w[2]]
            if pipes:  # "|" is the OCR'd column border after the serial number
                k = pipes[-1]
                before = before[k + 1:] if before[k][2].endswith("|") else [before[k][:2] + (before[k][2].split("|")[-1],)] + before[k + 1:]
            # drop serial column: everything before the last big horizontal gap
            for j in range(len(before) - 1, 0, -1):
                if before[j][0] - (before[j - 1][0] + before[j - 1][1]) > 0.025 * width:
                    before = before[j:]; break
            name = " ".join(n for _, _, t in before if NAME_RE.match(n := norm_token(t)))
            return (name, fmt_id(m)) if name else None
    return None


def fmt_id(m):
    num = lambda d: d.replace("O", "0")
    return f"{m[1]}{num(m[2])}{'*' * len(m[3])}{num(m[4])}"


def image_size(path):
    out = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)], capture_output=True, text=True).stdout
    return tuple(int(n) for n in re.findall(r"pixel(?:Width|Height): (\d+)", out))


def vote(readings):
    """readings: [(y as fraction of image height, (name or None, id))] from several OCR passes.
    Readings of the same table row land at about the same y; keep the most common name and id per row,
    and drop rows only one pass saw (usually junk) or that no pass read a name for. One photo uses one
    mask shape (MF12**34), so ids of that shape win over misreads like MF1**34."""
    shape = lambda i: re.sub(r"\d", "9", i or "")
    shapes = Counter(shape(i) for _, (_, i) in readings if i)
    # misreads drop digits ("MF6**56"), never add them: the true mask is the most-digit shape with real support
    page_shape = max((k for k, c in shapes.items() if c >= 0.25 * max(shapes.values())), key=lambda k: k.count("9"), default="")
    rows = []
    for y, p in sorted(readings, key=lambda r: r[0]):
        if rows and y - rows[-1][0] < 0.012:  # rows are ~0.035 of the page apart
            rows[-1][1].append(p)
        else:
            rows.append([y, [p]])
    best_id = lambda ps: Counter(i for _, i in ps).most_common(1)[0][0]
    return [(names.most_common(1)[0][0], best_id([p for p in ids if shape(p[1]) == page_shape] or ids))
            for _, ps in rows if len(ps) >= 2 and (names := Counter(n for n, _ in ps if n)) and (ids := [p for p in ps if p[1]])]


def extract(path):
    w, h = image_size(path)
    if w >= 900:
        width, words = ocr_words(path)
        return [p for _, ws in rows_by_id(words) if (p := parse_line(ws, width))]
    # Some lists are uploaded as ~470px PNGs. Tesseract reads them badly, and which upscale works best
    # varies per image, so OCR several upscales and vote per row.
    readings = []
    with tempfile.TemporaryDirectory() as d:
        for z in SCALES:
            up = f"{d}/{z}.png"
            subprocess.run(["sips", "-s", "format", "png", "--resampleWidth", str(z), str(path), "--out", up], capture_output=True, check=True)
            width, words = ocr_words(up, dpi=300)
            # names from the normal pass; its ids lose digits to look-alike letters ("MFS0**65"), so ids come from the digits pass
            readings += [(y / (h * z / w), (p[0], None)) for y, ws in rows_by_id(words) if (p := parse_line(ws, width))]
            _, words = ocr_words(up, dpi=300, whitelist=DIGITS)
            readings += [(y / (h * z / w), (None, fmt_id(ID_RE.match(ws[-1][2])))) for y, ws in rows_by_id(words)]
            if not readings:
                return []  # not a list (event photo, notice)
    return vote(readings)


def main():
    """OCR every photo in photos/ and merge into docs/data.json (rows: [date, name, id, fbid]),
    replacing any rows from the same photos. Then rebuild passports.db from data.json."""
    manifest = {r["fbid"]: r for r in csv.DictReader(open("photos/manifest.csv"))}
    data = Path("docs/data.json")
    paths = sorted(Path("photos").glob("*.jpg"))
    seen = {p.stem for p in paths}
    rows = [r for r in (json.loads(data.read_text()) if data.exists() else []) if r[3] not in seen]
    with ThreadPoolExecutor(8) as ex:
        for path, found in zip(paths, ex.map(extract, paths)):
            rows += [[manifest.get(path.stem, {}).get("date", ""), n, i, path.stem] for n, i in found]
    rows.sort(key=lambda r: (r[0], r[3]), reverse=True)
    data.parent.mkdir(exist_ok=True)
    data.write_text(json.dumps(rows, separators=(",", ":")))

    db = sqlite3.connect("passports.db")
    db.execute("DROP TABLE IF EXISTS entries")
    db.execute("CREATE TABLE entries (fb_post_link TEXT, date TEXT, name TEXT, id TEXT)")
    db.executemany("INSERT INTO entries VALUES (?,?,?,?)",
                   [(f"https://www.facebook.com/photo.php?fbid={f}", d, n, i) for d, n, i, f in rows])
    db.execute("CREATE INDEX idx_id ON entries(id)")
    db.commit()
    print(len(rows), "rows from", len({r[3] for r in rows}), "photos")


if __name__ == "__main__":
    W = 1133
    assert parse_line([(180, 20, "G"), (248, 40, "HSU"), (300, 70, "HLAING"), (380, 50, "HTUN"), (700, 90, "MF46**92")], W) == ("HSU HLAING HTUN", "MF46**92")
    assert parse_line([(180, 30, "JR"), (248, 40, "CHO"), (300, 40, "SIN"), (350, 50, "LYNN"), (700, 90, "MF61*k00")], W) == ("CHO SIN LYNN", "MF61**00")
    assert parse_line([(248, 40, "NU"), (290, 40, "YEE"), (700, 90, "MF50**36,")], W) == ("NU YEE", "MF50**36")
    assert parse_line([(180, 10, "J"), (200, 5, "|"), (248, 40, "CHIT"), (300, 20, "Ko"), (330, 20, "Ko"), (700, 90, "MF68**31")], W) == ("CHIT KO KO", "MF68**31")
    assert parse_line([(180, 20, "29"), (205, 5, "|"), (248, 40, "NAY"), (300, 20, "MYO"), (330, 20, "00"), (700, 90, "MFS56**09")], W) == ("NAY MYO OO", "MF56**09")
    assert parse_line([(180, 20, "J."), (230, 40, "MIN"), (280, 40, "KYAW"), (700, 90, "MF63**23")], W) == ("MIN KYAW", "MF63**23")
    assert parse_line([(184, 25, "go"), (253, 79, "|JUBILIE"), (342, 40, "HTA"), (741, 97, "MF22**51")], W) == ("JUBILIE HTA", "MF22**51")
    assert parse_line([(184, 28, "sq"), (253, 44, "[MYO"), (306, 40, "SAN"), (740, 98, "MF42**73")], W) == ("MYO SAN", "MF42**73")
    assert parse_line([(140, 20, "66"), (190, 40, "HEIN"), (250, 40, "HTET"), (300, 40, "AUNG"), (680, 90, "MF1***93")], W) == ("HEIN HTET AUNG", "MF1***93")
    assert parse_line([(100, 20, "34"), (170, 40, "THADAR"), (220, 40, "SOE"), (700, 90, 'ME3"***2')], W) == ("THADAR SOE", "ME3****2")
    split = [(100, 10, 20, 20, "J"), (250, 12, 40, 20, "SU"), (300, 12, 40, 20, "MYAT"), (800, 8, 25, 20, "MF"), (830, 8, 60, 20, "22**48"),
             (250, 60, 40, 20, "NU"), (300, 60, 40, 20, "YEE"), (800, 55, 90, 20, "MF50**36")]
    assert [parse_line(r, W) for _, r in rows_by_id(split)] == [("SU MYAT", "MF22**48"), ("NU YEE", "MF50**36")]
    assert vote([(0.10, ("SU MYAT", "MF22**48")), (0.101, ("SU MYAT", "MF2***48")), (0.102, ("SU MYAT", "MF22**48")),
                 (0.14, ("NU YEE", "MF5**36")), (0.141, ("NU YE", "MF50**36")), (0.142, ("NU YEE", "MF5**36")), (0.143, (None, "MF5**36")),
                 (0.30, ("JUNK", "MF11**11")), (0.50, (None, "MF22**22")), (0.501, (None, "MF22**22"))]) == [("SU MYAT", "MF22**48"), ("NU YEE", "MF50**36")]
    main()
