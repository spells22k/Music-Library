import argparse
import csv
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import (
    parse_qs,
    unquote,
    urljoin,
    urlparse,
)

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


BASE_URL = "https://www.whosampled.com"


# These are treated as artist catalog/navigation paths rather
# than track titles when they appear directly beneath an artist.
CATALOG_SEGMENTS = {
    "samples",
    "sampled",
    "sample",
    "covers",
    "covered",
    "cover",
    "remixes",
    "remixed",
    "remix",
    "interpolations",
    "interpolated",
    "interpolates",
    "connections",
    "credits",
    "appearances",
    "tracks",
    "songs",
}


def clean(value):
    if value is None:
        return ""
    return str(value).strip()


def load_json(path, default):
    if not path.exists():
        return default

    try:
        value = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )
        return value
    except Exception:
        return default


def save_json(path, value):
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    path.write_text(
        json.dumps(
            value,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def safe_name(value):
    value = re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        clean(value),
    ).strip("_")

    return value or "artist"


def normalized_url(url):
    url = clean(url)

    if not url:
        return ""

    parsed = urlparse(url)

    path = unquote(
        parsed.path
    ).rstrip("/")

    return (
        f"{parsed.scheme.casefold()}://"
        f"{parsed.netloc.casefold()}"
        f"{path.casefold()}"
    )


def artist_slug_from_url(url):
    parts = [
        unquote(part)
        for part in urlparse(
            url
        ).path.strip("/").split("/")
        if part
    ]

    return (
        parts[0]
        if parts
        else ""
    )


def classify_link(
    href,
    artist_slug,
):
    """
    Conservative classification.

    This does NOT claim a URL is definitely a track.
    It identifies track CANDIDATES for the next checkpoint.
    """

    parsed = urlparse(
        href
    )

    if (
        parsed.netloc
        and parsed.netloc.casefold()
        not in {
            "whosampled.com",
            "www.whosampled.com",
        }
    ):
        return "external"

    parts = [
        unquote(part)
        for part in parsed.path.strip("/").split("/")
        if part
    ]

    if not parts:
        return "site_root"

    # Relationship-detail pages are not artist track pages.
    if parts[0].casefold() in {
        "sample",
        "cover",
        "remix",
        "interpolation",
    }:
        return "relationship_detail"

    if (
        parts[0].casefold()
        != artist_slug.casefold()
    ):
        return "other_whosampled"

    # Same artist root with ?page=N or similar.
    query = parse_qs(
        parsed.query
    )

    if (
        len(parts) == 1
        and query
    ):
        return "artist_pagination"

    if len(parts) == 1:
        return "artist_profile"

    second = parts[1].casefold()

    if second in CATALOG_SEGMENTS:
        return "artist_catalog_page"

    if len(parts) == 2:
        return "artist_track_candidate"

    return "artist_internal_other"


def accepted_reviews(
    reviews,
):
    result = []

    if not isinstance(
        reviews,
        dict,
    ):
        return result

    for review_key, review in reviews.items():
        if not isinstance(
            review,
            dict,
        ):
            continue

        if (
            clean(
                review.get(
                    "decision"
                )
            ).casefold()
            != "accepted"
        ):
            continue

        url = clean(
            review.get(
                "whosampled_artist_url"
            )
        )

        if not url:
            continue

        result.append({
            "review_key":
                review_key,

            "artist_name":
                clean(
                    review.get(
                        "spotify_artist_name"
                    )
                ),

            "whosampled_artist_url":
                url,

            "spotify_artist_id":
                clean(
                    review.get(
                        "spotify_artist_id"
                    )
                ),

            "spotify_identity_reconciled":
                bool(
                    review.get(
                        "spotify_identity_reconciled"
                    )
                ),
        })

    return result


def select_artist(
    rows,
    requested,
):
    requested = clean(
        requested
    )

    if not requested:
        if len(rows) == 1:
            return rows[0]

        raise SystemExit(
            "Specify --artist when more than one "
            "accepted artist exists."
        )

    wanted = requested.casefold()

    matches = []

    for row in rows:
        values = {
            clean(
                row.get(
                    "artist_name"
                )
            ).casefold(),

            clean(
                row.get(
                    "review_key"
                )
            ).casefold(),

            artist_slug_from_url(
                row.get(
                    "whosampled_artist_url"
                )
            ).casefold(),
        }

        if wanted in values:
            matches.append(
                row
            )

    if len(matches) != 1:
        raise SystemExit(
            f"Could not uniquely resolve accepted artist: "
            f"{requested!r}. Matches: {len(matches)}"
        )

    return matches[0]


def extract_links(
    html,
    page_url,
    artist_slug,
):
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    rows = []
    seen = set()

    for anchor in soup.find_all(
        "a",
        href=True,
    ):
        raw_href = clean(
            anchor.get(
                "href"
            )
        )

        if not raw_href:
            continue

        absolute = urljoin(
            page_url,
            raw_href,
        )

        key = normalized_url(
            absolute
        )

        # Keep query distinctions for pagination.
        parsed = urlparse(
            absolute
        )

        dedupe_key = (
            key,
            parsed.query,
        )

        if dedupe_key in seen:
            continue

        seen.add(
            dedupe_key
        )

        text = clean(
            anchor.get_text(
                " ",
                strip=True,
            )
        )

        classes = " ".join(
            clean(value)
            for value in anchor.get(
                "class",
                [],
            )
        )

        parent_classes = ""

        parent = anchor.parent

        if parent is not None:
            parent_classes = " ".join(
                clean(value)
                for value in parent.get(
                    "class",
                    [],
                )
            )

        rows.append({
            "classification":
                classify_link(
                    absolute,
                    artist_slug,
                ),

            "url":
                absolute,

            "anchor_text":
                text,

            "anchor_classes":
                classes,

            "parent_classes":
                parent_classes,
        })

    return rows


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Discovery-only WhoSampled artist catalog "
            "inspection. Follows no discovered links."
        )
    )

    parser.add_argument(
        "--run-dir",
        default=(
            "runs/"
            "playlist_3XtRerTr3ndS88v51AAixb"
        ),
    )

    parser.add_argument(
        "--artist",
        required=True,
        help=(
            "Accepted artist name, review key, "
            "or WhoSampled slug."
        ),
    )

    parser.add_argument(
        "--headless",
        action="store_true",
    )

    parser.add_argument(
        "--cache-only",
        action="store_true",
        help=(
            "Parse saved artist HTML only. "
            "Never make a WhoSampled request."
        ),
    )

    args = parser.parse_args()

    run_dir = Path(
        args.run_dir
    )

    review_file = (
        run_dir
        / "artist_catalog_reviews.json"
    )

    output_dir = (
        run_dir
        / "artist_catalog_discovery"
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    manifest_file = (
        output_dir
        / "manifest.json"
    )

    reviews = load_json(
        review_file,
        {},
    )

    accepted = accepted_reviews(
        reviews
    )

    print("=" * 96)
    print(
        "WHOSAMPLED ARTIST CATALOG "
        "DISCOVERY-ONLY TEST"
    )
    print("=" * 96)
    print()
    print(
        "Accepted artist identities:",
        len(accepted),
    )

    artist = select_artist(
        accepted,
        args.artist,
    )

    artist_name = (
        artist[
            "artist_name"
        ]
        or artist_slug_from_url(
            artist[
                "whosampled_artist_url"
            ]
        )
    )

    artist_url = artist[
        "whosampled_artist_url"
    ]

    artist_slug = (
        artist_slug_from_url(
            artist_url
        )
    )

    archive_name = (
        "artist_profile__"
        + safe_name(
            artist_slug
        )
        + ".html"
    )

    archive_path = (
        output_dir
        / archive_name
    )

    csv_path = (
        output_dir
        / (
            "links__"
            + safe_name(
                artist_slug
            )
            + ".csv"
        )
    )

    print()
    print(
        "ARTIST:",
        artist_name,
    )
    print(
        "REVIEW KEY:",
        artist[
            "review_key"
        ],
    )
    print(
        "WHOSAMPLED:",
        artist_url,
    )
    print(
        "SPOTIFY ID:",
        artist[
            "spotify_artist_id"
        ]
        or "(none)",
    )
    print(
        "SPOTIFY RECONCILED:",
        artist[
            "spotify_identity_reconciled"
        ],
    )

    html = None
    http_status = None
    final_url = artist_url
    source = ""

    if archive_path.exists():
        print()
        print(
            "CACHE HIT:",
            archive_path,
        )

        html = archive_path.read_text(
            encoding="utf-8",
            errors="replace",
        )

        source = "cache"

    elif args.cache_only:
        print()
        print(
            "CACHE MISS — REQUEST SUPPRESSED"
        )

        raise SystemExit(
            "No cached profile HTML available."
        )

    else:
        print()
        print(
            "REQUEST:",
            artist_url,
        )

        with sync_playwright() as p:

            browser = (
                p.chromium.launch(
                    headless=
                        args.headless
                )
            )

            context = (
                browser.new_context()
            )

            page = (
                context.new_page()
            )

            try:
                response = page.goto(
                    artist_url,
                    wait_until=
                        "domcontentloaded",
                    timeout=60000,
                )

                http_status = (
                    response.status
                    if response
                    else None
                )

                final_url = clean(
                    page.url
                )

                print(
                    "HTTP STATUS:",
                    http_status,
                )
                print(
                    "FINAL URL:",
                    final_url,
                )

                if http_status == 429:
                    raise SystemExit(
                        "Stopped safely on "
                        "WhoSampled HTTP 429."
                    )

                if http_status != 200:
                    raise SystemExit(
                        f"WhoSampled returned "
                        f"HTTP {http_status}."
                    )

                html = page.content()

            finally:
                browser.close()

        archive_path.write_text(
            html,
            encoding="utf-8",
        )

        source = "live"

        print(
            "ARCHIVED:",
            archive_path,
        )

    links = extract_links(
        html,
        final_url,
        artist_slug,
    )

    counts = {}

    for row in links:
        kind = row[
            "classification"
        ]

        counts[
            kind
        ] = (
            counts.get(
                kind,
                0,
            )
            + 1
        )

    with csv_path.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "classification",
                "url",
                "anchor_text",
                "anchor_classes",
                "parent_classes",
            ],
        )

        writer.writeheader()
        writer.writerows(
            links
        )

    manifest = load_json(
        manifest_file,
        {},
    )

    if not isinstance(
        manifest,
        dict,
    ):
        manifest = {}

    manifest[
        artist[
            "review_key"
        ]
    ] = {
        **artist,

        "artist_name":
            artist_name,

        "requested_url":
            artist_url,

        "final_url":
            final_url,

        "artist_slug":
            artist_slug,

        "source":
            source,

        "http_status":
            http_status,

        "archive_path":
            str(
                archive_path
            ),

        "links_csv":
            str(
                csv_path
            ),

        "link_count":
            len(links),

        "classification_counts":
            counts,
    }

    save_json(
        manifest_file,
        manifest,
    )

    print()
    print("=" * 96)
    print("DISCOVERY RESULTS")
    print("=" * 96)

    for kind in sorted(
        counts
    ):
        print(
            f"{kind}:",
            counts[kind],
        )

    track_candidates = [
        row
        for row in links
        if row[
            "classification"
        ]
        == "artist_track_candidate"
    ]

    catalog_pages = [
        row
        for row in links
        if row[
            "classification"
        ]
        == "artist_catalog_page"
    ]

    pagination = [
        row
        for row in links
        if row[
            "classification"
        ]
        == "artist_pagination"
    ]

    print()
    print("-" * 96)
    print("TRACK CANDIDATES VISIBLE ON PROFILE")
    print("-" * 96)

    for row in track_candidates:
        print(
            row[
                "anchor_text"
            ]
            or "(no text)",
            "→",
            row["url"],
        )

    print()
    print("-" * 96)
    print("CATALOG / CATEGORY LINKS")
    print("-" * 96)

    for row in catalog_pages:
        print(
            row[
                "anchor_text"
            ]
            or "(no text)",
            "→",
            row["url"],
        )

    print()
    print("-" * 96)
    print("PAGINATION LINKS")
    print("-" * 96)

    for row in pagination:
        print(
            row[
                "anchor_text"
            ]
            or "(no text)",
            "→",
            row["url"],
        )

    print()
    print("=" * 96)
    print("DISCOVERY-ONLY COMPLETE")
    print("=" * 96)
    print()
    print(
        "Profile archive:",
        archive_path,
    )
    print(
        "Link inventory:",
        csv_path,
    )
    print(
        "Manifest:",
        manifest_file,
    )
    print()
    print(
        "No discovered link was visited."
    )
    print(
        "No Spotify requests were made."
    )


if __name__ == "__main__":
    main()
