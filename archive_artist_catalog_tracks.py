import argparse
import csv
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

from playwright.sync_api import sync_playwright

from evidence_store import EvidenceStore


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


def save_json(path, data):
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    path.write_text(
        json.dumps(
            data,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
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


def safe_name(value):
    value = re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        clean(value),
    ).strip("_")

    return value or "track"


def track_slug_parts(url):
    parts = [
        unquote(part)
        for part
        in urlparse(
            clean(url)
        ).path.strip("/").split("/")
        if part
    ]

    if len(parts) >= 2:
        return (
            parts[0],
            parts[1],
        )

    return (
        "artist",
        "track",
    )


def archive_name(url):
    artist_slug, track_slug = (
        track_slug_parts(
            url
        )
    )

    digest = hashlib.sha1(
        normalized_url(
            url
        ).encode(
            "utf-8"
        )
    ).hexdigest()[:10]

    return (
        safe_name(
            artist_slug
        )
        + "__"
        + safe_name(
            track_slug
        )
        + "__"
        + digest
        + ".html"
    )


def write_tracks_csv(
    path,
    rows,
    fieldnames,
):
    with path.open(
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
            rows
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
        "--limit",
        type=int,
        default=None,
        help=(
            "Maximum NEW WhoSampled track-page "
            "requests this run."
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

    parser.add_argument(
        "--evidence-root",
        default="whosampled_evidence",
        help=(
            "Centralized read-only evidence store."
        ),
    )

    args = parser.parse_args()

    evidence_store = EvidenceStore(
        args.evidence_root
    )

    run_dir = Path(
        args.run_dir
    )

    tracks_file = (
        run_dir
        / "artist_catalog_tracks.csv"
    )

    if not tracks_file.exists():
        raise SystemExit(
            f"Missing: {tracks_file}"
        )

    archive_dir = (
        run_dir
        / "artist_catalog_track_pages"
    )

    archive_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    manifest_file = (
        archive_dir
        / "manifest.json"
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

    with tracks_file.open(
        "r",
        encoding="utf-8",
        newline="",
    ) as f:
        reader = csv.DictReader(
            f
        )

        rows = list(
            reader
        )

        fieldnames = list(
            reader.fieldnames
            or []
        )

    for extra in (
        "track_archive_status",
        "track_archive_path",
        "track_archive_http_status",
        "track_archive_final_url",
    ):
        if extra not in fieldnames:
            fieldnames.append(
                extra
            )

    # --------------------------------------------------------
    # Build one request target per unique track URL.
    #
    # Multiple catalog-role assertions can point to the same
    # recording; they must share one archived track page.
    # --------------------------------------------------------

    unique_targets = {}

    for index, row in enumerate(
        rows
    ):
        url = clean(
            row.get(
                "whosampled_track_url"
            )
        )

        if not url:
            continue

        key = normalized_url(
            url
        )

        unique_targets.setdefault(
            key,
            {
                "url":
                    url,

                "row_indexes":
                    [],
            },
        )

        unique_targets[
            key
        ][
            "row_indexes"
        ].append(
            index
        )

    print("=" * 100)
    print(
        "ARTIST CATALOG TRACK PAGE ARCHIVER"
    )
    print("=" * 100)
    print()
    print(
        "Catalog assertions:",
        len(rows),
    )
    print(
        "Unique track URLs:",
        len(unique_targets),
    )
    print(
        "Delay:",
        args.delay,
        "seconds",
    )

    requests_made = 0
    archived = 0
    cache_hits = 0
    evidence_store_hits = 0
    evidence_store_ambiguous = 0
    failures = 0

    playwright = None
    browser = None
    context = None
    page = None

    try:
        for number, (
            key,
            target,
        ) in enumerate(
            unique_targets.items(),
            start=1,
        ):

            url = target[
                "url"
            ]

            path = (
                archive_dir
                / archive_name(
                    url
                )
            )

            print()
            print("-" * 100)
            print(
                f"[{number}/{len(unique_targets)}]"
            )
            print(
                "URL:",
                url,
            )

            status = None
            final_url = url
            result_path = None

            # ------------------------------------------------
            # LOCAL RUN CACHE HIT
            # ------------------------------------------------

            if path.exists():

                print(
                    "CACHE HIT:",
                    path,
                )

                cache_hits += 1
                result_path = path

                stored = manifest.get(
                    key,
                    {},
                )

                if isinstance(
                    stored,
                    dict,
                ):
                    status = stored.get(
                        "http_status"
                    )

                    final_url = clean(
                        stored.get(
                            "final_url"
                        )
                    ) or url

                result_status = (
                    "archived"
                )

            # ------------------------------------------------
            # CENTRALIZED EVIDENCE STORE
            # ------------------------------------------------

            else:

                evidence_matches = (
                    evidence_store.lookup(
                        url,
                        evidence_type=(
                            "artist_catalog_track_pages"
                        ),
                    )
                )

                if len(evidence_matches) == 1:

                    record = evidence_matches[0]

                    evidence_store.read(
                        record
                    )

                    result_path = (
                        evidence_store.root
                        / record["relative_path"]
                    )

                    print(
                        "EVIDENCE STORE HIT:",
                        result_path,
                    )

                    evidence_store_hits += 1

                    final_url = record[
                        "source_url"
                    ]

                    result_status = (
                        "evidence_store_hit"
                    )

                elif len(evidence_matches) > 1:

                    print(
                        "EVIDENCE STORE AMBIGUOUS:",
                        len(evidence_matches),
                        "matching captures",
                    )

                    for record in evidence_matches:
                        print(
                            "  ",
                            record["relative_path"],
                            record["sha256"],
                        )

                    evidence_store_ambiguous += 1

                    result_status = (
                        "evidence_store_ambiguous"
                    )

                    result_path = None

                # --------------------------------------------
                # CACHE-ONLY MISS
                # --------------------------------------------

                elif args.cache_only:

                    print(
                        "CACHE MISS — REQUEST SUPPRESSED"
                    )

                    result_status = (
                        "pending"
                    )

                    continue

                # --------------------------------------------
                # LIVE REQUEST
                # --------------------------------------------

                else:

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
                        url,
                    )

                    try:
                        response = page.goto(
                            url,
                            wait_until=
                                "domcontentloaded",
                            timeout=60000,
                        )

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

                        if status == 429:

                            manifest[
                                key
                            ] = {
                                "requested_url":
                                    url,

                                "final_url":
                                    final_url,

                                "http_status":
                                    status,

                                "archive_path":
                                    "",

                                "status":
                                    "rate_limited",
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
                                "ARCHIVE FAILED"
                            )

                            result_status = (
                                "failed"
                            )

                            failures += 1

                            manifest[
                                key
                            ] = {
                                "requested_url":
                                    url,

                                "final_url":
                                    final_url,

                                "http_status":
                                    status,

                                "archive_path":
                                    "",

                                "status":
                                    result_status,
                            }

                            save_json(
                                manifest_file,
                                manifest,
                            )

                            continue

                        html = page.content()

                        path.write_text(
                            html,
                            encoding="utf-8",
                        )

                        result_path = path

                        print(
                            "ARCHIVED:",
                            path,
                        )

                        result_status = (
                            "archived"
                        )

                        archived += 1

                        manifest[
                            key
                        ] = {
                            "requested_url":
                                url,

                            "final_url":
                                final_url,

                            "http_status":
                                status,

                            "archive_path":
                                str(
                                    path
                                ),

                            "status":
                                result_status,
                        }

                        save_json(
                            manifest_file,
                            manifest,
                        )

                    except SystemExit:
                        raise

                    except Exception as exc:

                        print(
                            "ARCHIVE ERROR:",
                            repr(exc),
                        )

                        result_status = (
                            "failed"
                        )

                        failures += 1

                        manifest[
                            key
                        ] = {
                            "requested_url":
                                url,

                            "final_url":
                                final_url,

                            "http_status":
                                status,

                            "archive_path":
                                "",

                            "status":
                                result_status,

                            "error":
                                repr(exc),
                        }

                        save_json(
                            manifest_file,
                            manifest,
                        )

                        continue

            # ------------------------------------------------
            # Write archive result back to every catalog-role
            # assertion for this recording.
            # ------------------------------------------------

            for row_index in target[
                "row_indexes"
            ]:

                rows[
                    row_index
                ][
                    "track_archive_status"
                ] = result_status

                rows[
                    row_index
                ][
                    "track_archive_path"
                ] = (
                    str(result_path)
                    if result_path is not None
                    else ""
                )

                rows[
                    row_index
                ][
                    "track_archive_http_status"
                ] = (
                    ""
                    if status is None
                    else str(
                        status
                    )
                )

                rows[
                    row_index
                ][
                    "track_archive_final_url"
                ] = final_url

            # Checkpoint CSV after every completed target.
            write_tracks_csv(
                tracks_file,
                rows,
                fieldnames,
            )

            # Delay only after an actual live request.
            if (
                not args.cache_only
                and path.exists()
                and requests_made > 0
                and (
                    args.limit is None
                    or requests_made
                    < args.limit
                )
            ):
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

    # Final write.
    write_tracks_csv(
        tracks_file,
        rows,
        fieldnames,
    )

    print()
    print("=" * 100)
    print(
        "CATALOG TRACK ARCHIVE SUMMARY"
    )
    print("=" * 100)
    print(
        "Unique track URLs:",
        len(unique_targets),
    )
    print(
        "Archived this run:",
        archived,
    )
    print(
        "Cache hits:",
        cache_hits,
    )
    print(
        "EvidenceStore hits:",
        evidence_store_hits,
    )
    print(
        "EvidenceStore ambiguous:",
        evidence_store_ambiguous,
    )
    print(
        "Live requests:",
        requests_made,
    )
    print(
        "Failures:",
        failures,
    )
    print(
        "Manifest:",
        manifest_file,
    )
    print()
    print(
        "No relationship-detail pages were requested."
    )
    print(
        "No Spotify requests were made."
    )


if __name__ == "__main__":
    main()
