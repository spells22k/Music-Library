import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urljoin, unquote

import pandas as pd
from bs4 import BeautifulSoup

from parse_whosampled_track import extract_source_metadata


BASE = "https://www.whosampled.com"


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
    value = clean(url)

    if not value:
        return ""

    return unquote(
        value
    ).rstrip("/").casefold()


def stable_id(prefix, namespace, value):
    raw = f"{namespace}:{value}"

    return (
        prefix
        + hashlib.sha1(
            raw.encode("utf-8")
        ).hexdigest()[:16]
    )


def absolute_ws_url(href):
    href = clean(href)

    if not href:
        return ""

    return urljoin(
        BASE,
        href
    )


def extract_recording_artists(soup):
    """
    Preserve WhoSampled artist URLs in addition to the names
    already returned by extract_source_metadata().
    """

    artists = []
    seen = set()

    for link in soup.select(
        ".trackArtistNames a[href]"
    ):
        name = clean(
            link.get_text(
                " ",
                strip=True,
            )
        )

        url = absolute_ws_url(
            link.get("href")
        )

        key = (
            name.casefold(),
            normalized_url(url),
        )

        if not name or key in seen:
            continue

        seen.add(key)

        artists.append({
            "artist_name": name,
            "whosampled_url": url,
        })

    return artists


