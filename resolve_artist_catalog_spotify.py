import argparse
import json
import re
import unicodedata
from pathlib import Path

import pandas as pd

from spotify_metadata import (
    get_spotify_client,
    load_resolution_cache,
    load_track_cache,
    resolution_key,
    resolve_track,
)


SPOTIFY_OUTPUT_COLUMNS = [
    "spotify_match_status",
    "spotify_match_method",
    "spotify_match_score",
    "spotify_match_margin",
    "spotify_track_id",
    "spotify_title",
    "spotify_artist_names",
    "spotify_album_name",
    "spotify_album_release_date",
    "spotify_duration_ms",
]


def clean(value):
    if pd.isna(value):
        return ""
    return str(value).strip()


def ensure_columns(df):
    for column in SPOTIFY_OUTPUT_COLUMNS:
        if column not in df.columns:
            df[column] = ""
    return df


def normalize_year(value):
    value = clean(value)
    return value[:4] if value else None


def normalize_duration(value):
    value = clean(value)
    if not value:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def evidence_from_resolution_cache(cached, track_cache):
    spotify_track_id = clean(cached.get("spotify_track_id"))
    track = {}

    if spotify_track_id:
        track = track_cache.get(f"spotify:{spotify_track_id}", {})

    return {
        "spotify_match_status": clean(cached.get("match_status")),
        "spotify_match_method": clean(cached.get("match_method")),
        "spotify_match_score": cached.get("match_score", ""),
        "spotify_match_margin": cached.get("match_margin", ""),
        "spotify_track_id": spotify_track_id,
        "spotify_title": clean(track.get("title")),
        "spotify_artist_names": clean(track.get("artist_names")),
        "spotify_album_name": clean(track.get("album_name")),
        "spotify_album_release_date": clean(track.get("album_release_date")),
        "spotify_duration_ms": track.get("duration_ms", ""),
    }


def evidence_from_result(result):
    return {
        "spotify_match_status": clean(result.get("match_status")),
        "spotify_match_method": clean(result.get("match_method")),
        "spotify_match_score": result.get("match_score", ""),
        "spotify_match_margin": result.get("match_margin", ""),
        "spotify_track_id": clean(result.get("spotify_track_id")),
        "spotify_title": clean(result.get("title")),
        "spotify_artist_names": clean(result.get("artist_names")),
        "spotify_album_name": clean(result.get("album_name")),
        "spotify_album_release_date": clean(result.get("album_release_date")),
        "spotify_duration_ms": result.get("duration_ms", ""),
    }


def persist_evidence(df, index, evidence):
    for column in SPOTIFY_OUTPUT_COLUMNS:
        value = evidence.get(column, "")

        if value is None:
            value = ""
        else:
            value = str(value)

        df.at[index, column] = value


def save_catalog(df, catalog_file):
    df.to_csv(catalog_file, index=False)



def normalize_identity_name(value):
    value = unicodedata.normalize(
        "NFKD",
        clean(value),
    )
    value = "".join(
        ch for ch in value
        if not unicodedata.combining(ch)
    )
    value = value.casefold()
    value = re.sub(r"[^\w]+", " ", value)
    return " ".join(value.split())


def whosampled_name_from_url(url):
    url = clean(url).rstrip("/")
    if not url:
        return ""
    slug = url.rsplit("/", 1)[-1]
    return slug.replace("-", " ").replace("_", " ").strip()


def load_accepted_artist_equivalences(run_dir):
    review_file = run_dir / "artist_catalog_reviews.json"

    if not review_file.exists():
        return {}, review_file

    data = json.loads(
        review_file.read_text(encoding="utf-8")
    )

    if not isinstance(data, dict):
        raise ValueError(
            "artist_catalog_reviews.json is not a JSON object"
        )

    aliases = {}

    for review in data.values():
        if not isinstance(review, dict):
            continue

        if clean(review.get("decision")).casefold() != "accepted":
            continue

        spotify_name = clean(
            review.get("spotify_artist_name")
        )
        whosampled_name = whosampled_name_from_url(
            review.get("whosampled_artist_url")
        )

        spotify_key = normalize_identity_name(spotify_name)
        whosampled_key = normalize_identity_name(whosampled_name)

        if not spotify_key or not whosampled_key:
            continue

        aliases.setdefault(whosampled_key, set()).add(
            spotify_key
        )

    return aliases, review_file


def boolish(value):
    if value is True:
        return True
    return clean(value).casefold() in {
        "true", "1", "yes"
    }


