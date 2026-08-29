#!/usr/bin/env python3

import argparse
import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
from playwright.async_api import async_playwright

from parse_whosampled_relationship import parse_relationship


DEFAULT_DELAY = 12.0


def clean(value):
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def norm_url(value):
    value = clean(value)
    if not value:
        return ""
    return value.rstrip("/") + "/"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def relationship_identity(url):
    path = urlparse(clean(url)).path
    match = re.search(
        r"/(sample|cover|remix|interpolation)/(\d+)/",
        path,
        flags=re.I,
    )
    if not match:
        return None, None
    return match.group(1).lower(), match.group(2)


def archive_name(url):
    kind, rel_id = relationship_identity(url)
    if not kind or not rel_id:
        raise ValueError(
            f"Could not determine relationship type/id from URL: {url}"
        )
    return f"relationship_detail_{kind}_{rel_id}.html"


def compact_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def read_csv(path):
    return pd.read_csv(
        path,
        dtype=str,
        keep_default_na=False,
    )


def ensure_columns(df):
    columns = [
        "relationship_detail_archive_path",
        "relationship_detail_status",
        "relationship_detail_http_status",
        "relationship_detail_final_url",
        "relationship_detail_parsed_at",
        "relationship_detail_error",
        "relationship_detail_whosampled_id",
        "relationship_detail_parsed_type",
        "relationship_detail_sample_type",
        "relationship_detail_source_side",
        "relationship_detail_target_side",
        "relationship_detail_source_track_json",
        "relationship_detail_target_track_json",
        "target_whosampled_url",
        "target_track",
        "target_artists_json",
        "target_year",
        "target_album",
        "target_label",
    ]
    for column in columns:
        if column not in df.columns:
            df[column] = ""
        else:
            df[column] = df[column].astype("string").fillna("")
    return columns


def canonical_recording_index(recordings):
    by_id = {}
    for _, row in recordings.iterrows():
        rid = clean(row.get("recording_id"))
        if rid:
            by_id[rid] = row
    return by_id


def existing_relationship_urls(relationships):
    return {
        norm_url(v)
        for v in relationships.get(
            "whosampled_relationship_url",
            pd.Series(dtype=str),
        )
        if norm_url(v)
    }


def find_existing_archive(run_dir, archive_dir, rel_url):
    """
    Cache-first lookup.

    Primary permanent cache:
      <archive_dir>/relationship_detail_<kind>_<id>.html

    Also recognizes earlier test/checkpoint locations so a page already
    fetched during development is not requested again.
    """
    name = archive_name(rel_url)

    candidates = [
        archive_dir / name,
        run_dir / "relationship_pages_test" / name,
    ]

    kind, rel_id = relationship_identity(rel_url)

    if kind and rel_id:
        candidates.extend([
            run_dir
            / "relationship_pages_test"
            / f"catalog_relationship_{kind}_{rel_id}.html",

            run_dir
            / "relationship_pages_test"
            / f"relationship_detail_{rel_id}.html",

            run_dir
            / "whosampled_pages"
            / name,

            run_dir / name,
        ])

    for candidate in candidates:
        if candidate.exists():
            return candidate

    # Last-resort exact-ID scan in known relationship cache directories.
    search_dirs = [
        archive_dir,
        run_dir / "relationship_pages_test",
        run_dir / "whosampled_pages",
    ]

    if rel_id:
        for directory in search_dirs:
            if not directory.exists():
                continue
            for candidate in directory.glob(f"*{rel_id}*.html"):
                if candidate.is_file():
                    return candidate

    return None


def orient_relationship(parsed, canonical_source_url):
    t1 = parsed.get("track_1") or {}
    t2 = parsed.get("track_2") or {}

    source_url = norm_url(canonical_source_url)
    t1_url = norm_url(t1.get("url"))
    t2_url = norm_url(t2.get("url"))

    if not source_url:
        raise RuntimeError(
            "Canonical source Recording has no WhoSampled URL."
        )

    if not t1_url or not t2_url:
        raise RuntimeError(
            "Parsed relationship did not provide URLs for both tracks."
        )

    m1 = source_url == t1_url
    m2 = source_url == t2_url

    if m1 == m2:
        if m1:
            raise RuntimeError(
                "Canonical source WhoSampled URL matched both parsed tracks."
            )
        raise RuntimeError(
            "Canonical source WhoSampled URL matched neither parsed track."
        )

    if m1:
        return "track_1", "track_2", t1, t2

    return "track_2", "track_1", t2, t1


