import argparse
import json
import re
import shutil
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

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


def normalize(value):
    value = clean(value)
    if not value:
        return ""

    value = unicodedata.normalize("NFKD", value)
    value = "".join(
        c for c in value
        if not unicodedata.combining(c)
    )
    value = value.casefold()
    value = value.replace("&", " and ")
    value = value.replace("’", "'")
    value = re.sub(r"[^a-z0-9\s]", " ", value)
    return " ".join(value.split())


def normalize_url(value):
    return clean(value).rstrip("/").casefold()


def profile_slug_name(url):
    """
    Convert a WhoSampled artist profile URL such as:
        https://www.whosampled.com/Jorge-Ben/
    into a conservative display-name alias:
        Jorge Ben
    """
    url = clean(url)
    if not url:
        return ""

    try:
        parts = [
            part
            for part in urlparse(url).path.split("/")
            if part
        ]
        if not parts:
            return ""

        slug = unquote(parts[0])
        return slug.replace("-", " ").strip()
    except Exception:
        return ""


def ensure_column(df, name, default=""):
    if name not in df.columns:
        df[name] = default


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def backup_file(path):
    """
    Create a non-destructive timestamped backup immediately before
    this script writes a file.
    """
    path = Path(path)
    if not path.exists():
        return None

    timestamp = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S_%fZ"
    )

    backup = path.with_name(
        path.stem
        + ".before_artist_credit_reconciliation."
        + timestamp
        + path.suffix
    )

    counter = 1
    while backup.exists():
        backup = path.with_name(
            path.stem
            + ".before_artist_credit_reconciliation."
            + timestamp
            + f".{counter}"
            + path.suffix
        )
        counter += 1

    shutil.copy2(path, backup)
    return backup


def canonical_name_from_row(row):
    for column in (
        "canonical_name",
        "artist_name",
        "name",
    ):
        value = clean(row.get(column))
        if value:
            return value
    return ""


