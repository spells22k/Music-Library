import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


def clean(value):
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def backup_file(path, suffix):
    path = Path(path)
    if not path.exists():
        return None

    backup = path.with_name(
        path.stem + suffix + path.suffix
    )

    shutil.copy2(
        path,
        backup,
    )

    return backup


def ensure_column(df, name, default=""):
    if name not in df.columns:
        df[name] = default


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--run-dir",
        default="runs/playlist_3XtRerTr3ndS88v51AAixb",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned changes without modifying files.",
    )

    args = parser.parse_args()

    run_dir = Path(args.run_dir)

    recordings_file = (
        run_dir
        / "recordings.csv"
    )

    reviews_file = (
        run_dir
        / "artist_catalog_spotify_reviews.json"
    )

    catalog_recordings_file = (
        run_dir
        / "artist_catalog_recordings_parsed.csv"
    )

    catalog_credits_file = (
        run_dir
        / "artist_catalog_credits_parsed.csv"
    )

    relationship_queue_file = (
        run_dir
        / "artist_catalog_relationship_queue.csv"
    )

    spotify_track_cache_file = (
        Path("spotify_cache")
        / "tracks.json"
    )

    required = [
        recordings_file,
        reviews_file,
        catalog_recordings_file,
        catalog_credits_file,
        relationship_queue_file,
        spotify_track_cache_file,
    ]

    for path in required:
        if not path.exists():
            raise SystemExit(
                f"Missing required file: {path}"
            )

    # --------------------------------------------------------
    # Load data.
    # --------------------------------------------------------

    recordings_df = pd.read_csv(
        recordings_file,
        dtype=str,
    ).fillna("")

    catalog_recordings_df = pd.read_csv(
        catalog_recordings_file,
        dtype=str,
    ).fillna("")

    catalog_credits_df = pd.read_csv(
        catalog_credits_file,
        dtype=str,
    ).fillna("")

    relationship_queue_df = pd.read_csv(
        relationship_queue_file,
        dtype=str,
    ).fillna("")

    reviews = json.loads(
        reviews_file.read_text(
            encoding="utf-8"
        )
    )

    if not isinstance(
        reviews,
        dict,
    ):
        raise SystemExit(
            "artist_catalog_spotify_reviews.json "
            "is not a JSON object."
        )

    spotify_track_cache = json.loads(
        spotify_track_cache_file.read_text(
            encoding="utf-8"
        )
    )

    if not isinstance(
        spotify_track_cache,
        dict,
    ):
        raise SystemExit(
            "spotify_cache/tracks.json "
            "is not a JSON object."
        )

    # --------------------------------------------------------
    # Back up every file this script mutates.
    # --------------------------------------------------------

    suffix = (
        ".before_artist_catalog_recording_reconciliation"
    )

    backups = []

    if not args.dry_run:
        for path in (
            recordings_file,
            catalog_recordings_file,
            catalog_credits_file,
            relationship_queue_file,
        ):
            backup = backup_file(
                path,
                suffix,
            )

            if backup:
                backups.append(
                    backup
                )

    # --------------------------------------------------------
    # Ensure reconciliation/provenance fields exist.
    # --------------------------------------------------------

    for column in (
        "catalog_reconciliation_status",
        "catalog_review_score",
        "catalog_provisional_recording_id",
        "catalog_reconciled_at",
    ):
        ensure_column(
            recordings_df,
            column,
        )

    for column in (
        "canonical_recording_id",
        "catalog_reconciliation_status",
        "catalog_reconciled_at",
    ):
        ensure_column(
            catalog_recordings_df,
            column,
        )

    for column in (
        "canonical_recording_id",
        "catalog_reconciliation_status",
        "catalog_reconciled_at",
    ):
        ensure_column(
            catalog_credits_df,
            column,
        )

    for column in (
        "canonical_source_recording_id",
        "catalog_reconciliation_status",
        "catalog_reconciled_at",
    ):
        ensure_column(
            relationship_queue_df,
            column,
        )

    # --------------------------------------------------------
    # Build local lookup indexes.
    # --------------------------------------------------------

    spotify_to_canonical = {}

    for idx, row in recordings_df.iterrows():

        spotify_track_id = clean(
            row.get(
                "spotify_track_id"
            )
        )

        canonical_id = clean(
            row.get(
                "recording_id"
            )
        )

        if (
            spotify_track_id
            and canonical_id
        ):
            spotify_to_canonical[
                spotify_track_id
            ] = (
                idx,
                canonical_id,
            )

    catalog_by_provisional = {}

    for idx, row in catalog_recordings_df.iterrows():

        provisional_id = clean(
            row.get(
                "recording_id"
            )
        )

        if provisional_id:
            catalog_by_provisional[
                provisional_id
            ] = idx

    # --------------------------------------------------------
    # Reconcile accepted catalog↔Spotify reviews.
    # --------------------------------------------------------

    mappings = {}
    accepted_reviews = 0
    reconciled = 0
    skipped = 0

    print("=" * 100)
    print("ARTIST CATALOG RECORDING RECONCILIATION")
    print("=" * 100)

    for review_key, review in reviews.items():

        if not isinstance(
            review,
            dict,
        ):
            continue

        decision = clean(
            review.get(
                "decision"
            )
        ).casefold()

        if decision != "accepted":
            continue

        accepted_reviews += 1

        provisional_id = clean(
            review.get(
                "recording_id"
            )
        )

        spotify_track_id = clean(
            review.get(
                "spotify_track_id"
            )
        )

        if (
            not provisional_id
            or not spotify_track_id
        ):
            print()
            print(
                "SKIP:",
                review_key,
                "missing provisional recording ID "
                "or Spotify track ID",
            )
            skipped += 1
            continue

        canonical_info = (
            spotify_to_canonical.get(
                spotify_track_id
            )
        )

        if canonical_info is None:
            print()
            print(
                "SKIP:",
                provisional_id,
                "Spotify track has no canonical "
                "recording row:",
                spotify_track_id,
            )
            skipped += 1
            continue

        catalog_idx = (
            catalog_by_provisional.get(
                provisional_id
            )
        )

        if catalog_idx is None:
            print()
            print(
                "SKIP:",
                provisional_id,
                "not found in "
                "artist_catalog_recordings_parsed.csv",
            )
            skipped += 1
            continue

        (
            canonical_idx,
            canonical_id,
        ) = canonical_info

        catalog_row = (
            catalog_recordings_df.loc[
                catalog_idx
            ]
        )

        reconciled_at = now_iso()

        # ----------------------------------------------------
        # Spotify remains baseline canonical identity.
        # WhoSampled enriches the existing canonical row.
        # ----------------------------------------------------

        field_map = {
            "whosampled_url":
                "whosampled_url",

            "whosampled_title":
                "whosampled_title",

            "whosampled_artist_names":
                "whosampled_artist_names",

            "whosampled_album":
                "whosampled_album",

            "whosampled_label":
                "whosampled_label",

            "whosampled_release_year":
                "whosampled_release_year",

            "whosampled_duration":
                "whosampled_duration",

            "whosampled_duration_iso":
                "whosampled_duration_iso",

            "whosampled_duration_ms":
                "whosampled_duration_ms",

            "whosampled_genre":
                "whosampled_genre",

            "whosampled_keywords":
                "whosampled_keywords",

            "whosampled_thumbnail_url":
                "whosampled_thumbnail_url",

            "youtube_video_id":
                "youtube_video_id",

            "youtube_url":
                "youtube_url",

            "youtube_thumbnail_url":
                "youtube_thumbnail_url",
        }

        for source_column, target_column in field_map.items():

            if source_column not in (
                catalog_recordings_df.columns
            ):
                continue

            if target_column not in (
                recordings_df.columns
            ):
                recordings_df[
                    target_column
                ] = ""

            value = clean(
                catalog_row.get(
                    source_column
                )
            )

            if value:
                recordings_df.at[
                    canonical_idx,
                    target_column
                ] = value

        if (
            "whosampled_match_status"
            not in recordings_df.columns
        ):
            recordings_df[
                "whosampled_match_status"
            ] = ""

        recordings_df.at[
            canonical_idx,
            "whosampled_match_status"
        ] = "matched"

        recordings_df.at[
            canonical_idx,
            "catalog_reconciliation_status"
        ] = "accepted_reconciled"

        recordings_df.at[
            canonical_idx,
            "catalog_review_score"
        ] = clean(
            review.get(
                "match_score"
            )
        )

        recordings_df.at[
            canonical_idx,
            "catalog_provisional_recording_id"
        ] = provisional_id

        recordings_df.at[
            canonical_idx,
            "catalog_reconciled_at"
        ] = reconciled_at

        # ----------------------------------------------------
        # Keep the provisional catalog row for provenance, but
        # explicitly point it to the canonical Spotify-backed row.
        # ----------------------------------------------------

        catalog_recordings_df.at[
            catalog_idx,
            "canonical_recording_id"
        ] = canonical_id

        catalog_recordings_df.at[
            catalog_idx,
            "catalog_reconciliation_status"
        ] = "accepted_reconciled"

        catalog_recordings_df.at[
            catalog_idx,
            "catalog_reconciled_at"
        ] = reconciled_at

        mappings[
            provisional_id
        ] = canonical_id

        reconciled += 1

        print()
        print(
            "RECONCILED:",
            clean(
                catalog_row.get(
                    "whosampled_title"
                )
            )
        )

        print(
            "  provisional:",
            provisional_id,
        )

        print(
            "  canonical:",
            canonical_id,
        )

        print(
            "  Spotify:",
            spotify_track_id,
        )

    # --------------------------------------------------------
    # Promote confident machine-matched catalog recordings.
    #
    # Promotion changes status, not identity: the catalog REC_*
    # itself becomes canonical when no canonical Recording already
    # owns the matched Spotify identity.
    # --------------------------------------------------------

    promoted = 0
    matched_to_existing = 0
    recording_columns = list(recordings_df.columns)

    whosampled_field_map = {
        "whosampled_url": "whosampled_url",
        "whosampled_title": "whosampled_title",
        "whosampled_artist_names": "whosampled_artist_names",
        "whosampled_album": "whosampled_album",
        "whosampled_label": "whosampled_label",
        "whosampled_release_year": "whosampled_release_year",
        "whosampled_duration": "whosampled_duration",
        "whosampled_duration_iso": "whosampled_duration_iso",
        "whosampled_duration_ms": "whosampled_duration_ms",
        "whosampled_genre": "whosampled_genre",
        "whosampled_keywords": "whosampled_keywords",
        "whosampled_thumbnail_url": "whosampled_thumbnail_url",
        "youtube_video_id": "youtube_video_id",
        "youtube_url": "youtube_url",
        "youtube_thumbnail_url": "youtube_thumbnail_url",
    }

    existing_recording_ids = set(
        recordings_df["recording_id"].map(clean)
    )

    for catalog_idx, catalog_row in catalog_recordings_df.iterrows():

        provisional_id = clean(
            catalog_row.get("recording_id")
        )
        existing_canonical_id = clean(
            catalog_row.get("canonical_recording_id")
        )
        spotify_match_status = clean(
            catalog_row.get("spotify_match_status")
        ).casefold()
        spotify_track_id = clean(
            catalog_row.get("spotify_track_id")
        )

        if existing_canonical_id:
            continue

        if spotify_match_status != "matched":
            continue

        if not provisional_id or not spotify_track_id:
            print()
            print(
                "SKIP AUTO-MATCH:",
                provisional_id or "<missing REC_*>",
                "missing provisional ID or Spotify ID",
            )
            skipped += 1
            continue

        reconciled_at = now_iso()
        canonical_info = spotify_to_canonical.get(
            spotify_track_id
        )

        # Another canonical Recording already owns this Spotify ID.
        if canonical_info is not None:
            _, canonical_id = canonical_info

            catalog_recordings_df.at[
                catalog_idx,
                "canonical_recording_id"
            ] = canonical_id
            catalog_recordings_df.at[
                catalog_idx,
                "catalog_reconciliation_status"
            ] = "matched_reconciled_existing"
            catalog_recordings_df.at[
                catalog_idx,
                "catalog_reconciled_at"
            ] = reconciled_at

            mappings[provisional_id] = canonical_id
            matched_to_existing += 1
            reconciled += 1

            print()
            print(
                "AUTO-RECONCILE EXISTING:",
                clean(catalog_row.get("whosampled_title")),
            )
            print("  provisional:", provisional_id)
            print("  canonical:", canonical_id)
            print("  Spotify:", spotify_track_id)
            continue

        # No canonical owner exists: promote the discovery itself.
        canonical_id = provisional_id

        if canonical_id in existing_recording_ids:
            raise SystemExit(
                f"Cannot promote {provisional_id}: its REC_* "
                "already exists in recordings.csv but does not "
                "own the expected Spotify identity."
            )

        cache_key = "spotify:" + spotify_track_id
        spotify_row = spotify_track_cache.get(cache_key)

        if not isinstance(spotify_row, dict):
            spotify_row = spotify_track_cache.get(
                spotify_track_id
            )

        if not isinstance(spotify_row, dict):
            raise SystemExit(
                f"Cannot promote {provisional_id}: no cached "
                f"Spotify metadata for {spotify_track_id}."
            )

        cached_spotify_id = clean(
            spotify_row.get("spotify_track_id")
            or spotify_row.get("id")
        )

        if (
            cached_spotify_id
            and cached_spotify_id != spotify_track_id
        ):
            raise SystemExit(
                f"Spotify cache identity mismatch for "
                f"{provisional_id}."
            )

        release_date = clean(
            spotify_row.get("album_release_date")
        )
        spotify_url = clean(
            spotify_row.get("spotify_url")
        ) or (
            "https://open.spotify.com/track/"
            + spotify_track_id
        )

        new_recording = {
            column: ""
            for column in recording_columns
        }

        new_recording.update({
            "recording_id": canonical_id,
            "title":
                clean(spotify_row.get("title"))
                or clean(catalog_row.get("whosampled_title")),
            "artist_names":
                clean(spotify_row.get("artist_names"))
                or clean(
                    catalog_row.get("whosampled_artist_names")
                ),
            "album": clean(spotify_row.get("album_name")),
            "label": clean(spotify_row.get("album_label")),
            "release_year":
                release_date[:4]
                if release_date
                else clean(
                    catalog_row.get("whosampled_release_year")
                )[:4],
            "duration": clean(spotify_row.get("duration_ms")),
            "spotify_track_id": spotify_track_id,
            "spotify_uri": clean(spotify_row.get("spotify_uri")),
            "spotify_url": spotify_url,
            "spotify_isrc": clean(spotify_row.get("isrc")),
            "spotify_album_name":
                clean(spotify_row.get("album_name")),
            "spotify_album_id":
                clean(spotify_row.get("album_id")),
            "spotify_album_release_date": release_date,
            "spotify_album_release_precision":
                clean(
                    spotify_row.get("album_release_precision")
                ),
            "spotify_album_image_url":
                clean(spotify_row.get("album_image_url")),
            "spotify_album_label":
                clean(spotify_row.get("album_label")),
            "spotify_duration_ms":
                clean(spotify_row.get("duration_ms")),
            "whosampled_match_status": "matched",
            "catalog_reconciliation_status":
                "matched_promoted",
            "catalog_review_score":
                clean(catalog_row.get("spotify_match_score")),
            "catalog_provisional_recording_id":
                provisional_id,
            "catalog_reconciled_at":
                reconciled_at,
        })

        for source_column, target_column in (
            whosampled_field_map.items()
        ):
            value = clean(
                catalog_row.get(source_column)
            )
            if value and target_column in new_recording:
                new_recording[target_column] = value

        recordings_df = pd.concat(
            [
                recordings_df,
                pd.DataFrame(
                    [new_recording],
                    columns=recording_columns,
                ),
            ],
            ignore_index=True,
        )

        canonical_idx = len(recordings_df) - 1
        existing_recording_ids.add(canonical_id)
        spotify_to_canonical[spotify_track_id] = (
            canonical_idx,
            canonical_id,
        )

        catalog_recordings_df.at[
            catalog_idx,
            "canonical_recording_id"
        ] = canonical_id
        catalog_recordings_df.at[
            catalog_idx,
            "catalog_reconciliation_status"
        ] = "matched_promoted"
        catalog_recordings_df.at[
            catalog_idx,
            "catalog_reconciled_at"
        ] = reconciled_at

        mappings[provisional_id] = canonical_id
        promoted += 1
        reconciled += 1

        print()
        print(
            "PROMOTE:",
            clean(catalog_row.get("whosampled_title")),
        )
        print(
            "  identity:",
            provisional_id,
            "->",
            canonical_id,
        )
        print(
            "  Spotify attached:",
            spotify_track_id,
        )

    # --------------------------------------------------------
    # Propagate canonical recording IDs into catalog credits.
    #
    # IMPORTANT:
    # We do NOT append these credits into credits.csv yet.
    # Artist identity reconciliation is a separate gate.
    # --------------------------------------------------------

    credit_rows_linked = 0

    for idx, row in catalog_credits_df.iterrows():

        provisional_id = clean(
            row.get(
                "recording_id"
            )
        )

        canonical_id = mappings.get(
            provisional_id
        )

        if not canonical_id:
            continue

        catalog_credits_df.at[
            idx,
            "canonical_recording_id"
        ] = canonical_id

        catalog_credits_df.at[
            idx,
            "catalog_reconciliation_status"
        ] = "recording_reconciled"

        catalog_credits_df.at[
            idx,
            "catalog_reconciled_at"
        ] = now_iso()

        credit_rows_linked += 1

    # --------------------------------------------------------
    # Propagate canonical source IDs into optional relationship
    # enrichment queue.
    # --------------------------------------------------------

    relationship_rows_linked = 0

    for idx, row in (
        relationship_queue_df.iterrows()
    ):

        provisional_id = clean(
            row.get(
                "source_recording_id"
            )
        )

        canonical_id = mappings.get(
            provisional_id
        )

        if not canonical_id:
            continue

        relationship_queue_df.at[
            idx,
            "canonical_source_recording_id"
        ] = canonical_id

        relationship_queue_df.at[
            idx,
            "catalog_reconciliation_status"
        ] = "source_recording_reconciled"

        relationship_queue_df.at[
            idx,
            "catalog_reconciled_at"
        ] = now_iso()

        relationship_rows_linked += 1

    # --------------------------------------------------------
    # Persist.
    # --------------------------------------------------------

    if not args.dry_run:
        recordings_df.to_csv(
            recordings_file,
            index=False,
            encoding="utf-8",
        )

        catalog_recordings_df.to_csv(
            catalog_recordings_file,
            index=False,
            encoding="utf-8",
        )

        catalog_credits_df.to_csv(
            catalog_credits_file,
            index=False,
            encoding="utf-8",
        )

        relationship_queue_df.to_csv(
            relationship_queue_file,
            index=False,
            encoding="utf-8",
        )

    print()
    print("=" * 100)
    print("RECONCILIATION SUMMARY")
    print("=" * 100)
    print(
        "Accepted reviews:",
        accepted_reviews,
    )
    print(
        "Recordings reconciled:",
        reconciled,
    )
    print(
        "Matched catalog recordings promoted "
        "without changing REC_* identity:",
        promoted,
    )
    print(
        "Matched catalog recordings mapped "
        "to an already-existing canonical Recording:",
        matched_to_existing,
    )
    print(
        "Skipped:",
        skipped,
    )
    print(
        "Catalog credit rows linked "
        "to canonical recordings:",
        credit_rows_linked,
    )
    print(
        "Deferred relationship rows linked "
        "to canonical source recordings:",
        relationship_rows_linked,
    )
    print()
    if args.dry_run:
        print("DRY RUN: no files were modified.")
        print("DRY RUN: no backups were created.")
    else:
        print("Backups:")
        for backup in backups:
            print(" ", backup)
    print()
    print("No network requests were made.")
    print(
        "Catalog credits were NOT appended "
        "to credits.csv."
    )
    print(
        "Artist identity reconciliation remains "
        "a separate gate."
    )


if __name__ == "__main__":
    main()
