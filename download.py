"""Walk the page's photo stream newest -> oldest via 'Next photo', saving images until before the cutoff.
Writes photos/<fbid>.jpg and photos/manifest.csv (fbid, url, date).

Usage: python download.py [--since YYYY-MM-DD] [--fix-dates]
Default cutoff: 14 days before the newest date in docs/data.json (weekly runs overlap; extract.py dedupes).
Set HEADLESS=1 to run without a window (CI)."""
import csv, json, os, re, sys, time, urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from playwright.sync_api import sync_playwright

PAGE = "https://www.facebook.com/profile.php?id=100064770370682&sk=photos"
HEADLESS = os.environ.get("HEADLESS") == "1"
OUT = Path("photos"); OUT.mkdir(exist_ok=True)
DATE_RE = re.compile(r"^(January|February|March|April|May|June|July|August|September|October|November|December) (\d{1,2})(?:, (\d{4}))?")


def parse_date(text, now=None):
    now = now or datetime.now()
    m = DATE_RE.match(text.strip())
    if not m:
        return None
    d = datetime.strptime(f"{m[1]} {m[2]} {m[3] or now.year}", "%B %d %Y")
    return d.replace(year=d.year - 1) if not m[3] and d > now + timedelta(days=1) else d  # "December 30" seen in January


def cutoff():
    if "--since" in sys.argv:
        return datetime.fromisoformat(sys.argv[sys.argv.index("--since") + 1])
    data = Path("docs/data.json")
    newest = max((r[0] for r in json.loads(data.read_text())), default="") if data.exists() else ""
    return datetime.fromisoformat(newest) - timedelta(days=14) if newest else datetime(2026, 6, 1)


def launch(p):
    ctx = p.chromium.launch_persistent_context("profile", channel="chrome", headless=HEADLESS, viewport={"width": 1280, "height": 900})
    return ctx, (ctx.pages[0] if ctx.pages else ctx.new_page())


def page_date(page, now=None):
    now = now or datetime.now()
    labels = page.eval_on_selector_all("a[aria-label]", "els => els.map(e => e.getAttribute('aria-label'))")
    for t in labels:
        if d := parse_date(t):
            return d
        if m := re.fullmatch(r"(\d+)([mhdw])", t.strip()):
            unit = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}[m[2]]
            return now - timedelta(**{unit: int(m[1])})
    return None


def fix_dates():
    """Revisit manifest rows with no date."""
    rows = list(csv.DictReader(open(OUT / "manifest.csv")))
    with sync_playwright() as p:
        ctx, page = launch(p)
        for r in rows:
            if not r["date"]:
                page.goto(r["url"]); time.sleep(4)
                d = page_date(page)
                r["date"] = d.date().isoformat() if d else ""
                print(r["fbid"], r["date"] or "?", flush=True)
        ctx.close()
    with open(OUT / "manifest.csv", "w", newline="") as f:
        w = csv.DictWriter(f, ["fbid", "url", "date"]); w.writeheader(); w.writerows(rows)


def main():
    since = cutoff()
    print("cutoff", since.date(), flush=True)
    manifest = OUT / "manifest.csv"
    done = {r["fbid"] for r in csv.DictReader(manifest.open())} if manifest.exists() else set()
    f = manifest.open("a", newline=""); w = csv.writer(f)
    if not done:
        w.writerow(["fbid", "url", "date"])
    with sync_playwright() as p:
        ctx, page = launch(p)
        page.goto(PAGE); time.sleep(6)
        url = page.eval_on_selector("a[href*='photo.php?fbid']", "e => e.href")  # raises if FB served a login wall
        misses = 0
        page.goto(url)
        while url:
            time.sleep(3)
            fbid = re.search(r"fbid=(\d+)", page.url)[1]
            d = page_date(page)
            if d and d < since:
                print("reached cutoff at", d.date(), page.url); break
            src = page.eval_on_selector_all("img", "els => els.filter(e => e.naturalWidth > 500).map(e => e.src)")
            if fbid not in done and src:
                urllib.request.urlretrieve(src[0], OUT / f"{fbid}.jpg")
                w.writerow([fbid, f"https://www.facebook.com/photo.php?fbid={fbid}", d.date().isoformat() if d else ""]); f.flush()
                done.add(fbid)
            print(fbid, d.date() if d else "?", flush=True)
            misses = misses + 1 if not d else 0
            if misses > 5:
                print("dates unreadable, stopping"); break
            prev = page.url
            try:
                page.eval_on_selector("div[aria-label='Next photo']", "e => e.click()")
                page.wait_for_function("u => location.href !== u", arg=prev, timeout=15000)
                url = page.url
            except Exception:
                print("no next photo"); url = None
        ctx.close()


if __name__ == "__main__":
    assert parse_date("October 25 at 11:21 AM", now=datetime(2026, 11, 13)) == datetime(2026, 10, 25)
    assert parse_date("December 30 at 9:00 AM", now=datetime(2027, 1, 2)) == datetime(2026, 12, 30)
    assert parse_date("May 3, 2025") == datetime(2025, 5, 3)
    fix_dates() if "--fix-dates" in sys.argv else main()