def persist_queue(queue, queue_path):
    queue.to_csv(
        queue_path,
        index=False,
    )


def apply_parsed_result(
    queue,
    idx,
    parsed,
    archive_path,
    canonical_source_url,
    http_status="",
    final_url="",
):
    source_side, target_side, source_track, target_track = (
        orient_relationship(
            parsed,
            canonical_source_url,
        )
    )

    queue.at[idx, "relationship_detail_archive_path"] = str(
        archive_path
    )
    queue.at[idx, "relationship_detail_status"] = "parsed"
    queue.at[idx, "relationship_detail_http_status"] = clean(
        http_status
    )
    queue.at[idx, "relationship_detail_final_url"] = clean(
        final_url
    )
    queue.at[idx, "relationship_detail_parsed_at"] = utc_now()
    queue.at[idx, "relationship_detail_error"] = ""

    queue.at[
        idx,
        "relationship_detail_whosampled_id",
    ] = clean(parsed.get("whosampled_id"))

    queue.at[
        idx,
        "relationship_detail_parsed_type",
    ] = clean(parsed.get("relationship_type"))

    queue.at[
        idx,
        "relationship_detail_sample_type",
    ] = clean(parsed.get("sample_type"))

    queue.at[
        idx,
        "relationship_detail_source_side",
    ] = source_side

    queue.at[
        idx,
        "relationship_detail_target_side",
    ] = target_side

    queue.at[
        idx,
        "relationship_detail_source_track_json",
    ] = compact_json(source_track)

    queue.at[
        idx,
        "relationship_detail_target_track_json",
    ] = compact_json(target_track)

    queue.at[
        idx,
        "target_whosampled_url",
    ] = norm_url(target_track.get("url"))

    queue.at[
        idx,
        "target_track",
    ] = clean(target_track.get("name"))

    queue.at[
        idx,
        "target_artists_json",
    ] = compact_json(target_track.get("artists") or [])

    queue.at[
        idx,
        "target_year",
    ] = clean(target_track.get("year"))

    queue.at[
        idx,
        "target_album",
    ] = clean(target_track.get("album"))

    queue.at[
        idx,
        "target_label",
    ] = clean(target_track.get("label"))

    return source_track, target_track


def validate_parsed_against_queue(row, parsed):
    queue_type = clean(row.get("relationship_type"))
    parsed_type = clean(parsed.get("relationship_type"))

    # Current staging uses "covered"; parser uses "covers".
    equivalent = {
        "sampled": "sampled",
        "covered": "covers",
        "covers": "covers",
        "remixed": "remix",
        "remix": "remix",
        "interpolated": "interpolation",
        "interpolation": "interpolation",
    }

    expected = equivalent.get(queue_type, queue_type)
    actual = equivalent.get(parsed_type, parsed_type)

    if expected and actual and expected != actual:
        raise RuntimeError(
            "Relationship type disagreement: "
            f"queue={queue_type!r}, parser={parsed_type!r}"
        )


async def fetch_page(browser, url, output_path, wait_seconds):
    context = await browser.new_context(
        viewport={
            "width": 1440,
            "height": 900,
        },
        locale="en-US",
    )

    try:
        page = await context.new_page()

        response = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60_000,
        )

        status = response.status if response else None
        final_url = page.url

        if status != 200:
            return status, final_url, None

        await asyncio.sleep(wait_seconds)

        html = await page.content()

        output_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        output_path.write_text(
            html,
            encoding="utf-8",
        )

        return status, final_url, html

    finally:
        await context.close()


