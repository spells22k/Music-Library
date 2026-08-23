from pathlib import Path
import re
import sys

from playwright.sync_api import sync_playwright


if len(sys.argv) != 3:
    raise SystemExit(
        "Usage: python inspect_relationship_variant.py <url> <output-name.html>"
    )

url = sys.argv[1]
output_name = sys.argv[2]

output = (
    Path("runs/playlist_3XtRerTr3ndS88v51AAixb_blind")
    / output_name
)

output.parent.mkdir(
    parents=True,
    exist_ok=True,
)

with sync_playwright() as p:

    browser = p.chromium.launch(
        headless=False
    )

    page = browser.new_page()

    print("=" * 80)
    print("OPENING RELATIONSHIP PAGE")
    print("=" * 80)

    response = page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=60000,
    )

    status = (
        response.status
        if response
        else None
    )

    print("STATUS:", status)
    print("FINAL URL:", page.url)

    if status == 429:
        browser.close()
        raise SystemExit(
            "WhoSampled returned HTTP 429."
        )

    if status != 200:
        browser.close()
        raise SystemExit(
            f"Unexpected HTTP status: {status}"
        )

    html = page.content()

    output.write_text(
        html,
        encoding="utf-8",
    )

    print("HTML SAVED:", output)
    print(
        "HTML BYTES:",
        len(html.encode("utf-8")),
    )

    browser.close()
