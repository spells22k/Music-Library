import argparse
import csv
import json
import random
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

from playwright.sync_api import sync_playwright


BASE = "https://www.whosampled.com"
ARTIST_NAME = "Kanye West"
ARTIST_SLUG = "Kanye-West"

OUTPUT = Path("kanye_whosampled_track_index.csv")
STATE_FILE = Path("kanye_cache_state.json")


def is_canonical_track_url(url):
    if not url.startswith(BASE + "/"):
        return False

    parsed = urlparse(url)

    parts = [
        unquote(p)
        for p in parsed.path.strip("/").split("/")
        if p
    ]

    if len(parts) != 2:
        return False

    blocked_first = {
        "user",
        "movie",
        "tv-show",
        "artist",
        "sample",
        "cover",
        "remix",
        "interpolation",
        "search",
        "browse",
        "song-tag",
    }

    if parts[0].lower() in blocked_first:
        return False

    blocked_second = {
        "covers",
        "covered",
        "remixes",
        "remixed",
        "samples",
        "sampled",
        "facts",
        "interpolations",
        "interpolated",
    }

    if parts[1].lower() in blocked_second:
        return False

    return True


def looks_like_connection_label(text):
    text = " ".join((text or "").split())

    if not text:
        return True

    if text.casefold() in {
        "sign in",
        "sign up",
        "ok",
        "next",
        "previous",
    }:
        return True

    if text.casefold().startswith("see ") and " more connection" in text.casefold():
        return True

    return False


def extract_track_links(page):
    rows = {}

    for link in page.locator("a[href]").all():
        try:
            href = link.get_attribute("href")
            text = " ".join(link.inner_text().split())

            if not href:
                continue

            url = urljoin(BASE, href)

            if not is_canonical_track_url(url):
                continue

            if looks_like_connection_label(text):
                continue

            parsed = urlparse(url)

            parts = [
                unquote(p)
                for p in parsed.path.strip("/").split("/")
                if p
            ]

            artist_slug = parts[0]
            track_slug = parts[1]

            # Keep the most useful visible label for a URL.
            existing = rows.get(url)

            if existing:
                old_title = existing["track_title"]

                if len(text) <= len(old_title):
                    continue

            rows[url] = {
                "discovered_via_artist": ARTIST_NAME,
                "track_artist_slug": artist_slug,
                "track_artist": artist_slug.replace("-", " "),
                "track_title": text,
                "track_slug": track_slug,
                "whosampled_track_url": url,
            }

        except Exception:
            continue

    return rows


def load_existing_rows():
    rows = {}

    if not OUTPUT.exists():
        return rows

    with OUTPUT.open(
        "r",
        encoding="utf-8",
        newline=""
    ) as f:
        reader = csv.DictReader(f)

        for row in reader:
            url = row.get("whosampled_track_url")

            if url:
                rows[url] = row

    return rows


def save_rows(rows):
    ordered = sorted(
        rows.values(),
        key=lambda r: (
            r.get("track_artist_slug", "").casefold(),
            r.get("track_title", "").casefold(),
            r.get("whosampled_track_url", ""),
        )
    )

    with OUTPUT.open(
        "w",
        newline="",
        encoding="utf-8"
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "discovered_via_artist",
                "track_artist_slug",
                "track_artist",
                "track_title",
                "track_slug",
                "whosampled_track_url",
            ]
        )

        writer.writeheader()
        writer.writerows(ordered)

    return len(ordered)


def load_state():
    if not STATE_FILE.exists():
        return {
            "artist": ARTIST_NAME,
            "completed_pages": [],
            "last_successful_page": 0,
            "last_status": None,
            "total_unique_tracks": 0,
        }

    with STATE_FILE.open(
        "r",
        encoding="utf-8"
    ) as f:
        return json.load(f)


def save_state(state):
    with STATE_FILE.open(
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            state,
            f,
            indent=2
        )


def page_url(page_number):
    if page_number == 1:
        return f"{BASE}/{ARTIST_SLUG}/"
    return f"{BASE}/{ARTIST_SLUG}/?sp={page_number}"


parser = argparse.ArgumentParser()

