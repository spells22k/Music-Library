#!/usr/bin/env python3

import argparse
import csv
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


BASE_URL = "https://www.whosampled.com"

# These are relationship-detail pages.
#
# IMPORTANT:
#   /Artist/Track/sampled/
# is a SECONDARY TRACK PAGE and is intentionally NOT matched here.
#
# We only want:
#   /sample/123/...
#   /cover/123/...
#   /remix/123/...
#   /interpolation/123/...
RELATIONSHIP_PATTERN = re.compile(
    r"/(sample|cover|remix|interpolation)/(\d+)/",
    re.IGNORECASE,
)


def normalize_url(url):
    if not url:
        return ""

    url = urljoin(BASE_URL, str(url).strip())

    parsed = urlparse(url)

    path = re.sub(r"/+", "/", parsed.path)

    if path != "/" and not path.endswith("/"):
        path += "/"

    return f"{BASE_URL}{path}"


def relationship_type(url):
    match = RELATIONSHIP_PATTERN.search(url)

    if not match:
        return ""

    kind = match.group(1).lower()

    return {
        "sample": "sampled",
        "cover": "covers",
        "remix": "remix",
        "interpolation": "interpolates",
    }.get(kind, "")


def relationship_id(url):
    match = RELATIONSHIP_PATTERN.search(url)

    if not match:
        return ""

    return match.group(2)


def relationship_archive_name(url):
    """
    Convert a WhoSampled relationship-detail URL such as:

        /sample/214877/Statik-Selektah-.../

    into:

        relationship_detail_sample_214877.html
    """

    kind = relationship_type(url)
    rid = relationship_id(url)

    if not kind or not rid:
        return None

    return f"relationship_detail_{kind}_{rid}.html"


def extract_relationship_links(html_path):
    """
    Scan an archived SECONDARY TRACK PAGE.

    Example source:

        secondary_Arthur-Verocai-Dedicada-a-Ela-sampled.html

    We do NOT treat that page as relationship metadata.

    Instead, we extract the /sample/, /cover/, /remix/, and
    /interpolation/ links contained inside it.
    """

    html = html_path.read_text(
        encoding="utf-8",
        errors="ignore",
    )

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    found = {}

    for link in soup.select("a[href]"):

        href = link.get("href", "")

        absolute = normalize_url(href)

        parsed_path = urlparse(absolute).path

        match = RELATIONSHIP_PATTERN.search(
            parsed_path
        )

        if not match:
            continue

        rid = relationship_id(absolute)

        if not rid:
            continue

        found[absolute] = {
            "relationship_url": absolute,
            "relationship_type": relationship_type(
                absolute
            ),
            "whosampled_relationship_id": rid,
            "source_secondary_page": str(
                html_path
            ),
            "link_text": " ".join(
                link.get_text(
                    " ",
                    strip=True,
                ).split()
            ),
        }

    return list(found.values())


def load_existing_manifest(path):
    """
    Preserve previous discoveries so the bridge is resumable.

    Existing manifest URLs are normalized before being used as
    dictionary keys. This ensures that deduplication happens
    AFTER URL normalization, consistently across runs.
    """

    if not path.exists():
        return {}

    existing = {}

    with path.open(
        "r",
        encoding="utf-8",
        newline="",
    ) as f:

        reader = csv.DictReader(f)

        for row in reader:

            raw_url = row.get(
                "relationship_url",
                "",
            ).strip()

            if not raw_url:
                continue

            url = normalize_url(raw_url)

            if not url:
                continue

            # Store the canonical URL in the manifest row itself.
            row["relationship_url"] = url

            existing[url] = row

    return existing


def write_manifest(path, rows):
    fieldnames = [
        "relationship_url",
        "relationship_type",
        "whosampled_relationship_id",
        "source_secondary_page",
        "link_text",
        "archive_path",
        "archive_status",
    ]

    with path.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames,
        )

        writer.writeheader()

        for row in sorted(
            rows,
            key=lambda x: (
                x.get(
                    "relationship_type",
                    "",
                ),
                x.get(
                    "whosampled_relationship_id",
                    "",
                ),
            ),
        ):
            writer.writerow(row)


