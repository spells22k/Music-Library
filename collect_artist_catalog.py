import argparse
import csv
import json
import re
import time
from pathlib import Path
from urllib.parse import (
    parse_qs,
    urlencode,
    unquote,
    urljoin,
    urlparse,
    urlunparse,
)

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


BASE = "https://www.whosampled.com"

ROLE_QUERY = {
    "artist": "1",
    "producer": "2",
}


def clean(value):
    if value is None:
        return ""
    return str(value).strip()


def load_json(path, default):
    if not path.exists():
        return default

    try:
        data = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )
        return data
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


def artist_slug_from_url(url):
    parts = [
        unquote(part)
        for part in urlparse(
            clean(url)
        ).path.strip("/").split("/")
        if part
    ]

    return (
        parts[0]
        if parts
        else ""
    )


def normalized_url(url):
    value = clean(url)

    if not value:
        return ""

    parsed = urlparse(
        value
    )

    path = unquote(
        parsed.path
    ).rstrip("/")

    return (
        f"{parsed.scheme.casefold()}://"
        f"{parsed.netloc.casefold()}"
        f"{path.casefold()}"
    )


def accepted_reviews(reviews):
    rows = []

    if not isinstance(
        reviews,
        dict,
    ):
        return rows

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

        ws_url = clean(
            review.get(
                "whosampled_artist_url"
            )
        )

        if not ws_url:
            continue

        rows.append({
            "review_key":
                review_key,

            "artist_name":
                clean(
                    review.get(
                        "spotify_artist_name"
                    )
                )
                or artist_slug_from_url(
                    ws_url
                ),

            "whosampled_artist_url":
                ws_url,

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

    return rows


def select_artist(
    rows,
    requested,
):
    wanted = clean(
        requested
    ).casefold()

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
            "Could not uniquely resolve accepted artist "
            f"{requested!r}; matches={len(matches)}"
        )

    return matches[0]


def build_listing_url(
    artist_url,
    role,
    page_number,
):
    parsed = urlparse(
        artist_url
    )

    query = {}

    if role in ROLE_QUERY:
        query[
            "role"
        ] = ROLE_QUERY[
            role
        ]

    if page_number > 1:
        query[
            "sp"
        ] = str(
            page_number
        )

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            "",
            urlencode(
                query
            ),
            "",
        )
    )


