import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import async_playwright

from parse_whosampled_relationship import parse_relationship


# ============================================================
# CONFIGURATION
# ============================================================

RUN_DIR = Path("runs/playlist_3XtRerTr3ndS88v51AAixb")

SECONDARY = (
    RUN_DIR
    / "whosampled_pages"
    / "secondary_Arthur-Verocai-Dedicada-a-Ela-sampled.html"
)

OUTPUT_DIR = RUN_DIR / "relationship_pages_test"

BASE = "https://www.whosampled.com"


# ============================================================
# RELATIONSHIP URL DISCOVERY
# ============================================================

def find_relationship_urls(path):
    """
    Extract actual relationship-detail URLs from the archived
    secondary WhoSampled page.

    We deliberately exclude /sampled/, /covered/, etc.
    navigation pages and only accept detail pages such as:

        /sample/214877/...
        /cover/12345/...
        /remix/...
        /interpolation/...
    """

    html = path.read_text(
        encoding="utf-8",
        errors="ignore",
    )

    patterns = [
        r'href=["\']([^"\']*/sample/\d+/[^"\']*)["\']',
        r'href=["\']([^"\']*/cover/\d+/[^"\']*)["\']',
        r'href=["\']([^"\']*/remix/\d+/[^"\']*)["\']',
        r'href=["\']([^"\']*/interpolation/\d+/[^"\']*)["\']',
    ]

    results = []
    seen = set()

    for pattern in patterns:

        for match in re.finditer(
            pattern,
            html,
            re.I,
        ):

            url = urljoin(
                BASE,
                match.group(1),
            )

            if url not in seen:
                seen.add(url)
                results.append(url)

    return results


# ============================================================
# DISPLAY PARSED RELATIONSHIP
# ============================================================

def print_track(label, track):

    print()
    print(label)
    print("-" * len(label))

    if not track:
        print("NONE")
        return

    fields = [
        ("name", track.get("name")),
        ("artists", track.get("artists")),
        ("year", track.get("year")),
        ("album", track.get("album")),
        ("label", track.get("label")),
        ("url", track.get("url")),
        ("producers", track.get("producers")),
        (
            "sample_timestamp_seconds",
            track.get("sample_timestamp_seconds"),
        ),
        ("duration", track.get("duration")),
    ]

    for key, value in fields:
        print(f"{key}: {value}")


# ============================================================
# MAIN
# ============================================================

async def main():

    print("=" * 72)
    print("PLAYWRIGHT → RELATIONSHIP PARSER FLOW TEST")
    print("=" * 72)
    print()

    # --------------------------------------------------------
    # STEP 1 — Verify archived secondary page
    # --------------------------------------------------------

    print("STEP 1 — Archived secondary page")
    print()

    print(f"Secondary: {SECONDARY}")

    if not SECONDARY.exists():
        raise SystemExit(
            f"\nERROR: Secondary page does not exist:\n{SECONDARY}"
        )

    print("FOUND")

    # --------------------------------------------------------
    # STEP 2 — Discover relationship URL
    # --------------------------------------------------------

    print()
    print("STEP 2 — Discover relationship-detail URL")
    print()

    urls = find_relationship_urls(SECONDARY)

    print(f"Relationship URLs discovered: {len(urls)}")

    if not urls:
        raise SystemExit(
            "\nERROR: No relationship-detail URLs found."
        )

    for i, url in enumerate(urls, start=1):
        print(f"{i}. {url}")

    # --------------------------------------------------------
    # For this checkpoint, use the known relationship that
    # previously produced the 403 and subsequently succeeded
    # with headed Playwright.
    # --------------------------------------------------------

    target = None

    for url in urls:
        if "/sample/214877/" in url:
            target = url
            break

    if target is None:
        target = urls[0]

    print()
    print("SELECTED RELATIONSHIP")
    print("---------------------")
    print(target)

    # --------------------------------------------------------
    # STEP 3 — Playwright request
    # --------------------------------------------------------

    print()
    print("STEP 3 — Playwright")
    print()

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    output = (
        OUTPUT_DIR
        / "relationship_flow_playwright.html"
    )

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=False
        )

        context = await browser.new_context(
            viewport={
                "width": 1440,
                "height": 900,
            },
            locale="en-US",
        )

        page = await context.new_page()

        print("Opening:")
        print(target)
        print()

        response = await page.goto(
            target,
            wait_until="domcontentloaded",
            timeout=60_000,
        )

        status = (
            response.status
            if response
            else None
        )

        print(f"STATUS: {status}")
        print(f"FINAL URL: {page.url}")
        print(f"TITLE: {await page.title()}")

        print()
        print("Waiting 10 seconds...")
        await asyncio.sleep(10)

        html = await page.content()

        output.write_text(
            html,
            encoding="utf-8",
        )

        print()
        print(f"HTML SIZE: {len(html):,}")
        print(f"SAVED: {output}")

        await context.close()
        await browser.close()

    # --------------------------------------------------------
    # STEP 4 — Parse the exact HTML Playwright produced
    # --------------------------------------------------------

    print()
    print("STEP 4 — Existing relationship parser")
    print()

    if status != 200:
        print(
            f"SKIPPED: Playwright returned HTTP {status}"
        )
        return

    try:

        result = parse_relationship(
            output,
            supplied_url=target,
        )

    except Exception as e:

        print("PARSER FAILED")
        print()
        print(
            f"{type(e).__name__}: {e}"
        )

        raise

    # --------------------------------------------------------
    # STEP 5 — Display extraction
    # --------------------------------------------------------

    print()
    print("=" * 72)
    print("PARSED RELATIONSHIP")
    print("=" * 72)

    print()
    print(
        "relationship_type:",
        result.get("relationship_type"),
    )

    print(
        "whosampled_id:",
        result.get("whosampled_id"),
    )

    print(
        "whosampled_url:",
        result.get("whosampled_url"),
    )

    print(
        "sample_type:",
        result.get("sample_type"),
    )

    print_track(
        "TRACK 1",
        result.get("track_1"),
    )

    print_track(
        "TRACK 2",
        result.get("track_2"),
    )

    # --------------------------------------------------------
    # STEP 6 — Save parser output
    # --------------------------------------------------------

    json_output = (
        OUTPUT_DIR
        / "relationship_flow_parsed.json"
    )

    json_output.write_text(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print()
    print("=" * 72)
    print("FLOW TEST COMPLETE")
    print("=" * 72)

    print()
    print(f"HTML: {output}")
    print(f"JSON: {json_output}")

    print()
    print("This test proved:")
    print("  1. Archived secondary page was readable.")
    print("  2. Relationship URL was discovered from it.")
    print("  3. Playwright successfully visited that URL.")
    print("  4. The resulting HTML was archived.")
    print("  5. The existing relationship parser consumed that HTML.")
    print("  6. Relationship metadata was extracted.")


if __name__ == "__main__":
    asyncio.run(main())
