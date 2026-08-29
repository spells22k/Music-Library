import argparse
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import pandas as pd
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from parse_whosampled_track import extract_source_metadata


RELATIONSHIP_MARKERS = (
    "/sample/",
    "/cover/",
    "/remix/",
    "/interpolation/",
    "/search/",
)


def clean(value):
    if value is None:
        return ""

    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass

    return str(value).strip()


def normalized_url(url):
    url = clean(url)

    if not url:
        return ""

    return unquote(
        url
    ).rstrip("/").casefold()


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


def save_manifest(path, manifest):
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    path.write_text(
        json.dumps(
            manifest,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def target_url_from_row(row):
    """
    Prefer an explicit related-track URL if the input contains one.

    The canonical relationships.csv currently preserves the complete
    detail-page track JSON, so it can reconstruct the related-track URL
    without needing Spotify or another network request.
    """

    direct = clean(
        row.get(
            "related_whosampled_url"
        )
    )

    if direct:
        return direct

    orientation = clean(
        row.get(
            "relationship_orientation_status"
        )
    )

    track_1_raw = clean(
        row.get(
            "relationship_detail_track_1_json"
        )
    )

    track_2_raw = clean(
        row.get(
            "relationship_detail_track_2_json"
        )
    )

    try:
        track_1 = (
            json.loads(track_1_raw)
            if track_1_raw
            else {}
        )
    except Exception:
        track_1 = {}

    try:
        track_2 = (
            json.loads(track_2_raw)
            if track_2_raw
            else {}
        )
    except Exception:
        track_2 = {}

    if orientation == "track_1_primary":
        return clean(
            track_2.get("url")
        )

    if orientation == "track_2_primary":
        return clean(
            track_1.get("url")
        )

    return ""


def archive_filename(url):
    parsed = urlparse(url)

    parts = [
        unquote(part)
        for part in parsed.path.strip("/").split("/")
        if part
    ]

    if len(parts) >= 2:
        base = (
            parts[-2]
            + "__"
            + parts[-1]
        )
    elif parts:
        base = parts[-1]
    else:
        base = "track"

    base = re.sub(
        r'[\\/:*?"<>|]+',
        "_",
        base,
    )

    base = re.sub(
        r"\s+",
        "_",
        base,
    ).strip("._")

    digest = hashlib.sha1(
        normalized_url(url).encode(
            "utf-8"
        )
    ).hexdigest()[:10]

    return (
        base[:150]
        + "__"
        + digest
        + ".html"
    )


def verify_saved_track(html, requested_url):
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    source = extract_source_metadata(
        soup
    )

    canonical_url = clean(
        source.get(
            "source_url"
        )
    )

    title = clean(
        source.get(
            "source_title"
        )
    )

    artists = clean(
        source.get(
            "source_artists"
        )
    )

    check_url = (
        canonical_url
        or requested_url
    ).casefold()

    is_relationship_page = any(
        marker in check_url
        for marker in RELATIONSHIP_MARKERS
    )

    valid = bool(
        title
        and canonical_url
        and not is_relationship_page
    )

    return {
        "valid_track_page": valid,
        "source_title": title,
        "source_artists": artists,
        "source_url": canonical_url,
        "source_album": clean(
            source.get(
                "source_album"
            )
        ),
        "source_label": clean(
            source.get(
                "source_label"
            )
        ),
        "source_release_year": clean(
            source.get(
                "source_release_year"
            )
        ),
        "source_duration": clean(
            source.get(
                "source_duration"
            )
        ),
        "source_duration_iso": clean(
            source.get(
                "source_duration_iso"
            )
        ),
        "source_duration_ms": clean(
            source.get(
                "source_duration_ms"
            )
        ),
    }


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Archive unique related WhoSampled track pages "
            "from a completed playlist run."
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
        "--limit",
        type=int,
        default=None,
        help=(
            "Process at most N uncached URLs. "
            "Useful for controlled testing."
        ),
    )

    parser.add_argument(
        "--delay",
        type=float,
        default=12.0,
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

    relationships_file = (
        run_dir
        / "relationships.csv"
    )

    if not relationships_file.exists():
        raise SystemExit(
            f"Missing: {relationships_file}"
        )

    archive_dir = (
        run_dir
        / "related_track_pages"
    )

    archive_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    manifest_file = (
        archive_dir
        / "manifest.json"
    )

    relationships = pd.read_csv(
        relationships_file
    ).fillna("")

    targets = {}

    unresolved_rows = []

    for index, row in (
        relationships.iterrows()
    ):
        url = target_url_from_row(
            row
        )

        if not url:
            unresolved_rows.append(
                int(index)
            )
            continue

        key = normalized_url(
            url
        )

        if key not in targets:
            targets[key] = {
                "requested_url":
                    url,

                "related_track":
                    clean(
                        row.get(
                            "related_track"
                        )
                    ),

                "related_artist":
                    clean(
                        row.get(
                            "related_artist"
                        )
                    ),

                "relationship_urls":
                    [],
            }

        relationship_url = clean(
            row.get(
                "whosampled_relationship_url"
            )
        )

        if (
            relationship_url
            and relationship_url
            not in targets[key][
                "relationship_urls"
            ]
        ):
            targets[key][
                "relationship_urls"
            ].append(
                relationship_url
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

    print("=" * 88)
    print("RELATED TRACK PAGE ARCHIVER")
    print("=" * 88)
    print()
    print(
        "Relationship rows:",
        len(relationships),
    )
    print(
        "Unique related track URLs:",
        len(targets),
    )
    print(
        "Rows without recoverable target URL:",
        len(unresolved_rows),
    )

    if unresolved_rows:
        print(
            "Unresolved row indexes:",
            unresolved_rows,
        )

    playwright = None
    browser = None
    page = None

    requests_made = 0

    try:
        for key, target in targets.items():
            url = target[
                "requested_url"
            ]

            filename = archive_filename(
                url
            )

            archive_path = (
                archive_dir
                / filename
            )

            entry = manifest.get(
                key,
                {}
            )

            entry.update({
                "requested_url":
                    url,

                "archive_path":
                    str(
                        archive_path
                    ),

                "related_track":
                    target[
                        "related_track"
                    ],

                "related_artist":
                    target[
                        "related_artist"
                    ],

                "relationship_urls":
                    target[
                        "relationship_urls"
                    ],
            })

            print()
            print("-" * 88)
            print(
                target[
                    "related_track"
                ],
                "—",
                target[
                    "related_artist"
                ],
            )
            print(
                "URL:",
                url,
            )

            if archive_path.exists():
                print(
                    "CACHE HIT:",
                    archive_path.name
                )

                html = archive_path.read_text(
                    encoding="utf-8",
                    errors="replace",
                )

                verification = (
                    verify_saved_track(
                        html,
                        url,
                    )
                )

                entry.update(
                    verification
                )

                entry[
                    "archive_status"
                ] = (
                    "archived"
                    if verification[
                        "valid_track_page"
                    ]
                    else "verification_failed"
                )

                manifest[
                    key
                ] = entry

                save_manifest(
                    manifest_file,
                    manifest,
                )

                continue

            if args.cache_only:
                print(
                    "CACHE MISS — REQUEST SUPPRESSED"
                )

                entry[
                    "archive_status"
                ] = "cache_miss"

                manifest[
                    key
                ] = entry

                save_manifest(
                    manifest_file,
                    manifest,
                )

                continue

            if (
                args.limit is not None
                and requests_made
                >= args.limit
            ):
                print()
                print(
                    "REQUEST LIMIT REACHED:",
                    args.limit,
                )
                break

            if page is None:
                print(
                    "Starting Playwright..."
                )

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

                page = (
                    browser.new_page()
                )

            print(
                "REQUEST:",
                url,
            )

            try:
                response = page.goto(
                    url,
                    wait_until=
                        "domcontentloaded",
                    timeout=60000,
                )

            except Exception as exc:
                print(
                    "REQUEST ERROR:",
                    repr(exc),
                )

                entry[
                    "archive_status"
                ] = "request_error"

                entry[
                    "error"
                ] = repr(exc)

                manifest[
                    key
                ] = entry

                save_manifest(
                    manifest_file,
                    manifest,
                )

                continue

            requests_made += 1

            status = (
                response.status
                if response
                else None
            )

            final_url = clean(
                page.url
            )

            print(
                "HTTP STATUS:",
                status,
            )
            print(
                "FINAL URL:",
                final_url,
            )

            entry[
                "http_status"
            ] = status

            entry[
                "final_url"
            ] = final_url

            if status == 429:
                entry[
                    "archive_status"
                ] = "stopped_on_429"

                manifest[
                    key
                ] = entry

                save_manifest(
                    manifest_file,
                    manifest,
                )

                print()
                print(
                    "HTTP 429 encountered."
                )
                print(
                    "Stopping safely. "
                    "Existing archives and "
                    "manifest progress are preserved."
                )
                return

            if status != 200:
                entry[
                    "archive_status"
                ] = (
                    "http_"
                    + str(status)
                )

                manifest[
                    key
                ] = entry

                save_manifest(
                    manifest_file,
                    manifest,
                )

                continue

            html = page.content()

            verification = (
                verify_saved_track(
                    html,
                    url,
                )
            )

            entry.update(
                verification
            )

            if not verification[
                "valid_track_page"
            ]:
                print(
                    "VERIFICATION FAILED — "
                    "HTML NOT SAVED AS VALID TRACK PAGE"
                )

                entry[
                    "archive_status"
                ] = "verification_failed"

                manifest[
                    key
                ] = entry

                save_manifest(
                    manifest_file,
                    manifest,
                )

                continue

            archive_path.write_text(
                html,
                encoding="utf-8",
            )

            entry[
                "archive_status"
            ] = "archived"

            manifest[
                key
            ] = entry

            save_manifest(
                manifest_file,
                manifest,
            )

            print(
                "ARCHIVED:",
                archive_path,
            )
            print(
                "VERIFIED:",
                verification[
                    "source_title"
                ],
                "—",
                verification[
                    "source_artists"
                ],
            )

            print(
                f"Waiting {args.delay:g} seconds..."
            )

            time.sleep(
                args.delay
            )

    finally:
        if browser is not None:
            browser.close()

        if playwright is not None:
            playwright.stop()

    archived = sum(
        1
        for entry in manifest.values()
        if entry.get(
            "archive_status"
        ) == "archived"
    )

    failures = sum(
        1
        for entry in manifest.values()
        if entry.get(
            "archive_status"
        )
        not in {
            "archived",
            "cache_miss",
        }
    )

    print()
    print("=" * 88)
    print("RELATED TRACK ARCHIVE SUMMARY")
    print("=" * 88)
    print(
        "Unique targets:",
        len(targets),
    )
    print(
        "Archived/verified:",
        archived,
    )
    print(
        "Requests made this run:",
        requests_made,
    )
    print(
        "Other failures:",
        failures,
    )
    print(
        "Manifest:",
        manifest_file,
    )


if __name__ == "__main__":
    main()
