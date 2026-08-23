import asyncio
import random
import re
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import async_playwright


RUN_DIR = Path("runs/playlist_3XtRerTr3ndS88v51AAixb")
SECONDARY = RUN_DIR / "whosampled_pages/secondary_Arthur-Verocai-Dedicada-a-Ela-sampled.html"
OUTPUT_DIR = RUN_DIR / "relationship_pages_test"

BASE = "https://www.whosampled.com"
DELAY_MIN = 25
DELAY_MAX = 35


def find_relationship_url(path):
    html = path.read_text(encoding="utf-8", errors="ignore")

    # Prefer actual relationship-detail URLs, not /sampled/ navigation pages.
    patterns = [
        r'href=["\']([^"\']*/sample/\d+/[^"\']*)["\']',
        r'href=["\']([^"\']*/cover/\d+/[^"\']*)["\']',
        r'href=["\']([^"\']*/remix/\d+/[^"\']*)["\']',
        r'href=["\']([^"\']*/interpolation/\d+/[^"\']*)["\']',
    ]

    for pattern in patterns:
        match = re.search(pattern, html, re.I)
        if match:
            return urljoin(BASE, match.group(1))

    return None


async def main():
    if not SECONDARY.exists():
        raise SystemExit(f"Secondary page not found: {SECONDARY}")

    url = find_relationship_url(SECONDARY)

    if not url:
        raise SystemExit("No relationship-detail URL found in the archived secondary page.")

    print("=" * 72)
    print("ONE-REQUEST WHO SAMPLED RATE-LIMIT TEST")
    print("=" * 72)
    print()
    print(f"Source archive: {SECONDARY}")
    print(f"Selected URL:   {url}")
    print()

    delay = random.uniform(DELAY_MIN, DELAY_MAX)

    print(f"Waiting {delay:.1f} seconds before the single request...")
    await asyncio.sleep(delay)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        context = await browser.new_context(
            viewport={"width": 1365, "height": 768},
            locale="en-US",
        )

        page = await context.new_page()

        print()
        print("REQUESTING — exactly one attempt, no retry...")
        print()

        try:
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=45_000,
            )

            status = response.status if response else None
            final_url = page.url

            print(f"HTTP status: {status}")
            print(f"Final URL:   {final_url}")

            if status == 429:
                print()
                print("RESULT: STILL RATE LIMITED.")
                print("Stopping. No retry will be attempted.")
                return

            if status is None:
                print()
                print("RESULT: No HTTP response was available.")
                return

            html = await page.content()

            if status == 200:
                output = OUTPUT_DIR / "relationship_test.html"
                output.write_text(html, encoding="utf-8")

                print()
                print("RESULT: REQUEST SUCCEEDED.")
                print(f"Archived to: {output}")
                print(f"Bytes:       {len(html):,}")

                if "/sample/" in final_url:
                    print("Relationship type: SAMPLE")
                elif "/cover/" in final_url:
                    print("Relationship type: COVER")
                elif "/remix/" in final_url:
                    print("Relationship type: REMIX")
                elif "/interpolation/" in final_url:
                    print("Relationship type: INTERPOLATION")
                else:
                    print("Relationship type: UNKNOWN")

            else:
                output = OUTPUT_DIR / f"relationship_test_http_{status}.html"
                output.write_text(html, encoding="utf-8")

                print()
                print(f"RESULT: HTTP {status}.")
                print(f"Response archived for inspection: {output}")
                print("Stopping. No retry will be attempted.")

        except Exception as e:
            print()
            print(f"RESULT: REQUEST FAILED: {type(e).__name__}: {e}")
            print("Stopping. No retry will be attempted.")

        finally:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