parser.add_argument(
    "--start-page",
    type=int,
    default=None,
    help="Page to start from. Defaults to checkpoint/resume."
)

parser.add_argument(
    "--end-page",
    type=int,
    default=100,
    help="Last page to attempt. Default: 100."
)

parser.add_argument(
    "--min-delay",
    type=int,
    default=45,
    help="Minimum delay between requests in seconds."
)

parser.add_argument(
    "--max-delay",
    type=int,
    default=75,
    help="Maximum delay between requests in seconds."
)

args = parser.parse_args()


state = load_state()
rows = load_existing_rows()

if args.start_page is not None:
    start_page = args.start_page
elif state.get("last_successful_page"):
    start_page = state["last_successful_page"] + 1
else:
    start_page = 1


completed_pages = set(
    state.get("completed_pages", [])
)


print()
print("=" * 70)
print("RESUMABLE WHOSAMPLED CACHE BUILDER")
print("=" * 70)
print("Artist:", ARTIST_NAME)
print("Starting page:", start_page)
print("Ending page:", args.end_page)
print("Existing cached tracks:", len(rows))
print(
    f"Delay between requests: "
    f"{args.min_delay}-{args.max_delay} seconds"
)
print("=" * 70)


with sync_playwright() as p:

    browser = p.chromium.launch(
        headless=False
    )

    context = browser.new_context(
        viewport={
            "width": 1440,
            "height": 900
        }
    )

    page = context.new_page()

    for page_number in range(
        start_page,
        args.end_page + 1
    ):

        if page_number in completed_pages:
            print(
                f"SKIPPING page {page_number} "
                f"(already completed)"
            )
            continue

        url = page_url(page_number)

        print()
        print("=" * 70)
        print(
            f"PAGE {page_number}/{args.end_page}"
        )
        print(url)
        print("=" * 70)

        try:
            response = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=60000
            )

            status = (
                response.status
                if response
                else None
            )

            print("STATUS:", status)
            print("TITLE:", page.title())

        except Exception as e:

            print("REQUEST ERROR:", repr(e))
            print(
                "Stopping so this page can be retried later."
            )
            break

        # ------------------------------------------------------
        # RATE LIMIT
        # ------------------------------------------------------

        if status == 429:

            print()
            print("!!! RATE LIMITED (429) !!!")
            print(
                "Stopping immediately."
            )
            print(
                f"Resume from page {page_number} "
                "after the restriction clears."
            )

            state["last_status"] = 429
            save_state(state)

            break

        # ------------------------------------------------------
        # OTHER HTTP ERRORS
        # ------------------------------------------------------

        if status is None or status >= 400:

            print(
                "HTTP error:",
                status
            )

            state["last_status"] = status
            save_state(state)

            break

        # ------------------------------------------------------
        # PARSE
        # ------------------------------------------------------

        html = page.content()

        print(
            "HTML SIZE:",
            len(html)
        )

        page_rows = extract_track_links(page)

        print(
            "TRACK LINKS FOUND:",
            len(page_rows)
        )

        for track_url, row in page_rows.items():
            rows[track_url] = row

        total = save_rows(rows)

        # ------------------------------------------------------
        # CHECKPOINT
        # ------------------------------------------------------

        completed_pages.add(page_number)

        state["completed_pages"] = sorted(
            completed_pages
        )

        state["last_successful_page"] = page_number
        state["last_status"] = status
        state["total_unique_tracks"] = total

        save_state(state)

        print(
            "UNIQUE TRACKS CACHED:",
            total
        )

        # ------------------------------------------------------
        # DELAY
        # ------------------------------------------------------

        if page_number < args.end_page:

            delay = random.uniform(
                args.min_delay,
                args.max_delay
            )

            print(
                f"Sleeping {delay:.1f} seconds..."
            )

            time.sleep(delay)

    browser.close()


print()
print("=" * 70)
print("CACHE RUN COMPLETE / STOPPED")
print("=" * 70)
print(
    "Unique tracks:",
    len(rows)
)
print(
    "Last successful page:",
    state.get("last_successful_page")
)
print(
    "Output:",
    OUTPUT
)
print(
    "State:",
    STATE_FILE
)
print()