def artist_identity_upgrade(row, cached, track_cache, aliases):
    """
    Upgrade REVIEW -> MATCHED only when:
      * title_score is exact;
      * release year is the same;
      * no recorded year/title-year/version conflict exists;
      * no version-term disagreement exists;
      * an already-accepted artist identity bridges the
        WhoSampled artist name to the Spotify artist name.

    This intentionally handles only a single source artist and a
    single Spotify candidate artist. Multi-artist cases stay review
    until we implement set-wise canonical artist comparison.
    """
    if clean(cached.get("match_status")).casefold() != "review":
        return False, ""

    spotify_id = clean(cached.get("spotify_track_id"))
    track = track_cache.get(f"spotify:{spotify_id}", {})

    if not isinstance(track, dict):
        return False, ""

    source_artist = clean(
        row.get("whosampled_artist_names")
    )
    spotify_artist = clean(
        track.get("artist_names")
    )

    # Conservative boundary: do not reinterpret multi-artist credits.
    if "," in source_artist or "," in spotify_artist:
        return False, ""

    source_key = normalize_identity_name(source_artist)
    spotify_key = normalize_identity_name(spotify_artist)

    if spotify_key not in aliases.get(source_key, set()):
        return False, ""

    try:
        title_score = float(cached.get("title_score"))
    except (TypeError, ValueError):
        return False, ""

    if title_score < 0.98:
        return False, ""

    year_difference = cached.get("year_difference")
    try:
        year_difference = int(float(year_difference))
    except (TypeError, ValueError):
        return False, ""

    if year_difference != 0:
        return False, ""

    if boolish(cached.get("year_conflict")):
        return False, ""

    if boolish(cached.get("title_year_conflict")):
        return False, ""

    if boolish(cached.get("version_conflict")):
        return False, ""

    target_versions = set(
        cached.get("target_version_terms") or []
    )
    candidate_versions = set(
        cached.get("candidate_version_terms") or []
    )

    if target_versions != candidate_versions:
        return False, ""

    return True, (
        "accepted_artist_identity_exact_title_same_year_no_version_conflict"
    )


def apply_artist_identity_upgrade(row, cached, track_cache, aliases):
    upgraded, method = artist_identity_upgrade(
        row,
        cached,
        track_cache,
        aliases,
    )

    if not upgraded:
        return False

    cached["match_status"] = "matched"
    cached["match_method"] = method
    cached["artist_identity_upgrade"] = True
    cached["artist_identity_upgrade_method"] = method

    return True