def extract_credit_artists(soup):
    """
    Extract the structured WhoSampled credits while retaining
    contributor profile URLs.

    This mirrors the canonical role vocabulary already used by
    parse_whosampled_track.extract_source_metadata().
    """

    canonical_roles = {
        "producer": "produced_by",
        "producer(s)": "produced_by",
        "composer": "composed_by",
        "composer(s)": "composed_by",
        "lyricist": "written_by",
        "lyricist(s)": "written_by",
        "songwriter": "written_by",
        "songwriter(s)": "written_by",
        "arranger": "arranged_by",
        "arranger(s)": "arranged_by",
        "performer": "performed_by",
        "performer(s)": "performed_by",
        "vocalist": "performed_by",
        "instrumentalist": "performed_by",
        "engineer": "engineered_by",
        "engineer(s)": "engineered_by",
        "mixer": "mixed_by",
        "mix engineer": "mixed_by",
        "remixer": "remixed_by",
    }

    credits = []
    seen = set()

    for item in soup.select(
        ".track-credit-item"
    ):
        title = item.select_one(
            ".track-credit-title"
        )

        if not title:
            continue

        source_role = clean(
            title.get_text(
                " ",
                strip=True,
            )
        ).rstrip(":")

        role_key = source_role.casefold()

        role = canonical_roles.get(
            role_key,
            role_key.replace(
                " ",
                "_",
            ),
        )

        contributors = item.select(
            '[itemprop="contributor"] [itemprop="name"]'
        )

        for contributor in contributors:
            name = clean(
                contributor.get_text(
                    " ",
                    strip=True,
                )
            )

            link = contributor.find_parent(
                "a",
                href=True,
            )

            url = (
                absolute_ws_url(
                    link.get("href")
                )
                if link
                else ""
            )

            key = (
                name.casefold(),
                role,
                source_role.casefold(),
                normalized_url(url),
            )

            if not name or key in seen:
                continue

            seen.add(key)

            credits.append({
                "artist_name": name,
                "role": role,
                "source_role": source_role,
                "whosampled_url": url,
            })

    # Schema.org producer markup can exist separately.
    for producer in soup.select(
        '[itemprop="producer"]'
    ):
        name_node = producer.select_one(
            '[itemprop="name"]'
        )

        if not name_node:
            continue

        name = clean(
            name_node.get_text(
                " ",
                strip=True,
            )
        )

        link = producer.find(
            "a",
            href=True,
        )

        url = (
            absolute_ws_url(
                link.get("href")
            )
            if link
            else ""
        )

        key = (
            name.casefold(),
            "produced_by",
            "producer",
            normalized_url(url),
        )

        if not name or key in seen:
            continue

        seen.add(key)

        credits.append({
            "artist_name": name,
            "role": "produced_by",
            "source_role": "Producer",
            "whosampled_url": url,
        })

    return credits


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
    )

    args = parser.parse_args()

    run_dir = Path(
        args.run_dir
    )

    page_dir = (
        run_dir
        / "related_track_pages"
    )

    manifest_file = (
        page_dir
        / "manifest.json"
    )

    relationships_file = (
        run_dir
        / "relationships.csv"
    )

    enriched_file = (
        run_dir
        / "relationships_enriched.csv"
    )

    recordings_file = (
        run_dir
        / "recordings.csv"
    )

    artists_file = (
        run_dir
        / "artists.csv"
    )

    if not manifest_file.exists():
        raise SystemExit(
            f"Missing {manifest_file}"
        )

    manifest = json.loads(
        manifest_file.read_text(
            encoding="utf-8"
        )
    )

    relationships = pd.read_csv(
        relationships_file
    ).fillna("")

    if enriched_file.exists():
        enriched = pd.read_csv(
            enriched_file
        ).fillna("")
    else:
        enriched = pd.DataFrame()

    recordings = pd.read_csv(
        recordings_file
    ).fillna("")

    artists = pd.read_csv(
        artists_file
    ).fillna("")

    # --------------------------------------------------------
    # Relationship URL -> canonical target recording ID.
    #
    # Prefer an already-linked target_recording_id. If Step 5
    # has not completed in the current run, recover the exact
    # same Spotify-based REC_ identity from the persisted
    # relationships_enriched.csv match/review evidence.
    # --------------------------------------------------------

    target_by_relationship = {}

    for _, row in relationships.iterrows():
        rel_url = normalized_url(
            row.get(
                "whosampled_relationship_url"
            )
        )

        target_id = clean(
            row.get(
                "target_recording_id"
            )
        )

        if rel_url and target_id:
            target_by_relationship[
                rel_url
            ] = target_id

    if not enriched.empty:

        for _, row in enriched.iterrows():

            rel_url = normalized_url(
                row.get(
                    "whosampled_relationship_url"
                )
            )

            if not rel_url:
                continue

            # An explicit rejection always blocks promotion.
            review_decision = clean(
                row.get(
                    "spotify_review_decision"
                )
            ).casefold()

            match_status = clean(
                row.get(
                    "spotify_match_status"
                )
            ).casefold()

            if review_decision == "rejected":
                continue

            if match_status != "matched":
                continue

            spotify_track_id = clean(
                row.get(
                    "spotify_track_id"
                )
            )

            if not spotify_track_id:
                continue

            recovered_recording_id = stable_id(
                "REC_",
                "spotify",
                spotify_track_id,
            )

            target_by_relationship.setdefault(
                rel_url,
                recovered_recording_id,
            )

    # Existing WhoSampled artist mappings.
    existing_artist_by_ws_url = {}

    for _, row in artists.iterrows():
        ws_url = normalized_url(
            row.get(
                "whosampled_url"
            )
        )

        if ws_url:
            existing_artist_by_ws_url[
                ws_url
            ] = clean(
                row.get(
                    "artist_id"
                )
            )

    recording_rows = []
    credit_rows = []
    candidate_rows = []

    processed = 0

    for entry in manifest.values():
        if (
            entry.get(
                "archive_status"
            )
            != "archived"
        ):
            continue

        if (
            args.limit is not None
            and processed >= args.limit
        ):
            break

        archive_path = Path(
            entry.get(
                "archive_path",
                ""
            )
        )

        if not archive_path.exists():
            continue

        processed += 1

        soup = BeautifulSoup(
            archive_path.read_text(
                encoding="utf-8",
                errors="replace",
            ),
            "html.parser",
        )

        metadata = (
            extract_source_metadata(
                soup
            )
        )

        source_url = clean(
            metadata.get(
                "source_url"
            )
        )

        relationship_urls = (
            entry.get(
                "relationship_urls",
                []
            )
            or []
        )

        target_ids = {
            target_by_relationship[
                normalized_url(rel_url)
            ]
            for rel_url
            in relationship_urls
            if normalized_url(rel_url)
            in target_by_relationship
        }

        target_ids.discard("")

        if len(target_ids) > 1:
            raise RuntimeError(
                "One WhoSampled recording maps to multiple "
                "canonical target IDs: "
                f"{source_url} -> {sorted(target_ids)}"
            )

        if target_ids:
            recording_id = next(
                iter(target_ids)
            )

            identity_status = (
                "existing_canonical_target"
            )

        else:
            recording_id = stable_id(
                "REC_",
                "whosampled",
                normalized_url(
                    source_url
                ),
            )

            identity_status = (
                "whosampled_only"
            )

        recording_artists = (
            extract_recording_artists(
                soup
            )
        )

        structured_credits = (
            extract_credit_artists(
                soup
            )
        )

        recording_rows.append({
            "recording_id":
                recording_id,

            "identity_status":
                identity_status,

            "whosampled_url":
                source_url,

            "whosampled_title":
                clean(
                    metadata.get(
                        "source_title"
                    )
                ),

            "whosampled_artist_names":
                clean(
                    metadata.get(
                        "source_artists"
                    )
                ),

            "whosampled_album":
                clean(
                    metadata.get(
                        "source_album"
                    )
                ),

            "whosampled_label":
                clean(
                    metadata.get(
                        "source_label"
                    )
                ),

            "whosampled_release_year":
                clean(
                    metadata.get(
                        "source_release_year"
                    )
                ),

            "whosampled_duration":
                clean(
                    metadata.get(
                        "source_duration"
                    )
                ),

            "whosampled_duration_iso":
                clean(
                    metadata.get(
                        "source_duration_iso"
                    )
                ),

            "whosampled_duration_ms":
                clean(
                    metadata.get(
                        "source_duration_ms"
                    )
                ),

            "whosampled_genre":
                clean(
                    metadata.get(
                        "source_genre"
                    )
                ),

            "whosampled_keywords":
                clean(
                    metadata.get(
                        "source_keywords"
                    )
                ),

            "whosampled_thumbnail_url":
                clean(
                    metadata.get(
                        "source_thumbnail_url"
                    )
                ),

            "youtube_video_id":
                clean(
                    metadata.get(
                        "source_youtube_video_id"
                    )
                ),

            "youtube_url":
                clean(
                    metadata.get(
                        "source_youtube_url"
                    )
                ),

            "youtube_thumbnail_url":
                clean(
                    metadata.get(
                        "source_youtube_thumbnail_url"
                    )
                ),

            "related_track_archive_path":
                str(
                    archive_path
                ),
        })

        # ----------------------------------------------------
        # Recording-artist assertions from WhoSampled.
        # ----------------------------------------------------

        for order, artist in enumerate(
            recording_artists,
            start=1,
        ):
            ws_artist_url = clean(
                artist.get(
                    "whosampled_url"
                )
            )

            ws_key = normalized_url(
                ws_artist_url
            )

            provisional_artist_id = (
                existing_artist_by_ws_url.get(
                    ws_key
                )
                if ws_key
                else ""
            )

            if not provisional_artist_id:
                identity_basis = (
                    ws_key
                    or (
                        "name:"
                        + artist[
                            "artist_name"
                        ].casefold()
                    )
                )

                provisional_artist_id = (
                    stable_id(
                        "ART_",
                        "whosampled",
                        identity_basis,
                    )
                )

            credit_rows.append({
                "recording_id":
                    recording_id,

                "artist_id":
                    provisional_artist_id,

                "artist_name":
                    artist[
                        "artist_name"
                    ],

                "role":
                    "performer",

                "source_role":
                    "WhoSampled track artist",

                "artist_order":
                    order,

                "source":
                    "WhoSampled",

                "source_url":
                    source_url,

                "artist_whosampled_url":
                    ws_artist_url,
            })

            if (
                ws_key
                and ws_key
                not in existing_artist_by_ws_url
            ):
                candidate_rows.append({
                    "provisional_artist_id":
                        provisional_artist_id,

                    "artist_name":
                        artist[
                            "artist_name"
                        ],

                    "whosampled_url":
                        ws_artist_url,

                    "evidence_type":
                        "recording_artist",

                    "evidence_recording_id":
                        recording_id,

                    "evidence_recording_url":
                        source_url,
                })

        # ----------------------------------------------------
        # Producer/composer/writer/etc. assertions.
        # ----------------------------------------------------

        for credit in structured_credits:
            ws_artist_url = clean(
                credit.get(
                    "whosampled_url"
                )
            )

            ws_key = normalized_url(
                ws_artist_url
            )

            provisional_artist_id = (
                existing_artist_by_ws_url.get(
                    ws_key
                )
                if ws_key
                else ""
            )

            if not provisional_artist_id:
                identity_basis = (
                    ws_key
                    or (
                        "name:"
                        + credit[
                            "artist_name"
                        ].casefold()
                    )
                )

                provisional_artist_id = (
                    stable_id(
                        "ART_",
                        "whosampled",
                        identity_basis,
                    )
                )

            credit_rows.append({
                "recording_id":
                    recording_id,

                "artist_id":
                    provisional_artist_id,

                "artist_name":
                    credit[
                        "artist_name"
                    ],

                "role":
                    credit[
                        "role"
                    ],

                "source_role":
                    credit[
                        "source_role"
                    ],

                "artist_order":
                    "",

                "source":
                    "WhoSampled",

                "source_url":
                    source_url,

                "artist_whosampled_url":
                    ws_artist_url,
            })

            if (
                ws_key
                and ws_key
                not in existing_artist_by_ws_url
            ):
                candidate_rows.append({
                    "provisional_artist_id":
                        provisional_artist_id,

                    "artist_name":
                        credit[
                            "artist_name"
                        ],

                    "whosampled_url":
                        ws_artist_url,

                    "evidence_type":
                        credit[
                            "role"
                        ],

                    "evidence_recording_id":
                        recording_id,

                    "evidence_recording_url":
                        source_url,
                })

    recordings_out = (
        page_dir
        / "related_recordings_parsed.csv"
    )

    credits_out = (
        page_dir
        / "related_credits_parsed.csv"
    )

    candidates_out = (
        page_dir
        / "related_artist_identity_candidates.csv"
    )

    pd.DataFrame(
        recording_rows
    ).drop_duplicates().to_csv(
        recordings_out,
        index=False,
        encoding="utf-8",
    )

    pd.DataFrame(
        credit_rows
    ).drop_duplicates().to_csv(
        credits_out,
        index=False,
        encoding="utf-8",
    )

    candidate_df = pd.DataFrame(
        candidate_rows
    )

    if not candidate_df.empty:
        candidate_df = (
            candidate_df
            .drop_duplicates(
                subset=[
                    "provisional_artist_id",
                    "whosampled_url",
                    "evidence_type",
                    "evidence_recording_id",
                ]
            )
        )

    candidate_df.to_csv(
        candidates_out,
        index=False,
        encoding="utf-8",
    )

    print("=" * 88)
    print("RELATED TRACK PARSE/STAGING COMPLETE")
    print("=" * 88)
    print()
    print(
        "Archived pages parsed:",
        processed,
    )
    print(
        "Recording rows:",
        len(recording_rows),
    )
    print(
        "Credit rows:",
        len(credit_rows),
    )
    print(
        "Artist identity evidence rows:",
        len(candidate_df),
    )
    print()
    print(
        "Recordings:",
        recordings_out,
    )
    print(
        "Credits:",
        credits_out,
    )
    print(
        "Artist candidates:",
        candidates_out,
    )


if __name__ == "__main__":
    main()
