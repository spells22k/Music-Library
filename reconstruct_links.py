#!/usr/bin/env python3

import argparse
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlparse


BASE_URL = "https://www.whosampled.com"

RELATIONSHIP_PATTERN = re.compile(
    r"/(sample|cover|interpolation|remix)/(\d+)/",
    re.IGNORECASE,
)


def normalize_url(url):
    if not url:
        return ""

    url = url.strip()

    if url.startswith("/"):
        url = urljoin(BASE_URL, url)

    parsed = urlparse(url)

    path = re.sub(r"/+", "/", parsed.path)

    if path != "/" and not path.endswith("/"):
        path += "/"

    return f"{BASE_URL}{path}"


def extract_links_from_html(html):
    """
    Extract EVERY occurrence of a WhoSampled relationship-detail
    URL from an HTML document.

    NO deduplication happens here.
    """

    results = []

    # Find href attributes.
    href_pattern = re.compile(
        r'''href\s*=\s*["']([^"']+)["']''',
        re.IGNORECASE,
    )

    for match in href_pattern.finditer(html):

        href = match.group(1)

        url = normalize_url(href)

        parsed_path = urlparse(url).path

        if RELATIONSHIP_PATTERN.search(parsed_path):

            results.append(url)

    return results


def main():

    parser = argparse.ArgumentParser(
        description=(
            "Reconstruct raw WhoSampled relationship links "
            "from archived HTML pages."
        )
    )

    parser.add_argument(
        "directory",
        help="Directory containing archived HTML files.",
    )

    parser.add_argument(
        "--output",
        default="relationship_links_raw.txt",
        help="Output file for raw links.",
    )

    args = parser.parse_args()

    directory = Path(args.directory)

    if not directory.exists():
        raise SystemExit(
            f"Directory does not exist: {directory}"
        )

    html_files = sorted(
        list(directory.rglob("*.html"))
        + list(directory.rglob("*.htm"))
    )

    print("=" * 80)
    print("RECONSTRUCTING RAW WHOSAMPLED RELATIONSHIP LINKS")
    print("=" * 80)

    print(f"\nHTML files found: {len(html_files)}")

    if not html_files:
        print("\nNo HTML files found.")
        return

    raw_relationship_links = []

    files_with_links = 0

    for i, html_path in enumerate(
        html_files,
        1,
    ):

        try:
            html = html_path.read_text(
                encoding="utf-8",
                errors="ignore",
            )
        except Exception as e:
            print(
                f"\nERROR reading {html_path}: {e}"
            )
            continue

        links = extract_links_from_html(html)

        if links:
            files_with_links += 1

            print(
                f"[{i}/{len(html_files)}] "
                f"{html_path.name}: "
                f"{len(links)} links"
            )

            raw_relationship_links.extend(links)

    # ---------------------------------------------------------
    # NORMALIZE + DEDUPLICATE
    # ---------------------------------------------------------
    # Normalize relationship URLs BEFORE deduplication.
    # This means equivalent URLs are treated as the same
    # relationship even if their formatting differs.

    def normalize_relationship_url(url):
        url = url.strip()

        if not url:
            return None

        # Convert relative WhoSampled URLs to absolute URLs.
        if url.startswith("/"):
            url = "https://www.whosampled.com" + url

        # Remove query strings and fragments.
        url = url.split("#", 1)[0]
        url = url.split("?", 1)[0]

        # Canonical trailing slash.
        url = url.rstrip("/") + "/"

        return url

    normalized_relationship_links = []

    for url in raw_relationship_links:
        normalized = normalize_relationship_url(url)

        if normalized:
            normalized_relationship_links.append(normalized)

    counts = Counter(
        normalized_relationship_links
    )

    unique_links = list(
        dict.fromkeys(
            normalized_relationship_links
        )
    )

    duplicate_occurrences = (
        len(normalized_relationship_links)
        - len(unique_links)
    )

    # ---------------------------------------------------------
    # RAW LIST
    # ---------------------------------------------------------

    print("\n" + "-" * 80)
    print("RAW LIST")
    print("-" * 80)

    for i, url in enumerate(
        raw_relationship_links,
        1,
    ):
        print(
            f"{i}. {url}"
        )

    # ---------------------------------------------------------
    # UNIQUE LIST
    # ---------------------------------------------------------

    print("\n" + "-" * 80)
    print("UNIQUE LIST")
    print("-" * 80)

    for i, url in enumerate(
        unique_links,
        1,
    ):
        print(
            f"{i}. {url}"
        )

    # ---------------------------------------------------------
    # SAVE RAW LIST
    # ---------------------------------------------------------

    output_path = Path(
        args.output
    )

    output_path.write_text(
        "\n".join(
            raw_relationship_links
        ),
        encoding="utf-8",
    )

    print("\n" + "=" * 80)
    print("RAW LIST SAVED")
    print("=" * 80)

    print(
        output_path.resolve()
    )

    # ---------------------------------------------------------
    # SAVE UNIQUE LIST
    # ---------------------------------------------------------

    unique_output = output_path.with_name(
        output_path.stem + "_unique.txt"
    )

    unique_output.write_text(
        "\n".join(
            unique_links
        ),
        encoding="utf-8",
    )

    print(
        "\nUNIQUE LIST SAVED"
    )

    print(
        unique_output.resolve()
    )


if __name__ == "__main__":
    main()