async def run(args):
    run_dir = Path(args.run_dir)

    queue_path = (
        run_dir
        / "artist_catalog_relationship_queue.csv"
    )
    recordings_path = run_dir / "recordings.csv"
    relationships_path = run_dir / "relationships.csv"

    for path in [
        queue_path,
        recordings_path,
        relationships_path,
    ]:
        if not path.exists():
            raise SystemExit(
                f"Required file not found: {path}"
            )

    queue = read_csv(queue_path)
    recordings = read_csv(recordings_path)
    relationships = read_csv(relationships_path)

    ensure_columns(queue)

    recordings_by_id = canonical_recording_index(
        recordings
    )
    canonical_ids = set(recordings_by_id)

    canonical_rel_urls = existing_relationship_urls(
        relationships
    )

    archive_dir = (
        run_dir
        / "relationship_detail_pages"
    )

    # Eligibility is recalculated from current canonical state.
    eligible = []

    for idx, row in queue.iterrows():
        source_id = clean(
            row.get("canonical_source_recording_id")
        )
        rel_url = norm_url(
            row.get("whosampled_relationship_url")
        )

        if source_id not in canonical_ids:
            continue

        if not rel_url:
            continue

        if rel_url in canonical_rel_urls:
            continue

        eligible.append(idx)

    print("=" * 100)
    print("ARTIST CATALOG RELATIONSHIP-DETAIL EVIDENCE COLLECTOR")
    print("=" * 100)

    print("\nCURRENT")
    print("-" * 100)
    print("Queue rows:", len(queue))
    print(
        "Canonical-source actionable relationships:",
        len(eligible),
    )
    print(
        "Deferred because source is not canonical:",
        sum(
            clean(row.get("canonical_source_recording_id"))
            not in canonical_ids
            for _, row in queue.iterrows()
        ),
    )
    print(
        "Relationship URLs already canonical:",
        sum(
            norm_url(row.get("whosampled_relationship_url"))
            in canonical_rel_urls
            and clean(
                row.get("canonical_source_recording_id")
            ) in canonical_ids
            for _, row in queue.iterrows()
        ),
    )

    already_parsed = 0
    cache_available = 0
    network_needed = 0

    for idx in eligible:
        row = queue.loc[idx]

        if (
            clean(row.get("relationship_detail_status"))
            == "parsed"
            and clean(row.get("target_whosampled_url"))
        ):
            already_parsed += 1
            continue

        cached = find_existing_archive(
            run_dir,
            archive_dir,
            row["whosampled_relationship_url"],
        )

        if cached:
            cache_available += 1
        else:
            network_needed += 1

    print("\nCHECKPOINT STATE")
    print("-" * 100)
    print("Already parsed:", already_parsed)
    print("Unparsed with local archive:", cache_available)
    print("Unparsed requiring network:", network_needed)

    if args.dry_run:
        print("\nDRY RUN PASSED.")
        print("No network requests made.")
        print("No files modified.")
        return

    requests_made = 0
    parsed_count = 0
    cache_count = 0
    failure_count = 0

    browser = None
    playwright = None

    try:
        for position, idx in enumerate(
            eligible,
            start=1,
        ):
            row = queue.loc[idx]

            rel_url = norm_url(
                row.get("whosampled_relationship_url")
            )

            source_id = clean(
                row.get("canonical_source_recording_id")
            )

            source_recording = recordings_by_id[source_id]
            source_ws_url = norm_url(
                source_recording.get("whosampled_url")
            )

            if (
                clean(row.get("relationship_detail_status"))
                == "parsed"
                and clean(row.get("target_whosampled_url"))
            ):
                print(
                    f"[{position}/{len(eligible)}] "
                    f"SKIP PARSED: {rel_url}"
                )
                continue

            archive_path = find_existing_archive(
                run_dir,
                archive_dir,
                rel_url,
            )

            from_cache = archive_path is not None
            http_status = ""
            final_url = ""

            print()
            print(
                f"[{position}/{len(eligible)}] "
                f"{clean(row.get('related_artist'))} - "
                f"{clean(row.get('related_track'))}"
            )
            print("SOURCE:", source_id)
            print("RELATIONSHIP:", rel_url)

            try:
                if from_cache:
                    print("CACHE:", archive_path)
                    cache_count += 1

                else:
                    if (
                        args.request_limit is not None
                        and requests_made >= args.request_limit
                    ):
                        print()
                        print(
                            "REQUEST LIMIT REACHED:",
                            args.request_limit,
                        )
                        break

                    if playwright is None:
                        playwright = await async_playwright().start()
                        browser = await playwright.chromium.launch(
                            headless=False
                        )

                    archive_path = (
                        archive_dir
                        / archive_name(rel_url)
                    )

                    print("REQUESTING:", rel_url)

                    (
                        http_status,
                        final_url,
                        html,
                    ) = await fetch_page(
                        browser,
                        rel_url,
                        archive_path,
                        args.page_wait,
                    )

                    requests_made += 1

                    print("STATUS:", http_status)
                    print("FINAL URL:", final_url)

                    if http_status != 200:
                        raise RuntimeError(
                            f"HTTP status {http_status}"
                        )

                    print("ARCHIVED:", archive_path)

                parsed = parse_relationship(
                    archive_path,
                    supplied_url=rel_url,
                )

                validate_parsed_against_queue(
                    row,
                    parsed,
                )

                source_track, target_track = (
                    apply_parsed_result(
                        queue,
                        idx,
                        parsed,
                        archive_path,
                        source_ws_url,
                        http_status=http_status,
                        final_url=final_url,
                    )
                )

                parsed_count += 1

                print(
                    "ORIENTED SOURCE:",
                    clean(source_track.get("name")),
                    "|",
                    norm_url(source_track.get("url")),
                )

                print(
                    "ORIENTED TARGET:",
                    clean(target_track.get("name")),
                    "|",
                    norm_url(target_track.get("url")),
                )

                # Checkpoint after every successful parse.
                persist_queue(
                    queue,
                    queue_path,
                )

                print("CHECKPOINT SAVED.")

            except Exception as exc:
                failure_count += 1

                queue.at[
                    idx,
                    "relationship_detail_status",
                ] = "error"

                queue.at[
                    idx,
                    "relationship_detail_error",
                ] = f"{type(exc).__name__}: {exc}"

                if archive_path:
                    queue.at[
                        idx,
                        "relationship_detail_archive_path",
                    ] = str(archive_path)

                if http_status != "":
                    queue.at[
                        idx,
                        "relationship_detail_http_status",
                    ] = clean(http_status)

                if final_url:
                    queue.at[
                        idx,
                        "relationship_detail_final_url",
                    ] = clean(final_url)

                persist_queue(
                    queue,
                    queue_path,
                )

                print(
                    "ERROR:",
                    f"{type(exc).__name__}: {exc}",
                )
                print("ERROR CHECKPOINT SAVED.")

                if args.stop_on_error:
                    raise

            # Delay only after an actual live request and only if
            # another request may occur.
            if not from_cache and args.delay > 0:
                await asyncio.sleep(args.delay)

    finally:
        if browser is not None:
            await browser.close()
        if playwright is not None:
            await playwright.stop()

    print()
    print("=" * 100)
    print("RUN SUMMARY")
    print("=" * 100)
    print("Parsed this run:", parsed_count)
    print("Parsed from cache:", cache_count)
    print("Live requests made:", requests_made)
    print("Failures:", failure_count)

    refreshed = read_csv(queue_path)

    actionable_mask = (
        refreshed["canonical_source_recording_id"]
        .map(clean)
        .isin(canonical_ids)
        &
        ~refreshed["whosampled_relationship_url"]
        .map(norm_url)
        .isin(canonical_rel_urls)
    )

    actionable = refreshed[actionable_mask]

    complete = (
        actionable["relationship_detail_status"]
        .map(clean)
        .eq("parsed")
        &
        actionable["target_whosampled_url"]
        .map(clean)
        .ne("")
    )

    print(
        "Actionable relationships parsed:",
        int(complete.sum()),
        "/",
        len(actionable),
    )

    print(
        "Actionable relationships remaining:",
        int((~complete).sum()),
    )

    print()
    print(
        "Canonical recordings.csv and relationships.csv "
        "were not modified."
    )


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Archive and parse relationship-detail pages for "
            "artist-catalog relationships whose source Recording "
            "is already canonical."
        )
    )

    parser.add_argument(
        "--run-dir",
        required=True,
        help="Playlist run directory",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Report current eligibility/cache state only; "
            "make no network requests and modify no files."
        ),
    )

    parser.add_argument(
        "--request-limit",
        type=int,
        default=None,
        help=(
            "Maximum number of live WhoSampled requests. "
            "Cached pages do not count against this limit."
        ),
    )

    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY,
        help=(
            "Seconds to wait after each live request "
            f"(default: {DEFAULT_DELAY})."
        ),
    )

    parser.add_argument(
        "--page-wait",
        type=float,
        default=10.0,
        help=(
            "Seconds to wait after DOMContentLoaded before "
            "capturing rendered HTML (default: 10)."
        ),
    )

    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Stop immediately on the first row error.",
    )

    return parser


def main():
    args = build_parser().parse_args()

    if args.request_limit is not None and args.request_limit < 0:
        raise SystemExit(
            "--request-limit must be >= 0"
        )

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