def add_unique(mapping, key, value):
    """
    Append value to mapping[key] unless the same canonical artist ID
    is already represented there.
    """
    if not key:
        return

    bucket = mapping.setdefault(key, [])
    artist_id = clean(
        value.get("artist_id")
        or value.get("canonical_artist", {}).get("artist_id")
    )

    for existing in bucket:
        existing_id = clean(
            existing.get("artist_id")
            or existing.get("canonical_artist", {}).get("artist_id")
        )
        if artist_id and existing_id == artist_id:
            return

    bucket.append(value)


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile artist identities on already-canonicalized "
            "artist-catalog credits."
        )
    )

    parser.add_argument(
        "--run-dir",
        default="runs/playlist_3XtRerTr3ndS88v51AAixb",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Show proposed artist/credit changes without creating "
            "backups or modifying any files."
        ),
    )

    args = parser.parse_args()
    run_dir = Path(args.run_dir)

    artists_file = run_dir / "artists.csv"
    credits_file = run_dir / "artist_catalog_credits_parsed.csv"
    reviews_file = run_dir / "artist_catalog_reviews.json"

    spotify_artist_search_cache_file = (
        run_dir
        / "related_track_pages"
        / "spotify_artist_search_cache.json"
    )

    required = (
        artists_file,
        credits_file,
        reviews_file,
    )

    for path in required:
        if not path.exists():
            raise SystemExit(f"Missing required file: {path}")

    # Spotify artist-search evidence is optional as a file, but
    # catalog-only promotion requires a matching no_results entry.
    spotify_artist_search_cache = {}
    if spotify_artist_search_cache_file.exists():
        spotify_artist_search_cache = json.loads(
            spotify_artist_search_cache_file.read_text(
                encoding="utf-8"
            )
        )
        if not isinstance(spotify_artist_search_cache, dict):
            raise SystemExit(
                "spotify_artist_search_cache.json is not a JSON object."
            )

    artists_df = pd.read_csv(
        artists_file,
        dtype=str,
    ).fillna("")

    credits_df = pd.read_csv(
        credits_file,
        dtype=str,
    ).fillna("")

    reviews = json.loads(
        reviews_file.read_text(
            encoding="utf-8"
        )
    )

    if not isinstance(reviews, dict):
        raise SystemExit(
            "artist_catalog_reviews.json is not a JSON object."
        )

    # ------------------------------------------------------------
    # Validate the staging input.
    # ------------------------------------------------------------

    required_credit_columns = (
        "recording_id",
        "artist_id",
        "artist_name",
        "canonical_recording_id",
    )

    missing_credit_columns = [
        column
        for column in required_credit_columns
        if column not in credits_df.columns
    ]

    if missing_credit_columns:
        raise SystemExit(
            "artist_catalog_credits_parsed.csv is missing required "
            "columns: "
            + ", ".join(missing_credit_columns)
        )

    if "artist_id" not in artists_df.columns:
        raise SystemExit(
            "artists.csv is missing required column: artist_id"
        )

    for column in (
        "canonical_artist_id",
        "canonical_artist_name",
        "artist_reconciliation_status",
        "artist_reconciliation_method",
        "artist_reconciliation_evidence",
    ):
        ensure_column(credits_df, column)

    # Work only on credits whose Recording identity has already
    # passed the separate recording-reconciliation gate.
    eligible_mask = (
        credits_df["canonical_recording_id"]
        .astype(str)
        .map(clean)
        .ne("")
    )

    eligible_rows = int(eligible_mask.sum())
    deferred_recording_rows = int((~eligible_mask).sum())

    # ------------------------------------------------------------
    # Validate canonical Artist IDs and build indexes.
    # ------------------------------------------------------------

    canonical_by_id = {}
    canonical_by_spotify_id = {}
    canonical_by_normalized_name = {}
    canonical_by_whosampled_url = {}

    duplicate_artist_ids = []

    for idx, row in artists_df.iterrows():
        artist_id = clean(row.get("artist_id"))
        if not artist_id:
            continue

        if artist_id in canonical_by_id:
            duplicate_artist_ids.append(artist_id)
            continue

        canonical_name = canonical_name_from_row(row)
        spotify_artist_id = clean(
            row.get("spotify_artist_id")
        )
        whosampled_url = clean(
            row.get("whosampled_url")
        )

        item = {
            "index": idx,
            "artist_id": artist_id,
            "canonical_name": canonical_name,
            "spotify_artist_id": spotify_artist_id,
            "whosampled_url": whosampled_url,
        }

        canonical_by_id[artist_id] = item

        if spotify_artist_id:
            existing = canonical_by_spotify_id.get(
                spotify_artist_id
            )
            if (
                existing
                and existing["artist_id"] != artist_id
            ):
                raise SystemExit(
                    "Duplicate canonical Spotify artist ID: "
                    f"{spotify_artist_id} belongs to both "
                    f"{existing['artist_id']} and {artist_id}."
                )
            canonical_by_spotify_id[
                spotify_artist_id
            ] = item

        name_key = normalize(canonical_name)
        if name_key:
            add_unique(
                canonical_by_normalized_name,
                name_key,
                item,
            )

        url_key = normalize_url(whosampled_url)
        if url_key:
            existing = canonical_by_whosampled_url.get(
                url_key
            )
            if (
                existing
                and existing["artist_id"] != artist_id
            ):
                raise SystemExit(
                    "Duplicate canonical WhoSampled artist URL: "
                    f"{whosampled_url} belongs to both "
                    f"{existing['artist_id']} and {artist_id}."
                )
            canonical_by_whosampled_url[url_key] = item

    if duplicate_artist_ids:
        raise SystemExit(
            "Duplicate canonical artist_id values in artists.csv: "
            + ", ".join(sorted(set(duplicate_artist_ids)))
        )

    # ------------------------------------------------------------
    # Build accepted-review alias bridges.
    #
    # An accepted review is explicit identity evidence. We use its
    # Spotify artist ID when possible, then an exact canonical name
    # only as a fallback. The accepted WhoSampled profile URL and
    # profile slug can then point catalog credits to that Artist.
    # ------------------------------------------------------------

    review_aliases = {}
    review_urls = {}

    for review_key, review in reviews.items():
        if not isinstance(review, dict):
            continue

        if clean(
            review.get("decision")
        ).casefold() != "accepted":
            continue

        spotify_artist_id = clean(
            review.get("spotify_artist_id")
        )
        spotify_artist_name = clean(
            review.get("spotify_artist_name")
        )
        whosampled_url = clean(
            review.get("whosampled_artist_url")
        )
        whosampled_name = profile_slug_name(
            whosampled_url
        )

        canonical_artist = None

        if spotify_artist_id:
            canonical_artist = (
                canonical_by_spotify_id.get(
                    spotify_artist_id
                )
            )

        if (
            canonical_artist is None
            and spotify_artist_name
        ):
            matches = canonical_by_normalized_name.get(
                normalize(spotify_artist_name),
                [],
            )
            if len(matches) == 1:
                canonical_artist = matches[0]

        if canonical_artist is None:
            continue

        bridge = {
            "canonical_artist": canonical_artist,
            "review_key": review_key,
            "spotify_artist_name": spotify_artist_name,
            "whosampled_artist_url": whosampled_url,
        }

        for alias in (
            spotify_artist_name,
            whosampled_name,
        ):
            alias_key = normalize(alias)
            if alias_key:
                add_unique(
                    review_aliases,
                    alias_key,
                    bridge,
                )

        url_key = normalize_url(whosampled_url)
        if url_key:
            add_unique(
                review_urls,
                url_key,
                bridge,
            )

    # ------------------------------------------------------------
    # Catalog-only Artist promotion helper.
    #
    # This is deliberately narrow:
    #   * Recording already canonical
    #   * provisional ART_* exists
    #   * explicit WhoSampled profile URL exists
    #   * controlled Spotify artist search says no_results
    #   * cache entry describes the same staged identity
    #
    # The provisional ART_* becomes canonical. Spotify absence is
    # evidence about Spotify, not evidence that the Artist does not
    # exist.
    # ------------------------------------------------------------

    promoted_artist_ids = set()
    promotion_details = {}

    def safe_catalog_only_promotion(row):
        provisional_artist_id = clean(
            row.get("artist_id")
        )
        artist_name = clean(
            row.get("artist_name")
        )
        artist_whosampled_url = clean(
            row.get("artist_whosampled_url")
        )

        if (
            not provisional_artist_id
            or not provisional_artist_id.startswith("ART_")
            or not artist_name
            or not artist_whosampled_url
        ):
            return None

        cache_entry = (
            spotify_artist_search_cache.get(
                provisional_artist_id
            )
        )

        if not isinstance(cache_entry, dict):
            return None

        if clean(
            cache_entry.get("status")
        ).casefold() != "no_results":
            return None

        cache_artist_name = clean(
            cache_entry.get("artist_name")
        )
        cache_whosampled_url = clean(
            cache_entry.get("whosampled_url")
        )

        if (
            cache_artist_name
            and normalize(cache_artist_name)
            != normalize(artist_name)
        ):
            return None

        if (
            cache_whosampled_url
            and normalize_url(cache_whosampled_url)
            != normalize_url(artist_whosampled_url)
        ):
            return None

        # Never create a second Artist if this exact WhoSampled URL
        # is already canonical under another ART_*.
        existing_by_url = canonical_by_whosampled_url.get(
            normalize_url(artist_whosampled_url)
        )

        if existing_by_url:
            return {
                "kind": "existing",
                "artist": existing_by_url,
                "evidence": (
                    "whosampled_url="
                    + artist_whosampled_url
                    + ";spotify_search_status=no_results"
                    + ";existing_canonical_whosampled_url=true"
                ),
            }

        # If the provisional ID is already canonical, reuse it.
        existing_by_id = canonical_by_id.get(
            provisional_artist_id
        )

        if existing_by_id:
            return {
                "kind": "existing",
                "artist": existing_by_id,
                "evidence": (
                    "whosampled_url="
                    + artist_whosampled_url
                    + ";spotify_search_status=no_results"
                    + ";existing_canonical_artist_id=true"
                ),
            }

        return {
            "kind": "promote",
            "artist": {
                "artist_id": provisional_artist_id,
                "canonical_name": artist_name,
                "spotify_artist_id": "",
                "whosampled_url": artist_whosampled_url,
            },
            "evidence": (
                "whosampled_url="
                + artist_whosampled_url
                + ";spotify_search_status=no_results"
            ),
        }

    # ------------------------------------------------------------
    # Reconcile eligible credit artists.
    # ------------------------------------------------------------

    resolved_rows = 0
    already_resolved_rows = 0
    unresolved_rows = 0
    ambiguous_rows = 0

    method_counts = {}
    proposed_promotions = {}

    print("=" * 100)
    print("ARTIST CATALOG CREDIT RECONCILIATION")
    print("=" * 100)
    print()
    print("Canonical artists before:", len(artists_df))
    print("Catalog credit rows:", len(credits_df))
    print("Eligible canonical-recording credits:", eligible_rows)
    print("Deferred because Recording is not canonical:",
          deferred_recording_rows)

    for idx, row in credits_df.iterrows():
        canonical_recording_id = clean(
            row.get("canonical_recording_id")
        )

        if not canonical_recording_id:
            continue

        artist_name = clean(
            row.get("artist_name")
        )
        provisional_artist_id = clean(
            row.get("artist_id")
        )
        artist_whosampled_url = clean(
            row.get("artist_whosampled_url")
        )

        existing_canonical_artist_id = clean(
            row.get("canonical_artist_id")
        )
        existing_status = clean(
            row.get("artist_reconciliation_status")
        ).casefold()

        # Preserve a prior valid decision, but verify that it still
        # points to a canonical Artist.
        if (
            existing_canonical_artist_id
            and existing_status == "resolved"
        ):
            if (
                existing_canonical_artist_id
                not in canonical_by_id
            ):
                raise SystemExit(
                    "Catalog credit row "
                    f"{idx} claims resolved Artist "
                    f"{existing_canonical_artist_id}, but that "
                    "Artist is absent from artists.csv."
                )

            already_resolved_rows += 1
            continue

        chosen = None
        method = ""
        evidence = ""

        # --------------------------------------------------------
        # 1. Exact accepted-review WhoSampled URL bridge.
        # --------------------------------------------------------

        if artist_whosampled_url:
            url_matches = review_urls.get(
                normalize_url(artist_whosampled_url),
                [],
            )

            unique_url_matches = {
                match["canonical_artist"]["artist_id"]: match
                for match in url_matches
            }

            if len(unique_url_matches) == 1:
                match = next(
                    iter(unique_url_matches.values())
                )
                chosen = match["canonical_artist"]
                method = (
                    "accepted_artist_review_whosampled_url"
                )
                evidence = (
                    "review_key="
                    + clean(match.get("review_key"))
                    + ";whosampled_url="
                    + artist_whosampled_url
                )

            elif len(unique_url_matches) > 1:
                credits_df.at[
                    idx,
                    "artist_reconciliation_status",
                ] = "ambiguous"
                credits_df.at[
                    idx,
                    "artist_reconciliation_method",
                ] = "accepted_review_url_conflict"
                credits_df.at[
                    idx,
                    "artist_reconciliation_evidence",
                ] = json.dumps(
                    sorted(unique_url_matches.keys()),
                    ensure_ascii=False,
                )
                ambiguous_rows += 1
                print()
                print("AMBIGUOUS:", artist_name)
                print("  reason: accepted review URL conflict")
                continue

        # --------------------------------------------------------
        # 2. Accepted-review alias bridge.
        # --------------------------------------------------------

        if chosen is None and artist_name:
            key = normalize(artist_name)
            review_matches = review_aliases.get(
                key,
                [],
            )

            unique_review_matches = {
                match["canonical_artist"]["artist_id"]: match
                for match in review_matches
            }

            if len(unique_review_matches) == 1:
                match = next(
                    iter(unique_review_matches.values())
                )
                chosen = match["canonical_artist"]
                method = "accepted_artist_review_alias"
                evidence = (
                    "review_key="
                    + clean(match.get("review_key"))
                    + ";whosampled_url="
                    + clean(
                        match.get(
                            "whosampled_artist_url"
                        )
                    )
                )

            elif len(unique_review_matches) > 1:
                credits_df.at[
                    idx,
                    "artist_reconciliation_status",
                ] = "ambiguous"
                credits_df.at[
                    idx,
                    "artist_reconciliation_method",
                ] = "accepted_review_alias_conflict"
                credits_df.at[
                    idx,
                    "artist_reconciliation_evidence",
                ] = json.dumps(
                    sorted(unique_review_matches.keys()),
                    ensure_ascii=False,
                )
                ambiguous_rows += 1
                print()
                print("AMBIGUOUS:", artist_name)
                print("  reason: accepted review alias conflict")
                continue

        # --------------------------------------------------------
        # 3. Exact canonical WhoSampled artist URL.
        # --------------------------------------------------------

        if (
            chosen is None
            and artist_whosampled_url
        ):
            chosen = canonical_by_whosampled_url.get(
                normalize_url(artist_whosampled_url)
            )

            if chosen is not None:
                method = "exact_canonical_whosampled_url"
                evidence = (
                    "whosampled_url="
                    + artist_whosampled_url
                )

        # --------------------------------------------------------
        # 4. Exact unique canonical name.
        # --------------------------------------------------------

        if chosen is None and artist_name:
            key = normalize(artist_name)
            exact_matches = (
                canonical_by_normalized_name.get(
                    key,
                    [],
                )
            )

            if len(exact_matches) == 1:
                chosen = exact_matches[0]
                method = "exact_canonical_name"
                evidence = "normalized_name=" + key

            elif len(exact_matches) > 1:
                credits_df.at[
                    idx,
                    "artist_reconciliation_status",
                ] = "ambiguous"
                credits_df.at[
                    idx,
                    "artist_reconciliation_method",
                ] = "exact_name_conflict"
                credits_df.at[
                    idx,
                    "artist_reconciliation_evidence",
                ] = json.dumps(
                    [
                        item["artist_id"]
                        for item in exact_matches
                    ],
                    ensure_ascii=False,
                )
                ambiguous_rows += 1
                print()
                print("AMBIGUOUS:", artist_name)
                print("  reason: canonical name collision")
                continue

        # --------------------------------------------------------
        # 5. Safe WhoSampled-only Artist promotion.
        # --------------------------------------------------------

        if chosen is None:
            promotion = safe_catalog_only_promotion(
                row
            )

            if promotion is not None:
                chosen = promotion["artist"]
                evidence = promotion["evidence"]

                if promotion["kind"] == "promote":
                    method = "catalog_only_canonical"

                    proposed_promotions[
                        chosen["artist_id"]
                    ] = {
                        "artist_id": chosen["artist_id"],
                        "canonical_name":
                            chosen["canonical_name"],
                        "whosampled_url":
                            chosen["whosampled_url"],
                    }

                    # Make the proposed Artist visible to subsequent
                    # rows in this same in-memory run so repeated
                    # credits resolve consistently.
                    if (
                        chosen["artist_id"]
                        not in canonical_by_id
                    ):
                        item = {
                            "index": None,
                            "artist_id":
                                chosen["artist_id"],
                            "canonical_name":
                                chosen["canonical_name"],
                            "spotify_artist_id": "",
                            "whosampled_url":
                                chosen["whosampled_url"],
                        }

                        canonical_by_id[
                            chosen["artist_id"]
                        ] = item

                        name_key = normalize(
                            chosen["canonical_name"]
                        )
                        if name_key:
                            add_unique(
                                canonical_by_normalized_name,
                                name_key,
                                item,
                            )

                        url_key = normalize_url(
                            chosen["whosampled_url"]
                        )
                        if url_key:
                            canonical_by_whosampled_url[
                                url_key
                            ] = item

                        promoted_artist_ids.add(
                            chosen["artist_id"]
                        )

                else:
                    method = (
                        "catalog_only_existing_canonical"
                    )

        # --------------------------------------------------------
        # 6. No safe local resolution.
        # --------------------------------------------------------

        if chosen is None:
            credits_df.at[
                idx,
                "canonical_artist_id",
            ] = ""
            credits_df.at[
                idx,
                "canonical_artist_name",
            ] = ""
            credits_df.at[
                idx,
                "artist_reconciliation_status",
            ] = "unresolved"
            credits_df.at[
                idx,
                "artist_reconciliation_method",
            ] = ""
            credits_df.at[
                idx,
                "artist_reconciliation_evidence",
            ] = ""

            unresolved_rows += 1

            print()
            print(
                "UNRESOLVED:",
                artist_name,
                "| role:",
                clean(row.get("role")),
            )
            continue

        # --------------------------------------------------------
        # Resolved.
        # --------------------------------------------------------

        canonical_artist_id = clean(
            chosen.get("artist_id")
        )
        canonical_artist_name = clean(
            chosen.get("canonical_name")
        )

        if not canonical_artist_id:
            raise SystemExit(
                f"Resolved row {idx} has no canonical Artist ID."
            )

        credits_df.at[
            idx,
            "canonical_artist_id",
        ] = canonical_artist_id
        credits_df.at[
            idx,
            "canonical_artist_name",
        ] = canonical_artist_name
        credits_df.at[
            idx,
            "artist_reconciliation_status",
        ] = "resolved"
        credits_df.at[
            idx,
            "artist_reconciliation_method",
        ] = method
        credits_df.at[
            idx,
            "artist_reconciliation_evidence",
        ] = evidence

        resolved_rows += 1
        method_counts[method] = (
            method_counts.get(method, 0) + 1
        )

        print()
        print(
            "RESOLVED:",
            artist_name,
            "→",
            canonical_artist_name,
        )
        print(
            "  canonical artist:",
            canonical_artist_id,
        )
        print(
            "  method:",
            method,
        )

    # ------------------------------------------------------------
    # Materialize proposed catalog-only Artists in memory.
    # ------------------------------------------------------------

    new_artist_rows = []

    for artist_id, proposal in proposed_promotions.items():
        # If it existed before this run, it is not a new row.
        original_ids = set(
            artists_df["artist_id"]
            .astype(str)
            .map(clean)
        )

        if artist_id in original_ids:
            continue

        new_artist = {
            column: ""
            for column in artists_df.columns
        }

        new_artist["artist_id"] = artist_id

        if "canonical_name" in new_artist:
            new_artist["canonical_name"] = (
                proposal["canonical_name"]
            )

        if "whosampled_name" in new_artist:
            new_artist["whosampled_name"] = (
                proposal["canonical_name"]
            )

        if "whosampled_url" in new_artist:
            new_artist["whosampled_url"] = (
                proposal["whosampled_url"]
            )

        new_artist_rows.append(new_artist)

    if new_artist_rows:
        artists_df = pd.concat(
            [
                artists_df,
                pd.DataFrame(
                    new_artist_rows,
                    columns=artists_df.columns,
                ),
            ],
            ignore_index=True,
        )

    # ------------------------------------------------------------
    # Post-reconciliation integrity checks before any write.
    # ------------------------------------------------------------

    final_artist_ids = [
        clean(value)
        for value in artists_df["artist_id"].tolist()
        if clean(value)
    ]

    if len(final_artist_ids) != len(
        set(final_artist_ids)
    ):
        raise SystemExit(
            "Integrity failure: duplicate Artist IDs would exist "
            "after reconciliation."
        )

    final_artist_id_set = set(final_artist_ids)

    resolved_mask = (
        credits_df["artist_reconciliation_status"]
        .astype(str)
        .map(clean)
        .str.casefold()
        .eq("resolved")
        & eligible_mask
    )

    missing_artist_refs = credits_df.loc[
        resolved_mask
        & ~credits_df[
            "canonical_artist_id"
        ].astype(str).map(clean).isin(
            final_artist_id_set
        )
    ]

    if len(missing_artist_refs):
        raise SystemExit(
            "Integrity failure: "
            f"{len(missing_artist_refs)} resolved catalog credits "
            "would reference Artist IDs absent from artists.csv."
        )

    # No credit whose Recording is still deferred may gain a
    # canonical Artist from this run.
    deferred_with_artist = credits_df.loc[
        (~eligible_mask)
        & credits_df[
            "canonical_artist_id"
        ].astype(str).map(clean).ne("")
    ]

    # These may pre-exist from historical state. Report rather than
    # silently delete them, because this script owns artist identity,
    # not historical cleanup.
    if len(deferred_with_artist):
        print()
        print(
            "WARNING:",
            len(deferred_with_artist),
            "recording-deferred credit rows already carry a "
            "canonical Artist ID. They were not modified.",
        )

    final_resolved_eligible = int(
        (
            eligible_mask
            & credits_df[
                "canonical_artist_id"
            ].astype(str).map(clean).ne("")
            & credits_df[
                "artist_reconciliation_status"
            ].astype(str).map(clean).str.casefold().eq(
                "resolved"
            )
        ).sum()
    )

    final_unresolved_eligible = int(
        (
            eligible_mask
            & credits_df[
                "artist_reconciliation_status"
            ].astype(str).map(clean).str.casefold().eq(
                "unresolved"
            )
        ).sum()
    )

    final_ambiguous_eligible = int(
        (
            eligible_mask
            & credits_df[
                "artist_reconciliation_status"
            ].astype(str).map(clean).str.casefold().eq(
                "ambiguous"
            )
        ).sum()
    )

    # ------------------------------------------------------------
    # Summary before persistence.
    # ------------------------------------------------------------

    print()
    print("=" * 100)
    print("ARTIST CREDIT RECONCILIATION SUMMARY")
    print("=" * 100)
    print("Eligible credit rows:", eligible_rows)
    print(
        "Recording-deferred credit rows:",
        deferred_recording_rows,
    )
    print(
        "Already-resolved eligible rows preserved:",
        already_resolved_rows,
    )
    print(
        "Rows resolved/re-resolved this run:",
        resolved_rows,
    )
    print(
        "Final resolved eligible rows:",
        final_resolved_eligible,
    )
    print(
        "Final unresolved eligible rows:",
        final_unresolved_eligible,
    )
    print(
        "Final ambiguous eligible rows:",
        final_ambiguous_eligible,
    )
    print(
        "New catalog-only Artists proposed:",
        len(new_artist_rows),
    )

    if proposed_promotions:
        print()
        print("CATALOG-ONLY ARTIST PROMOTIONS")
        print("-" * 100)
        for artist_id, proposal in sorted(
            proposed_promotions.items(),
            key=lambda item: (
                normalize(
                    item[1]["canonical_name"]
                ),
                item[0],
            ),
        ):
            print(
                proposal["canonical_name"],
                "→",
                artist_id,
            )
            print(
                "  WhoSampled:",
                proposal["whosampled_url"],
            )

    if method_counts:
        print()
        print("RESOLUTION METHODS")
        print("-" * 100)
        for method, count in sorted(
            method_counts.items()
        ):
            print(f"{method}: {count}")

    # ------------------------------------------------------------
    # Persist ONLY artists.csv and catalog-credit staging.
    # ------------------------------------------------------------

    backups = []

    if args.dry_run:
        print()
        print("DRY RUN: no files were modified.")
        print("DRY RUN: no backups were created.")
    else:
        for path in (
            artists_file,
            credits_file,
        ):
            backup = backup_file(path)
            if backup:
                backups.append(backup)

        artists_df.to_csv(
            artists_file,
            index=False,
            encoding="utf-8",
        )

        credits_df.to_csv(
            credits_file,
            index=False,
            encoding="utf-8",
        )

        print()
        print("UPDATED:")
        print(" ", artists_file)
        print(" ", credits_file)
        print()
        print("BACKUPS:")
        for backup in backups:
            print(" ", backup)

    print()
    print("No network requests were made.")
    print("recordings.csv was NOT modified.")
    print(
        "artist_catalog_recordings_parsed.csv was NOT modified."
    )
    print(
        "artist_catalog_relationship_queue.csv was NOT modified."
    )
    print("credits.csv was NOT modified.")


if __name__ == "__main__":
    main()