def save_resolution_cache_file(resolution_cache):
    path = Path("spotify_cache") / "resolutions.json"
    path.write_text(
        json.dumps(
            resolution_cache,
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Resolve unresolved artist-catalog recordings against Spotify "
            "while preserving canonical identity."
        )
    )
    parser.add_argument(
        "--run-dir",
        required=True,
        help="Pipeline run directory containing artist_catalog_recordings_parsed.csv",
    )
    parser.add_argument(
        "--request-limit",
        type=int,
        default=None,
        help="Maximum number of previously uncached Spotify resolution attempts.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Show artist-identity-aware status changes without "
            "modifying the catalog or resolution cache."
        ),
    )
    args = parser.parse_args()

    if args.request_limit is not None and args.request_limit < 0:
        raise ValueError("--request-limit must be >= 0")

    run_dir = Path(args.run_dir)
    catalog_file = run_dir / "artist_catalog_recordings_parsed.csv"

    if not catalog_file.exists():
        raise FileNotFoundError(catalog_file)

    catalog = pd.read_csv(catalog_file, dtype=str).fillna("")
    catalog = ensure_columns(catalog)

    canonical_before = (
        catalog["canonical_recording_id"].map(clean).tolist()
    )

    resolution_cache = load_resolution_cache()
    track_cache = load_track_cache()
    attempted_before = set(resolution_cache.keys())

    accepted_artist_aliases, artist_review_file = (
        load_accepted_artist_equivalences(run_dir)
    )

    cached_rows = 0
    artist_identity_upgrades = 0
    new_requests = 0
    skipped_canonical = 0
    untouched_due_to_limit = 0
    status_counts = {}
    spotify_client = None

    print("=" * 100)
    print("ARTIST-CATALOG SPOTIFY RESOLUTION")
    print("=" * 100)
    print()
    print("Catalog recordings:", len(catalog))
    print(
        "Request limit:",
        args.request_limit if args.request_limit is not None else "none",
    )
    print(
        "Accepted artist alias bridges:",
        sum(len(v) for v in accepted_artist_aliases.values()),
    )
    print(
        "Artist review file:",
        artist_review_file,
    )
    print("Dry run:", args.dry_run)

    for index, row in catalog.iterrows():
        recording_id = clean(row.get("recording_id"))
        canonical_recording_id = clean(row.get("canonical_recording_id"))

        if canonical_recording_id:
            skipped_canonical += 1
            continue

        title = clean(row.get("whosampled_title"))
        artists = clean(row.get("whosampled_artist_names"))
        year = normalize_year(row.get("whosampled_release_year"))
        duration_ms = normalize_duration(row.get("whosampled_duration_ms"))

        key = resolution_key(title, artists, year)

        # Wrapper checkpoint: any existing resolution means this catalog
        # recording has already had a Spotify resolution attempt.
        if key in resolution_cache:
            cached = resolution_cache[key]

            upgraded = apply_artist_identity_upgrade(
                row,
                cached,
                track_cache,
                accepted_artist_aliases,
            )

            if upgraded:
                artist_identity_upgrades += 1
                print()
                print(
                    "ARTIST-IDENTITY UPGRADE:",
                    recording_id,
                    "|",
                    title,
                    "| review -> matched",
                )

            evidence = evidence_from_resolution_cache(cached, track_cache)
            persist_evidence(catalog, index, evidence)
            cached_rows += 1

            status = clean(evidence.get("spotify_match_status"))
            status_counts[status] = status_counts.get(status, 0) + 1

            print()
            print("CACHE:", recording_id, "|", title, "|", status)
            continue

        if (
            args.request_limit is not None
            and new_requests >= args.request_limit
        ):
            untouched_due_to_limit += 1
            continue

        if spotify_client is None:
            spotify_client = get_spotify_client()

        print()
        print(f"REQUESTING [{new_requests + 1}]")
        print("recording_id:", recording_id)
        print("title:", repr(title))
        print("artists:", repr(artists))
        print("year:", repr(year))

        result = resolve_track(
            title=title,
            artists=artists,
            year=year,
            duration_ms=duration_ms,
            sp=spotify_client,
            track_cache=track_cache,
            resolution_cache=resolution_cache,
        )

        new_requests += 1

        cached = resolution_cache.get(key, {})
        upgraded = False

        if isinstance(cached, dict):
            upgraded = apply_artist_identity_upgrade(
                row,
                cached,
                track_cache,
                accepted_artist_aliases,
            )

        if upgraded:
            artist_identity_upgrades += 1
            evidence = evidence_from_resolution_cache(
                cached,
                track_cache,
            )
            print(
                "artist_identity_upgrade:",
                "review -> matched",
            )
        else:
            evidence = evidence_from_result(result)

        persist_evidence(catalog, index, evidence)

        status = clean(evidence.get("spotify_match_status"))
        status_counts[status] = status_counts.get(status, 0) + 1

        print("status:", status)
        print("spotify_track_id:", evidence.get("spotify_track_id"))
        print("spotify_title:", evidence.get("spotify_title"))
        print("spotify_artists:", evidence.get("spotify_artist_names"))
        print("score:", evidence.get("spotify_match_score"))
        print("margin:", evidence.get("spotify_match_margin"))

        # Checkpoint after every completed recording.
        if not args.dry_run:
            save_catalog(catalog, catalog_file)
            save_resolution_cache_file(resolution_cache)

    canonical_after = (
        catalog["canonical_recording_id"].map(clean).tolist()
    )

    if canonical_after != canonical_before:
        raise RuntimeError(
            "SAFETY FAILURE: canonical_recording_id changed"
        )

    if not args.dry_run:
        save_catalog(catalog, catalog_file)
        save_resolution_cache_file(resolution_cache)

    print()
    print("=" * 100)
    print("SUMMARY")
    print("=" * 100)
    print()
    print("Skipped already canonical:", skipped_canonical)
    print("Existing cached attempts consumed:", cached_rows)
    print("New Spotify attempts:", new_requests)
    print("Untouched due to request limit:", untouched_due_to_limit)

    print()
    print("Statuses persisted:")
    if status_counts:
        for status in sorted(status_counts):
            print(f"  {status or '<blank>'}: {status_counts[status]}")
    else:
        print("  <none>")

    print()
    print(
        "Canonical IDs before:",
        sum(bool(value) for value in canonical_before),
    )
    print(
        "Canonical IDs after:",
        sum(bool(value) for value in canonical_after),
    )

    attempted_after = set(resolution_cache.keys())
    new_cache_keys = attempted_after - attempted_before

    print("New resolution-cache keys:", len(new_cache_keys))
    print(
        "Artist-identity review -> matched upgrades:",
        artist_identity_upgrades,
    )
    print()
    print("Catalog file:", catalog_file)
    print()
    if args.dry_run:
        print("DRY RUN: no catalog/cache files were modified.")
    print("No canonical reconciliation was performed.")
    print("No contributor review UI was invoked.")


if __name__ == "__main__":
    main()
