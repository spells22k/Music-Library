import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import unquote, urlparse

import pandas as pd
from bs4 import BeautifulSoup

from parse_whosampled_track import (
    extract_source_metadata,
    extract_relationships,
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


def stable_id(prefix, namespace, value):
    raw = (
        f"{namespace}:"
        f"{value}"
    )

    return (
        prefix
        + hashlib.sha1(
            raw.encode(
                "utf-8"
            )
        ).hexdigest()[:16]
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


def normalize_credit_list(value):
    """
    source_credits may be:
      - already a Python list
      - JSON encoded string
      - blank / malformed

    Always return a list of dicts.
    """

    if isinstance(
        value,
        list,
    ):
        return [
            item
            for item in value
            if isinstance(
                item,
                dict,
            )
        ]

    raw = clean(
        value
    )

    if not raw:
        return []

    try:
        parsed = json.loads(
            raw
        )
    except Exception:
        return []

    if not isinstance(
        parsed,
        list,
    ):
        return []

    return [
        item
        for item in parsed
        if isinstance(
            item,
            dict,
        )
    ]


def parse_artist_names(value):
    """
    Preserve the display string, while producing a conservative
    names list for performer-credit staging.

    The detailed WhoSampled track parser currently returns a
    comma-separated display string. This is adequate for the
    current catalog test set, but the raw display value is also
    retained on the recording row.
    """

    raw = clean(
        value
    )

    if not raw:
        return []

    return [
        part.strip()
        for part in raw.split(",")
        if part.strip()
    ]


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
            "Maximum unique archived catalog track pages "
            "to parse this run."
        ),
    )

    args = parser.parse_args()

    run_dir = Path(
        args.run_dir
    )

    catalog_file = (
        run_dir
        / "artist_catalog_tracks.csv"
    )

    archive_dir = (
        run_dir
        / "artist_catalog_track_pages"
    )

    recordings_file = (
        run_dir
        / "artist_catalog_recordings_parsed.csv"
    )

    credits_file = (
        run_dir
        / "artist_catalog_credits_parsed.csv"
    )

    relationship_queue_file = (
        run_dir
        / "artist_catalog_relationship_queue.csv"
    )

    if not catalog_file.exists():
        raise SystemExit(
            f"Missing: {catalog_file}"
        )

    catalog_df = pd.read_csv(
        catalog_file
    ).fillna("")

    # --------------------------------------------------------
    # Preserve downstream state from prior parser runs.
    #
    # This parser rebuilds WhoSampled-derived staging rows from
    # archived HTML. Downstream review/reconciliation stages may
    # subsequently add or change canonical identity fields. Those
    # decisions are authoritative and must survive later parser
    # reruns when new catalog pages are added.
    # --------------------------------------------------------

    existing_recordings_by_id = {}
    existing_credits_by_id = {}
    existing_relationships_by_key = {}

    if recordings_file.exists():
        existing_recordings_df = pd.read_csv(
            recordings_file
        ).fillna("")

        for _, existing_row in existing_recordings_df.iterrows():
            existing_id = clean(
                existing_row.get(
                    "recording_id"
                )
            )
            if existing_id:
                existing_recordings_by_id[
                    existing_id
                ] = existing_row.to_dict()

    if credits_file.exists():
        existing_credits_df = pd.read_csv(
            credits_file
        ).fillna("")

        for _, existing_row in existing_credits_df.iterrows():
            existing_id = clean(
                existing_row.get(
                    "credit_id"
                )
            )
            if existing_id:
                existing_credits_by_id[
                    existing_id
                ] = existing_row.to_dict()

    if relationship_queue_file.exists():
        existing_relationships_df = pd.read_csv(
            relationship_queue_file
        ).fillna("")

        for _, existing_row in existing_relationships_df.iterrows():
            relationship_url = normalized_url(
                existing_row.get(
                    "whosampled_relationship_url"
                )
            )
            source_url = normalized_url(
                existing_row.get(
                    "source_whosampled_url"
                )
            )

            if relationship_url:
                existing_relationships_by_key[
                    (
                        source_url,
                        relationship_url,
                    )
                ] = existing_row.to_dict()

    # --------------------------------------------------------
    # Build provenance lookup by WhoSampled track URL.
    #
    # A single recording can legitimately appear under multiple
    # catalog roles, e.g. artist + producer.
    # --------------------------------------------------------

    provenance_by_track = {}

    for _, row in catalog_df.iterrows():

        ws_url = clean(
            row.get(
                "whosampled_track_url"
            )
        )

        if not ws_url:
            continue

        key = normalized_url(
            ws_url
        )

        provenance_by_track.setdefault(
            key,
            []
        ).append({
            "catalog_artist_review_key":
                clean(
                    row.get(
                        "catalog_artist_review_key"
                    )
                ),

            "catalog_artist_name":
                clean(
                    row.get(
                        "catalog_artist_name"
                    )
                ),

            "catalog_artist_whosampled_url":
                clean(
                    row.get(
                        "catalog_artist_whosampled_url"
                    )
                ),

            "catalog_role":
                clean(
                    row.get(
                        "catalog_role"
                    )
                ),

            "catalog_listing_page":
                clean(
                    row.get(
                        "catalog_listing_page"
                    )
                ),

            "catalog_listing_url":
                clean(
                    row.get(
                        "catalog_listing_url"
                    )
                ),
        })

    # --------------------------------------------------------
    # Find archived track pages from the catalog manifest.
    # --------------------------------------------------------

    manifest_file = (
        archive_dir
        / "manifest.json"
    )

    manifest = {}

    if manifest_file.exists():
        try:
            manifest = json.loads(
                manifest_file.read_text(
                    encoding="utf-8"
                )
            )
        except Exception:
            manifest = {}

    if not isinstance(
        manifest,
        dict,
    ):
        manifest = {}

    targets = []

    seen = set()

    for key, entry in manifest.items():

        if not isinstance(
            entry,
            dict,
        ):
            continue

        if clean(
            entry.get(
                "status"
            )
        ) != "archived":
            continue

        archive_path = clean(
            entry.get(
                "archive_path"
            )
        )

        requested_url = clean(
            entry.get(
                "requested_url"
            )
        )

        final_url = clean(
            entry.get(
                "final_url"
            )
        )

        if not archive_path:
            continue

        path = Path(
            archive_path
        )

        if not path.exists():
            continue

        ws_url = (
            final_url
            or requested_url
        )

        url_key = normalized_url(
            ws_url
        )

        if not url_key:
            continue

        if url_key in seen:
            continue

        seen.add(
            url_key
        )

        targets.append({
            "url_key":
                url_key,

            "requested_url":
                requested_url,

            "final_url":
                final_url,

            "archive_path":
                path,
        })

    if args.limit is not None:
        targets = targets[
            :args.limit
        ]

    print("=" * 100)
    print(
        "ARTIST CATALOG TRACK PARSE / STAGING"
    )
    print("=" * 100)
    print()
    print(
        "Archived unique track pages:",
        len(targets),
    )

    recording_rows = []
    credit_rows = []
    relationship_rows = []

    seen_recording_ids = set()
    seen_credit_keys = set()
    seen_relationship_keys = set()

    for number, target in enumerate(
        targets,
        start=1,
    ):

        path = target[
            "archive_path"
        ]

        print()
        print("-" * 100)
        print(
            f"[{number}/{len(targets)}]",
            path.name,
        )

        html = path.read_text(
            encoding="utf-8",
            errors="replace",
        )

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        source = extract_source_metadata(
            soup
        )

        relationships = extract_relationships(
            soup,
            source,
        )

        ws_url = (
            clean(
                source.get(
                    "source_url"
                )
            )
            or target[
                "final_url"
            ]
            or target[
                "requested_url"
            ]
        )

        ws_key = normalized_url(
            ws_url
        )

        recording_id = stable_id(
            "REC_",
            "whosampled",
            ws_key,
        )

        provenance = (
            provenance_by_track.get(
                ws_key,
                []
            )
        )

        catalog_roles = sorted(
            {
                clean(
                    item.get(
                        "catalog_role"
                    )
                )
                for item in provenance
                if clean(
                    item.get(
                        "catalog_role"
                    )
                )
            }
        )

        catalog_artist_names = sorted(
            {
                clean(
                    item.get(
                        "catalog_artist_name"
                    )
                )
                for item in provenance
                if clean(
                    item.get(
                        "catalog_artist_name"
                    )
                )
            }
        )

        catalog_review_keys = sorted(
            {
                clean(
                    item.get(
                        "catalog_artist_review_key"
                    )
                )
                for item in provenance
                if clean(
                    item.get(
                        "catalog_artist_review_key"
                    )
                )
            }
        )

        if recording_id not in seen_recording_ids:

            recording_rows.append({
                "recording_id":
                    recording_id,

                "identity_status":
                    "whosampled_catalog_only",

                "whosampled_url":
                    ws_url,

                "whosampled_title":
                    clean(
                        source.get(
                            "source_title"
                        )
                    ),

                "whosampled_artist_names":
                    clean(
                        source.get(
                            "source_artists"
                        )
                    ),

                "whosampled_producers":
                    clean(
                        source.get(
                            "source_producers"
                        )
                    ),

                "whosampled_album":
                    clean(
                        source.get(
                            "source_album"
                        )
                    ),

                "whosampled_label":
                    clean(
                        source.get(
                            "source_label"
                        )
                    ),

                "whosampled_release_year":
                    clean(
                        source.get(
                            "source_release_year"
                        )
                    ),

                "whosampled_duration":
                    clean(
                        source.get(
                            "source_duration"
                        )
                    ),

                "whosampled_duration_iso":
                    clean(
                        source.get(
                            "source_duration_iso"
                        )
                    ),

                "whosampled_duration_ms":
                    clean(
                        source.get(
                            "source_duration_ms"
                        )
                    ),

                "whosampled_genre":
                    clean(
                        source.get(
                            "source_genre"
                        )
                    ),

                "whosampled_keywords":
                    clean(
                        source.get(
                            "source_keywords"
                        )
                    ),

                "whosampled_thumbnail_url":
                    clean(
                        source.get(
                            "source_thumbnail_url"
                        )
                    ),

                "youtube_video_id":
                    clean(
                        source.get(
                            "source_youtube_video_id"
                        )
                    ),

                "youtube_url":
                    clean(
                        source.get(
                            "source_youtube_url"
                        )
                    ),

                "youtube_thumbnail_url":
                    clean(
                        source.get(
                            "source_youtube_thumbnail_url"
                        )
                    ),

                "catalog_roles_json":
                    json.dumps(
                        catalog_roles,
                        ensure_ascii=False,
                    ),

                "catalog_artist_names_json":
                    json.dumps(
                        catalog_artist_names,
                        ensure_ascii=False,
                    ),

                "catalog_artist_review_keys_json":
                    json.dumps(
                        catalog_review_keys,
                        ensure_ascii=False,
                    ),

                "catalog_provenance_json":
                    json.dumps(
                        provenance,
                        ensure_ascii=False,
                    ),

                "catalog_track_archive_path":
                    str(
                        path
                    ),

                "spotify_match_status":
                    "pending",

                "spotify_review_decision":
                    "",

                "canonical_merge_status":
                    "pending",
            })

            seen_recording_ids.add(
                recording_id
            )

        # ----------------------------------------------------
        # Performer credits from source_artists.
        # ----------------------------------------------------

        performer_names = parse_artist_names(
            source.get(
                "source_artists"
            )
        )

        source_artist_profiles = {}

        raw_source_artist_profiles = clean(
            source.get(
                "source_artist_profiles"
            )
        )

        if raw_source_artist_profiles:
            try:
                parsed_source_artist_profiles = (
                    json.loads(
                        raw_source_artist_profiles
                    )
                )
            except Exception:
                parsed_source_artist_profiles = []

            if isinstance(
                parsed_source_artist_profiles,
                list,
            ):
                for profile in parsed_source_artist_profiles:
                    if not isinstance(
                        profile,
                        dict,
                    ):
                        continue

                    profile_name = clean(
                        profile.get(
                            "artist"
                        )
                    )

                    profile_url = clean(
                        profile.get(
                            "url"
                        )
                    )

                    if profile_name and profile_url:
                        source_artist_profiles[
                            profile_name.casefold()
                        ] = profile_url

        for order, artist_name in enumerate(
            performer_names,
            start=1,
        ):

            artist_id = stable_id(
                "ART_",
                "whosampled-name",
                artist_name.casefold(),
            )

            credit_key = (
                recording_id,
                artist_id,
                "performer",
                "WhoSampled",
            )

            if credit_key in seen_credit_keys:
                continue

            seen_credit_keys.add(
                credit_key
            )

            credit_rows.append({
                "credit_id":
                    stable_id(
                        "CRD_",
                        "credit",
                        "|".join(
                            credit_key
                        ),
                    ),

                "recording_id":
                    recording_id,

                "artist_id":
                    artist_id,

                "artist_name":
                    artist_name,

                "role":
                    "performer",

                "source_role":
                    "WhoSampled track artist",

                "artist_order":
                    order,

                "source":
                    "WhoSampled",

                "source_url":
                    ws_url,

                "artist_whosampled_url":
                    source_artist_profiles.get(
                        artist_name.casefold(),
                        "",
                    ),
            })

        # ----------------------------------------------------
        # Explicit structured credits from source_credits.
        # ----------------------------------------------------

        explicit_credits = normalize_credit_list(
            source.get(
                "source_credits"
            )
        )

        for item in explicit_credits:

            artist_name = clean(
                item.get(
                    "artist"
                )
            )

            role = clean(
                item.get(
                    "role"
                )
            )

            source_role = clean(
                item.get(
                    "source_role"
                )
            )

            artist_whosampled_url = clean(
                item.get(
                    "whosampled_url"
                )
            )

            if not artist_name or not role:
                continue

            artist_id = stable_id(
                "ART_",
                "whosampled-name",
                artist_name.casefold(),
            )

            credit_key = (
                recording_id,
                artist_id,
                role,
                "WhoSampled",
            )

            if credit_key in seen_credit_keys:
                continue

            seen_credit_keys.add(
                credit_key
            )

            credit_rows.append({
                "credit_id":
                    stable_id(
                        "CRD_",
                        "credit",
                        "|".join(
                            credit_key
                        ),
                    ),

                "recording_id":
                    recording_id,

                "artist_id":
                    artist_id,

                "artist_name":
                    artist_name,

                "role":
                    role,

                "source_role":
                    source_role,

                "artist_order":
                    "",

                "source":
                    "WhoSampled",

                "source_url":
                    ws_url,

                "artist_whosampled_url":
                    artist_whosampled_url,
            })

        # ----------------------------------------------------
        # Deferred relationship-enrichment queue.
        #
        # These are discovered locally from the archived track
        # page. Nothing is requested here.
        # ----------------------------------------------------

        for rel in relationships:

            relationship_url = clean(
                rel.get(
                    "whosampled_relationship_url"
                )
            )

            if not relationship_url:
                continue

            relationship_type = clean(
                rel.get(
                    "relationship_type"
                )
            )

            queue_key = (
                recording_id,
                normalized_url(
                    relationship_url
                ),
            )

            if queue_key in seen_relationship_keys:
                continue

            seen_relationship_keys.add(
                queue_key
            )

            relationship_rows.append({
                "source_recording_id":
                    recording_id,

                "source_whosampled_url":
                    ws_url,

                "relationship_type":
                    relationship_type,

                "related_track":
                    clean(
                        rel.get(
                            "related_track"
                        )
                    ),

                "related_artist":
                    clean(
                        rel.get(
                            "related_artist"
                        )
                    ),

                "whosampled_relationship_url":
                    relationship_url,

                "catalog_artist_names_json":
                    json.dumps(
                        catalog_artist_names,
                        ensure_ascii=False,
                    ),

                "catalog_roles_json":
                    json.dumps(
                        catalog_roles,
                        ensure_ascii=False,
                    ),

                "discovery_source":
                    "artist_catalog_track_page",

                "enrichment_status":
                    "not_requested",

                "relationship_detail_archive_path":
                    "",

                "relationship_detail_status":
                    "",
            })

        print(
            "TITLE:",
            clean(
                source.get(
                    "source_title"
                )
            )
        )

        print(
            "ARTISTS:",
            clean(
                source.get(
                    "source_artists"
                )
            )
        )

        print(
            "CREDITS:",
            len(
                performer_names
            )
            + len(
                explicit_credits
            )
        )

        print(
            "RELATIONSHIPS QUEUED:",
            len(
                relationships
            )
        )

    # --------------------------------------------------------
    # Reapply prior downstream state to regenerated staging rows.
    #
    # Source-derived WhoSampled metadata is refreshed from the
    # archive, while review/canonicalization state remains sticky.
    # --------------------------------------------------------

    recording_state_columns = {
        "spotify_match_status",
        "spotify_review_decision",
        "spotify_review_status",
        "spotify_match_method",
        "spotify_track_id",
        "spotify_candidate_track_id",
        "canonical_merge_status",
        "catalog_reconciliation_status",
        "canonical_recording_id",
        "catalog_reconciled_at",
        "canonical_identity_status",
        "canonical_identity_proposal_id",
        "canonical_identity_proposal_ids_json",
    }

    for row in recording_rows:
        prior = existing_recordings_by_id.get(
            clean(
                row.get(
                    "recording_id"
                )
            )
        )

        if not prior:
            continue

        # Preserve columns introduced by downstream stages.
        for column, value in prior.items():
            if column not in row and clean(value):
                row[column] = value

        # Preserve explicit downstream identity/review state even
        # when this parser supplies a default such as "pending".
        for column in recording_state_columns:
            if column in prior and clean(
                prior.get(
                    column
                )
            ):
                row[column] = prior[
                    column
                ]

    for row in credit_rows:
        prior = existing_credits_by_id.get(
            clean(
                row.get(
                    "credit_id"
                )
            )
        )

        if not prior:
            continue

        # Credit reconciliation may replace the provisional
        # WhoSampled-name artist_id with a canonical Artist ID.
        if clean(
            prior.get(
                "artist_id"
            )
        ):
            row["artist_id"] = prior[
                "artist_id"
            ]

        for column, value in prior.items():
            if column not in row and clean(value):
                row[column] = value

    for row in relationship_rows:
        key = (
            normalized_url(
                row.get(
                    "source_whosampled_url"
                )
            ),
            normalized_url(
                row.get(
                    "whosampled_relationship_url"
                )
            ),
        )

        prior = existing_relationships_by_key.get(
            key
        )

        if not prior:
            continue

        # Recording reconciliation may redirect the provisional
        # catalog source to an existing canonical recording.
        if clean(
            prior.get(
                "source_recording_id"
            )
        ):
            row[
                "source_recording_id"
            ] = prior[
                "source_recording_id"
            ]

        # Preserve all downstream enrichment columns/states that
        # are not regenerated from the archived track page.
        for column, value in prior.items():
            if column not in row and clean(value):
                row[column] = value

        for column in (
            "enrichment_status",
            "relationship_detail_archive_path",
            "relationship_detail_status",
            "target_recording_id",
            "relationship_id",
        ):
            if column in prior and clean(
                prior.get(
                    column
                )
            ):
                row[column] = prior[
                    column
                ]

    recordings_df = pd.DataFrame(
        recording_rows
    )

    credits_df = pd.DataFrame(
        credit_rows
    )

    relationships_df = pd.DataFrame(
        relationship_rows
    )

    recordings_df.to_csv(
        recordings_file,
        index=False,
        encoding="utf-8",
    )

    credits_df.to_csv(
        credits_file,
        index=False,
        encoding="utf-8",
    )

    relationships_df.to_csv(
        relationship_queue_file,
        index=False,
        encoding="utf-8",
    )

    print()
    print("=" * 100)
    print(
        "ARTIST CATALOG PARSE/STAGING COMPLETE"
    )
    print("=" * 100)
    print()
    print(
        "Recording rows:",
        len(
            recordings_df
        ),
    )
    print(
        "Credit rows:",
        len(
            credits_df
        ),
    )
    print(
        "Deferred relationship rows:",
        len(
            relationships_df
        ),
    )
    print()
    print(
        "Recordings:",
        recordings_file,
    )
    print(
        "Credits:",
        credits_file,
    )
    print(
        "Relationship queue:",
        relationship_queue_file,
    )
    print()
    print(
        "No network requests were made."
    )


if __name__ == "__main__":
    main()