def classify_artist_link(
    url,
    artist_slug,
):
    parsed = urlparse(
        url
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

    if len(parts) == 1:
        return "artist_listing"

    # Explicit non-track artist subpages.
    if parts[1].casefold() in {
        "facts",
        "samples",
        "sampled",
        "covers",
        "covered",
        "remixes",
        "remixed",
        "connections",
    }:
        return "artist_subpage"

    if len(parts) == 2:
        return "track_candidate"

    return "artist_internal_other"


def extract_listing_tracks(
    html,
    page_url,
    artist_slug,
):
    """
    Extract one credited recording per WhoSampled .trackItem.

    The recording URL is NOT required to live under the catalog
    artist's slug. This is essential for producer catalogs, where
    Madlib may be credited on a Kanye West, MF DOOM, Madvillain,
    etc. recording.

    Relationship-detail links inside the same trackItem are
    deliberately ignored here and can be collected later as
    optional enrichment evidence.
    """

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    discovered = []
    seen = set()

    track_items = soup.select(
        ".trackItem"
    )

    for item in track_items:

        track_name = item.select_one(
            ".trackName"
        )

        if track_name is None:
            continue

        # The first meaningful link inside .trackName is the
        # recording page. Subsequent links are artist profiles.
        track_link = None

        for anchor in track_name.find_all(
            "a",
            href=True,
        ):

            href = clean(
                anchor.get(
                    "href"
                )
            )

            if not href:
                continue

            absolute = urljoin(
                page_url,
                href,
            )

            parsed = urlparse(
                absolute
            )

            parts = [
                unquote(part)
                for part
                in parsed.path
                .strip("/")
                .split("/")
                if part
            ]

            # A recording page has the ordinary:
            #
            #   /Artist-Slug/Track-Slug/
            #
            # shape. Explicit relationship/detail/media paths
            # are not recording pages.
            if len(parts) != 2:
                continue

            if parts[0].casefold() in {
                "sample",
                "cover",
                "remix",
                "interpolation",
                "movie",
                "tv",
            }:
                continue

            track_link = anchor
            break

        if track_link is None:
            continue

        track_url = urljoin(
            page_url,
            clean(
                track_link.get(
                    "href"
                )
            ),
        )

        key = normalized_url(
            track_url
        )

        if not key or key in seen:
            continue

        seen.add(
            key
        )

        track_title = clean(
            track_link.get_text(
                " ",
                strip=True,
            )
        )

        # ----------------------------------------------------
        # Preserve listing-level evidence.
        #
        # This text contains useful things such as:
        #
        #   title
        #   year
        #   by <artists>
        #   feat. <artists>
        #   Producer credit: Madlib
        #
        # We keep it intact now and can parse finer-grained
        # listing metadata later if useful.
        # ----------------------------------------------------

        track_name_text = clean(
            track_name.get_text(
                " ",
                strip=True,
            )
        )

        track_item_text = clean(
            item.get_text(
                " ",
                strip=True,
            )
        )

        # ----------------------------------------------------
        # Preserve relationship-detail URLs as deferred
        # enrichment evidence WITHOUT visiting them.
        # ----------------------------------------------------

        relationship_urls = []

        for anchor in item.find_all(
            "a",
            href=True,
        ):

            absolute = urljoin(
                page_url,
                clean(
                    anchor.get(
                        "href"
                    )
                ),
            )

            parsed = urlparse(
                absolute
            )

            parts = [
                part
                for part
                in parsed.path
                .strip("/")
                .split("/")
                if part
            ]

            if (
                parts
                and parts[0].casefold()
                in {
                    "sample",
                    "cover",
                    "remix",
                    "interpolation",
                }
            ):
                if absolute not in relationship_urls:
                    relationship_urls.append(
                        absolute
                    )

        discovered.append({
            "whosampled_track_url":
                track_url,

            "discovered_title":
                track_title,

            "listing_track_name_text":
                track_name_text,

            "listing_track_item_text":
                track_item_text,

            "relationship_urls_json":
                json.dumps(
                    relationship_urls,
                    ensure_ascii=False,
                ),

            "relationship_url_count":
                len(
                    relationship_urls
                ),

            "anchor_classes":
                " ".join(
                    clean(value)
                    for value
                    in track_link.get(
                        "class",
                        [],
                    )
                ),

            "parent_classes":
                " ".join(
                    clean(value)
                    for value
                    in (
                        track_link.parent.get(
                            "class",
                            [],
                        )
                        if track_link.parent
                        else []
                    )
                ),
        })

    return discovered

def extract_max_page(
    html,
    page_url,
):
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    pages = {
        1
    }

    for anchor in soup.find_all(
        "a",
        href=True,
    ):

        absolute = urljoin(
            page_url,
            clean(
                anchor.get(
                    "href"
                )
            ),
        )

        query = parse_qs(
            urlparse(
                absolute
            ).query
        )

        values = query.get(
            "sp",
            []
        )

        for value in values:
            try:
                page_number = int(
                    value
                )
            except Exception:
                continue

            if page_number >= 1:
                pages.add(
                    page_number
                )

    return max(
        pages
    )


def main():
    parser = argparse.ArgumentParser()

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
    )

    parser.add_argument(
        "--roles",
        default="artist,producer",
        help=(
            "Comma-separated catalog roles. "
            "Currently supports artist,producer."
        ),
    )

    parser.add_argument(
        "--delay",
        type=float,
        default=12.0,
    )

    parser.add_argument(
        "--page-limit",
        type=int,
        default=None,
        help=(
            "Maximum live/cached listing pages "
            "processed per role during this run."
        ),
    )

    parser.add_argument(
        "--cache-only",
        action="store_true",
    )

    parser.add_argument(
        "--headless",
        action="store_true",
    )

    args = parser.parse_args()

    run_dir = Path(
        args.run_dir
    )

    reviews_file = (
        run_dir
        / "artist_catalog_reviews.json"
    )

    reviews = load_json(
        reviews_file,
        {},
    )

    artists = accepted_reviews(
        reviews
    )

    artist = select_artist(
        artists,
        args.artist,
    )

    artist_url = artist[
        "whosampled_artist_url"
    ]

    artist_slug = (
        artist_slug_from_url(
            artist_url
        )
    )

    roles = [
        clean(role).casefold()
        for role
        in args.roles.split(",")
        if clean(role)
    ]

    unsupported = [
        role
        for role in roles
        if role not in ROLE_QUERY
    ]

    if unsupported:
        raise SystemExit(
            "Unsupported roles: "
            + ", ".join(
                unsupported
            )
        )

    output_dir = (
        run_dir
        / "artist_catalog_pages"
        / safe_name(
            artist_slug
        )
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    manifest_file = (
        run_dir
        / "artist_catalog_manifest.json"
    )

    tracks_file = (
        run_dir
        / "artist_catalog_tracks.csv"
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

    existing_tracks = {}

    if tracks_file.exists():
        with tracks_file.open(
            "r",
            encoding="utf-8",
            newline="",
        ) as f:
            reader = csv.DictReader(
                f
            )

            for row in reader:
                key = (
                    clean(
                        row.get(
                            "catalog_artist_review_key"
                        )
                    ),
                    clean(
                        row.get(
                            "catalog_role"
                        )
                    ),
                    normalized_url(
                        row.get(
                            "whosampled_track_url"
                        )
                    ),
                )

                existing_tracks[
                    key
                ] = row

    print("=" * 100)
    print(
        "WHOSAMPLED CREDITED-TRACK "
        "CATALOG COLLECTION"
    )
    print("=" * 100)
    print()
    print(
        "ARTIST:",
        artist[
            "artist_name"
        ],
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
        "ROLES:",
        ", ".join(
            roles
        ),
    )
    print(
        "DELAY:",
        args.delay,
        "seconds",
    )

    browser = None
    context = None
    page = None
    playwright = None

    requests_made = 0
    pages_processed = 0

    new_track_count = 0

    try:
        for role in roles:

            print()
            print("=" * 100)
            print(
                "ROLE:",
                role
            )
            print("=" * 100)

            role_manifest_key = (
                artist[
                    "review_key"
                ]
                + "::"
                + role
            )

            role_state = manifest.get(
                role_manifest_key,
                {},
            )

            if not isinstance(
                role_state,
                dict,
            ):
                role_state = {}

            known_max_page = int(
                role_state.get(
                    "max_page",
                    1,
                )
                or 1
            )

            role_status = clean(
                role_state.get(
                    "status",
                    "",
                )
            ).casefold()

            last_page_processed = int(
                role_state.get(
                    "last_page_processed",
                    0,
                )
                or 0
            )

            # A completed role has already reached its observed final
            # listing page. Do not walk it again during an ordinary
            # resumable collection run.
            if role_status == "complete":
                print(
                    "ROLE ALREADY COMPLETE:",
                    role,
                    "through page",
                    last_page_processed,
                )
                continue

            # Resume after the last page that was successfully parsed and
            # checkpointed. A failed/rate-limited page is not recorded as
            # last_page_processed, so it will be retried on the next run.
            page_number = max(
                1,
                last_page_processed + 1,
            )
            role_pages_this_run = 0

            if last_page_processed:
                print(
                    "RESUMING AFTER CHECKPOINTED PAGE:",
                    last_page_processed,
                )
                print(
                    "NEXT PAGE:",
                    page_number,
                )

            while True:

                if (
                    args.page_limit
                    is not None
                    and role_pages_this_run
                    >= args.page_limit
                ):
                    print(
                        "PAGE LIMIT REACHED:",
                        args.page_limit,
                    )
                    break

                listing_url = (
                    build_listing_url(
                        artist_url,
                        role,
                        page_number,
                    )
                )

                archive_path = (
                    output_dir
                    / (
                        "role_"
                        + safe_name(
                            role
                        )
                        + "__page_"
                        + str(
                            page_number
                        )
                        + ".html"
                    )
                )

                print()
                print("-" * 100)
                print(
                    "PAGE:",
                    page_number,
                )
                print(
                    "URL:",
                    listing_url,
                )

                html = None
                source = ""

                if archive_path.exists():

                    html = (
                        archive_path
                        .read_text(
                            encoding="utf-8",
                            errors="replace",
                        )
                    )

                    source = "cache"

                    print(
                        "CACHE HIT:",
                        archive_path,
                    )

                elif args.cache_only:

                    print(
                        "CACHE MISS — REQUEST SUPPRESSED"
                    )
                    break

                else:

                    if playwright is None:

                        playwright = (
                            sync_playwright()
                            .start()
                        )

                        browser = (
                            playwright
                            .chromium
                            .launch(
                                headless=
                                    args.headless
                            )
                        )

                        context = (
                            browser
                            .new_context()
                        )

                        page = (
                            context
                            .new_page()
                        )

                    print(
                        "REQUEST:",
                        listing_url,
                    )

                    response = page.goto(
                        listing_url,
                        wait_until=
                            "domcontentloaded",
                        timeout=60000,
                    )

                    status = (
                        response.status
                        if response
                        else None
                    )

                    requests_made += 1

                    print(
                        "HTTP STATUS:",
                        status,
                    )

                    if status == 429:

                        manifest[
                            role_manifest_key
                        ] = {
                            **role_state,

                            "artist_name":
                                artist[
                                    "artist_name"
                                ],

                            "review_key":
                                artist[
                                    "review_key"
                                ],

                            "catalog_role":
                                role,

                            "status":
                                "rate_limited",

                            "last_page_attempted":
                                page_number,

                            "requests_made":
                                requests_made,
                        }

                        save_json(
                            manifest_file,
                            manifest,
                        )

                        raise SystemExit(
                            "Stopped safely on "
                            "WhoSampled HTTP 429."
                        )

                    if status != 200:

                        print(
                            "STOP ROLE: HTTP",
                            status,
                        )

                        break

                    html = page.content()

                    archive_path.write_text(
                        html,
                        encoding="utf-8",
                    )

                    source = "live"

                    print(
                        "ARCHIVED:",
                        archive_path,
                    )

                discovered = (
                    extract_listing_tracks(
                        html,
                        listing_url,
                        artist_slug,
                    )
                )

                max_page = (
                    extract_max_page(
                        html,
                        listing_url,
                    )
                )

                known_max_page = max(
                    known_max_page,
                    max_page,
                )

                print(
                    "TRACK URLS DISCOVERED:",
                    len(
                        discovered
                    ),
                )
                print(
                    "MAX PAGE OBSERVED:",
                    known_max_page,
                )

                for track in discovered:

                    key = (
                        artist[
                            "review_key"
                        ],
                        role,
                        normalized_url(
                            track[
                                "whosampled_track_url"
                            ]
                        ),
                    )

                    row = {
                        "catalog_artist_review_key":
                            artist[
                                "review_key"
                            ],

                        "catalog_artist_name":
                            artist[
                                "artist_name"
                            ],

                        "catalog_artist_whosampled_url":
                            artist_url,

                        "catalog_role":
                            role,

                        "catalog_listing_page":
                            page_number,

                        "catalog_listing_url":
                            listing_url,

                        "whosampled_track_url":
                            track[
                                "whosampled_track_url"
                            ],

                        "discovered_title":
                            track[
                                "discovered_title"
                            ],

                        "anchor_classes":
                            track[
                                "anchor_classes"
                            ],

                        "parent_classes":
                            track[
                                "parent_classes"
                            ],

                        "listing_track_name_text":
                            track[
                                "listing_track_name_text"
                            ],

                        "listing_track_item_text":
                            track[
                                "listing_track_item_text"
                            ],

                        "relationship_urls_json":
                            track[
                                "relationship_urls_json"
                            ],

                        "relationship_url_count":
                            track[
                                "relationship_url_count"
                            ],

                        # Downstream state fields.
                        "track_archive_status":
                            "pending",

                        "track_parse_status":
                            "pending",

                        "spotify_match_status":
                            "pending",

                        "spotify_review_status":
                            "pending",

                        "relationship_enrichment_status":
                            "not_requested",
                    }

                    if key not in existing_tracks:
                        existing_tracks[
                            key
                        ] = row

                        new_track_count += 1

                pages_processed += 1
                role_pages_this_run += 1

                role_state = {
                    "artist_name":
                        artist[
                            "artist_name"
                        ],

                    "review_key":
                        artist[
                            "review_key"
                        ],

                    "whosampled_artist_url":
                        artist_url,

                    "catalog_role":
                        role,

                    "status":
                        "in_progress",

                    "last_page_processed":
                        page_number,

                    "max_page":
                        known_max_page,

                    "requests_made":
                        requests_made,
                }

                manifest[
                    role_manifest_key
                ] = role_state

                save_json(
                    manifest_file,
                    manifest,
                )

                # Persist track manifest after EVERY page.
                fieldnames = [
                    "catalog_artist_review_key",
                    "catalog_artist_name",
                    "catalog_artist_whosampled_url",
                    "catalog_role",
                    "catalog_listing_page",
                    "catalog_listing_url",
                    "whosampled_track_url",
                    "discovered_title",
                    "anchor_classes",
                    "parent_classes",
                    "listing_track_name_text",
                    "listing_track_item_text",
                    "relationship_urls_json",
                    "relationship_url_count",
                    "track_archive_status",
                    "track_parse_status",
                    "spotify_match_status",
                    "spotify_review_status",
                    "relationship_enrichment_status",
                ]

                # Preserve columns added later by downstream stages,
                # e.g. the catalog track-page archiver.
                for existing_row in existing_tracks.values():

                    for column in existing_row.keys():

                        if column not in fieldnames:
                            fieldnames.append(
                                column
                            )

                with tracks_file.open(
                    "w",
                    encoding="utf-8",
                    newline="",
                ) as f:
                    writer = csv.DictWriter(
                        f,
                        fieldnames=
                            fieldnames,
                    )

                    writer.writeheader()

                    writer.writerows(
                        existing_tracks.values()
                    )

                if page_number >= known_max_page:

                    role_state[
                        "status"
                    ] = "complete"

                    manifest[
                        role_manifest_key
                    ] = role_state

                    save_json(
                        manifest_file,
                        manifest,
                    )

                    print(
                        "ROLE COMPLETE."
                    )

                    break

                page_number += 1

                if source == "live":

                    print(
                        f"Waiting "
                        f"{args.delay:g} seconds..."
                    )

                    time.sleep(
                        args.delay
                    )

    finally:

        if browser is not None:
            browser.close()

        if playwright is not None:
            playwright.stop()

    print()
    print("=" * 100)
    print("CATALOG COLLECTION SUMMARY")
    print("=" * 100)
    print(
        "Pages processed:",
        pages_processed,
    )
    print(
        "Live requests:",
        requests_made,
    )
    print(
        "New track URLs:",
        new_track_count,
    )
    print(
        "Unique catalog track assertions:",
        len(
            existing_tracks
        ),
    )
    print(
        "Track manifest:",
        tracks_file,
    )
    print(
        "Catalog state:",
        manifest_file,
    )
    print()
    print(
        "No track pages were requested."
    )
    print(
        "No relationship-detail pages were requested."
    )
    print(
        "No Spotify requests were made."
    )


if __name__ == "__main__":
    main()
