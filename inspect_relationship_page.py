from pathlib import Path

from playwright.sync_api import sync_playwright


URL = "https://www.whosampled.com/sample/1821/DJ-Marky-XRS-Stamina-MC-LK-Toquinho-Jorge-Ben-Carolina-Carol-Bela/"

OUTPUT = Path(
    "runs/playlist_3XtRerTr3ndS88v51AAixb_blind"
) / "relationship_detail_1821.html"

OUTPUT.parent.mkdir(
    parents=True,
    exist_ok=True,
)


with sync_playwright() as p:

    browser = p.chromium.launch(
        headless=False
    )

    page = browser.new_page()

    print()
    print("=" * 80)
    print("OPENING RELATIONSHIP PAGE")
    print("=" * 80)

    response = page.goto(
        URL,
        wait_until="domcontentloaded",
        timeout=60000,
    )

    status = (
        response.status
        if response
        else None
    )

    print(
        "STATUS:",
        status,
    )

    print(
        "FINAL URL:",
        page.url,
    )

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

    OUTPUT.write_text(
        html,
        encoding="utf-8",
    )

    print(
        "HTML SAVED:",
        OUTPUT,
    )

    print(
        "HTML BYTES:",
        len(html.encode("utf-8")),
    )

    browser.close()