def main():

    parser = argparse.ArgumentParser(
        description=(
            "Bridge archived WhoSampled secondary "
            "track pages to relationship-detail pages."
        )
    )

    parser.add_argument(
        "run_dir",
        help=(
            "Run directory, e.g. "
            "runs/playlist_3XtRerTr3ndS88v51AAixb"
        ),
    )

    parser.add_argument(
        "--delay",
        type=float,
        default=12,
        help=(
            "Seconds to wait between relationship-page "
            "requests. Default: 12."
        ),
    )

    parser.add_argument(
        "--headless",
        action="store_true",
        help=(
            "Run Chromium headlessly. "
            "Default is headed so you can observe the request."
        ),
    )

    parser.add_argument(
        "--discover-only",
        action="store_true",
        help=(
            "Scan archived secondary pages and write the "
            "discovery manifest without making network requests."
        ),
    )

    args = parser.parse_args()

    run_dir = Path(
        args.run_dir
    )

    if not run_dir.exists():
        raise SystemExit(
            f"Run directory does not exist: {run_dir}"
        )

    html_dir = (
        run_dir / "whosampled_pages"
    )

    if not html_dir.exists():
        raise SystemExit(
            f"Missing WhoSampled page directory: {html_dir}"
        )

    relationship_dir = (
        run_dir / "relationship_pages"
    )

    relationship_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    manifest_path = (
        run_dir
        / "relationship_pages_manifest.csv"
    )

    secondary_files = sorted(
        html_dir.glob(
            "secondary_*.html"
        )
    )

    print("=" * 80)
    print("SECONDARY → RELATIONSHIP DETAIL BRIDGE")
    print("=" * 80)

    print(
        f"Run directory: {run_dir}"
    )

    print(
        f"Secondary pages found: {len(secondary_files)}"
    )

    if not secondary_files:
        print()
        print(
            "No secondary_*.html files found."
        )
        print(
            "Nothing to do."
        )
        return

    # --------------------------------------------------------
    # PHASE 1
    #
    # Scan ALL existing secondary pages first.
    #
    # No network requests happen during this phase.
    # --------------------------------------------------------

    all_rows = load_existing_manifest(
        manifest_path
    )

    discovered = 0

    print()
    print("-" * 80)
    print("PHASE 1: DISCOVER RELATIONSHIP URLs")
    print("-" * 80)

    for secondary_file in secondary_files:

        print()
        print(
            f"Scanning: {secondary_file.name}"
        )

        rows = extract_relationship_links(
            secondary_file
        )

        print(
            f"  Found {len(rows)} relationship-detail links"
        )

        for row in rows:

            url = row[
                "relationship_url"
            ]

            archive_name = (
                relationship_archive_name(
                    url
                )
            )

            if not archive_name:
                continue

            archive_path = (
                relationship_dir
                / archive_name
            )

            row["archive_path"] = str(
                archive_path
            )

            if url in all_rows:

                existing = all_rows[url]

                # Keep the existing archive path/status,
                # but fill missing information if necessary.
                if not existing.get(
                    "archive_path"
                ):
                    existing[
                        "archive_path"
                    ] = str(
                        archive_path
                    )

                if archive_path.exists():
                    existing[
                        "archive_status"
                    ] = "archived"

                continue

            row[
                "archive_status"
            ] = (
                "already_archived"
                if archive_path.exists()
                else "pending"
            )

            all_rows[url] = row

            discovered += 1

    print()
    print(
        f"Unique relationship-detail URLs: {len(all_rows)}"
    )

    print(
        f"New relationship-detail URLs: {discovered}"
    )

    # Save the discovery results BEFORE making network requests.
    write_manifest(
        manifest_path,
        list(all_rows.values()),
    )

    print()
    print(
        f"Discovery manifest saved: {manifest_path}"
    )

    if args.discover_only:
        print()
        print("=" * 80)
        print("DISCOVERY-ONLY MODE")
        print("=" * 80)
        print("No relationship-page network requests were made.")
        return

    # --------------------------------------------------------
    # PHASE 2
    #
    # Archive each relationship-detail page.
    #
    # Existing archives are skipped.
    # --------------------------------------------------------

    print()
    print("-" * 80)
    print("PHASE 2: ARCHIVE RELATIONSHIP DETAIL PAGES")
    print("-" * 80)

    playwright = None
    browser = None
    page = None

    try:

        for url in sorted(all_rows):

            row = all_rows[url]

            archive_path = Path(
                row["archive_path"]
            )

            if archive_path.exists():

                row[
                    "archive_status"
                ] = "archived"

                print()
                print(
                    f"CACHE HIT: {archive_path.name}"
                )

                continue

            print()
            print(
                f"REQUEST: {url}"
            )

            if page is None:

                print(
                    "Starting Playwright..."
                )

                playwright = (
                    sync_playwright().start()
                )

                browser = (
                    playwright.chromium.launch(
                        headless=args.headless
                    )
                )

                page = (
                    browser.new_page()
                )

            try:

                response = page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )

            except Exception as exc:

                row[
                    "archive_status"
                ] = (
                    "request_error:"
                    + repr(exc)
                )

                print(
                    f"  ERROR: {exc!r}"
                )

                # Persist immediately.
                write_manifest(
                    manifest_path,
                    list(
                        all_rows.values()
                    ),
                )

                continue

            status = (
                response.status
                if response
                else None
            )

            print(
                f"  HTTP status: {status}"
            )

            if status == 429:

                row[
                    "archive_status"
                ] = "stopped_on_429"

                write_manifest(
                    manifest_path,
                    list(
                        all_rows.values()
                    ),
                )

                print()
                print(
                    "HTTP 429 encountered."
                )
                print(
                    "Stopping safely so the run can be resumed."
                )

                return

            if status != 200:

                row[
                    "archive_status"
                ] = (
                    "http_"
                    + str(status)
                )

                write_manifest(
                    manifest_path,
                    list(
                        all_rows.values()
                    ),
                )

                continue

            html = page.content()

            archive_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            archive_path.write_text(
                html,
                encoding="utf-8",
            )

            row[
                "archive_status"
            ] = "archived"

            print(
                f"  ARCHIVED: {archive_path}"
            )

            write_manifest(
                manifest_path,
                list(
                    all_rows.values()
                ),
            )

            print(
                f"  Waiting {args.delay:g} seconds..."
            )

            time.sleep(
                args.delay
            )

    finally:

        if browser is not None:
            browser.close()

        if playwright is not None:
            playwright.stop()

    # --------------------------------------------------------
    # FINAL REPORT
    # --------------------------------------------------------

    write_manifest(
        manifest_path,
        list(all_rows.values()),
    )

    archived = sum(
        1
        for row in all_rows.values()
        if row.get(
            "archive_status"
        ) == "archived"
    )

    pending = sum(
        1
        for row in all_rows.values()
        if row.get(
            "archive_status"
        ) != "archived"
    )

    print()
    print("=" * 80)
    print("BRIDGE COMPLETE")
    print("=" * 80)

    print(
        f"Secondary pages scanned: {len(secondary_files)}"
    )

    print(
        f"Relationship URLs discovered: {len(all_rows)}"
    )

    print(
        f"Relationship pages archived: {archived}"
    )

    print(
        f"Relationship pages not archived: {pending}"
    )

    print(
        f"Manifest: {manifest_path}"
    )

    print(
        f"Archive directory: {relationship_dir}"
    )


if __name__ == "__main__":
    main()
