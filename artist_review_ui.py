import json
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import pandas as pd

from whosampled_match import (
    artist_slug_variants,
    normalize,
)


DECISIONS = {
    "accepted",
    "rejected",
    "unresolved",
}


def clean(value):
    if pd.isna(value):
        return ""
    return str(value or "").strip()


def now_iso():
    return datetime.now(
        timezone.utc
    ).isoformat()


def load_json(path, default):
    path = Path(path)

    if not path.exists():
        return default

    try:
        return json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )
    except Exception:
        return default


def save_json(path, data):
    path = Path(path)

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


def profile_url_from_slug(slug):
    slug = unquote(
        clean(slug)
    ).strip("/")

    if not slug:
        return ""

    return (
        "https://www.whosampled.com/"
        + quote(
            slug,
            safe="-&,.'()",
        )
        + "/"
    )


def profile_url_from_track_url(url):
    """
    Convert:

        /Artist/Track/

    to:

        /Artist/

    without assuming the displayed Spotify artist name is the
    WhoSampled canonical slug.
    """

    url = clean(url)

    if not url:
        return ""

    try:
        parsed = urlparse(url)

        parts = [
            part
            for part in parsed.path.split("/")
            if part
        ]

        if not parts:
            return ""

        # Do not derive artist profiles from relationship pages.
        if parts[0].casefold() in {
            "sample",
            "cover",
            "remix",
            "interpolation",
            "search",
        }:
            return ""

        return (
            f"{parsed.scheme or 'https'}://"
            f"{parsed.netloc or 'www.whosampled.com'}/"
            f"{parts[0]}/"
        )

    except Exception:
        return ""


def candidate_slug_from_url(url):
    try:
        parts = [
            part
            for part in urlparse(
                clean(url)
            ).path.split("/")
            if part
        ]

        if not parts:
            return ""

        return unquote(parts[0])

    except Exception:
        return ""


def spotify_artist_rows(spotify_df):
    """
    Return canonical Spotify artist identities.

    Prefer artists_json when available.

    Older/current cached Spotify exports may instead contain only
    artist_ids + artist_names. In that case reconstruct identities
    locally without making Spotify requests.
    """

    artists = {}

    for _, row in spotify_df.iterrows():

        raw = clean(
            row.get(
                "artists_json"
            )
        )

        structured = []

        if raw:

            try:
                parsed = json.loads(
                    raw
                )

                if isinstance(
                    parsed,
                    list,
                ):
                    structured = [
                        artist
                        for artist in parsed
                        if isinstance(
                            artist,
                            dict,
                        )
                    ]

            except Exception:
                structured = []

        # ----------------------------------------------------
        # Preferred structured representation.
        # ----------------------------------------------------

        if structured:

            for artist in structured:

                spotify_artist_id = clean(
                    artist.get(
                        "id"
                    )
                )

                artist_name = clean(
                    artist.get(
                        "name"
                    )
                )

                if (
                    not spotify_artist_id
                    or not artist_name
                ):
                    continue

                artists[
                    spotify_artist_id
                ] = {
                    "spotify_artist_id":
                        spotify_artist_id,

                    "spotify_artist_name":
                        artist_name,

                    "spotify_uri":
                        clean(
                            artist.get(
                                "uri"
                            )
                        )
                        or (
                            "spotify:artist:"
                            + spotify_artist_id
                        ),

                    "spotify_url":
                        clean(
                            artist.get(
                                "spotify_url"
                            )
                        )
                        or (
                            "https://open.spotify.com/artist/"
                            + spotify_artist_id
                        ),
                }

            continue

        # ----------------------------------------------------
        # Legacy/current CSV fallback:
        #
        #   artist_ids
        #   artist_names
        #
        # No network request is required.
        # ----------------------------------------------------

        artist_ids = [
            value.strip()
            for value
            in clean(
                row.get(
                    "artist_ids"
                )
            ).split(",")
            if value.strip()
        ]

        artist_names_raw = clean(
            row.get(
                "artist_names"
            )
        )

        if not artist_ids:
            continue

        # One ID means the entire display string belongs to that
        # artist, even if the artist name itself contains commas.
        if len(
            artist_ids
        ) == 1:

            if not artist_names_raw:
                continue

            spotify_artist_id = (
                artist_ids[0]
            )

            artists[
                spotify_artist_id
            ] = {
                "spotify_artist_id":
                    spotify_artist_id,

                "spotify_artist_name":
                    artist_names_raw,

                "spotify_uri":
                    (
                        "spotify:artist:"
                        + spotify_artist_id
                    ),

                "spotify_url":
                    (
                        "https://open.spotify.com/artist/"
                        + spotify_artist_id
                    ),
            }

            continue

        # Multi-artist legacy rows are only reconstructed when
        # IDs and parsed display names line up one-to-one.
        artist_names = [
            value.strip()
            for value
            in artist_names_raw.split(",")
            if value.strip()
        ]

        if (
            len(
                artist_names
            )
            != len(
                artist_ids
            )
        ):
            continue

        for (
            spotify_artist_id,
            artist_name,
        ) in zip(
            artist_ids,
            artist_names,
        ):

            artists[
                spotify_artist_id
            ] = {
                "spotify_artist_id":
                    spotify_artist_id,

                "spotify_artist_name":
                    artist_name,

                "spotify_uri":
                    (
                        "spotify:artist:"
                        + spotify_artist_id
                    ),

                "spotify_url":
                    (
                        "https://open.spotify.com/artist/"
                        + spotify_artist_id
                    ),
            }

    return artists

def track_artists_by_spotify_id(
    spotify_df,
):
    result = {}

    for _, row in spotify_df.iterrows():

        track_id = clean(
            row.get(
                "spotify_track_id"
            )
        )

        if not track_id:
            continue

        raw = clean(
            row.get("artists_json")
        )

        artists = []

        if raw:
            try:
                parsed = json.loads(raw)

                artists = [
                    {
                        "id":
                            clean(
                                artist.get("id")
                            ),

                        "name":
                            clean(
                                artist.get("name")
                            ),
                    }
                    for artist in parsed
                    if isinstance(
                        artist,
                        dict,
                    )
                    and clean(
                        artist.get("id")
                    )
                    and clean(
                        artist.get("name")
                    )
                ]

            except Exception:
                artists = []

        # ----------------------------------------------------
        # Legacy/current Spotify export fallback.
        #
        # Older cached spotify_tracks.csv files may not contain
        # artists_json. Reconstruct artist identities from the
        # parallel artist_ids + artist_names fields instead.
        # ----------------------------------------------------

        if not artists:

            artist_ids = [
                value.strip()
                for value
                in clean(
                    row.get(
                        "artist_ids"
                    )
                ).split(",")
                if value.strip()
            ]

            artist_names_raw = clean(
                row.get(
                    "artist_names"
                )
            )

            # One artist ID means the entire display string is
            # that artist's name, even if the name itself
            # contains commas.
            if len(artist_ids) == 1:

                if artist_names_raw:
                    artists = [{
                        "id":
                            artist_ids[0],

                        "name":
                            artist_names_raw,
                    }]

            else:

                artist_names = [
                    value.strip()
                    for value
                    in artist_names_raw.split(",")
                    if value.strip()
                ]

                if (
                    artist_ids
                    and len(
                        artist_ids
                    )
                    == len(
                        artist_names
                    )
                ):

                    artists = [
                        {
                            "id":
                                artist_id,

                            "name":
                                artist_name,
                        }
                        for (
                            artist_id,
                            artist_name,
                        )
                        in zip(
                            artist_ids,
                            artist_names,
                        )
                    ]

        result[track_id] = artists

    return result


def add_candidate(
    candidates,
    url,
    source,
    evidence="",
):
    url = clean(url)

    if not url:
        return

    key = (
        unquote(url)
        .rstrip("/")
        .casefold()
    )

    for item in candidates:

        item_key = (
            unquote(
                clean(item["url"])
            )
            .rstrip("/")
            .casefold()
        )

        if item_key == key:

            if (
                source
                not in item["sources"]
            ):
                item["sources"].append(
                    source
                )

            if (
                evidence
                and evidence
                not in item["evidence"]
            ):
                item["evidence"].append(
                    evidence
                )

            return

    candidates.append({
        "url":
            url,

        "slug":
            candidate_slug_from_url(
                url
            ),

        "sources":
            [source],

        "evidence":
            (
                [evidence]
                if evidence
                else []
            ),
    })


def build_artist_review_candidates(
    match_file,
    spotify_file,
    artist_slug_cache,
    related_artist_candidates_file=None,
):
    """
    Build one review record per Spotify artist involved in at least
    one unresolved/not-found playlist track.

    Candidate order:
        1. learned WhoSampled artist slug
        2. profile derived from a verified matched track
        3. generated Unicode/ASCII artist slug candidates

    Generated candidates are intentionally only suggestions.
    Contributor acceptance is required before catalog crawling.
    """

    match_df = pd.read_csv(
        match_file
    )

    spotify_df = pd.read_csv(
        spotify_file
    )

    spotify_artists = (
        spotify_artist_rows(
            spotify_df
        )
    )

    track_artists = (
        track_artists_by_spotify_id(
            spotify_df
        )
    )

    status_column = (
        "match_status"
        if "match_status"
        in match_df.columns
        else "whosampled_match_status"
    )

    unresolved_statuses = {
        "unresolved",
        "not_found",
        "artist_profile_only",
    }

    unresolved_by_artist = {}

    matched_tracks_by_artist = {}

    # Actual WhoSampled artist-profile landings observed during
    # Phase 1. These are stronger evidence than a URL merely
    # generated from the Spotify artist name.
    observed_profiles_by_artist = {}

    # --------------------------------------------------------
    # Associate tracks with their structured Spotify artists.
    # --------------------------------------------------------

    for _, row in match_df.iterrows():

        track_id = clean(
            row.get(
                "spotify_track_id"
            )
        )

        title = clean(
            row.get(
                "spotify_title"
            )
        )

        status = clean(
            row.get(
                status_column
            )
        ).casefold()

        ws_url = clean(
            row.get(
                "whosampled_url"
            )
        )

        row_artists = track_artists.get(
            track_id,
            [],
        )

        for artist in row_artists:

            artist_id = artist["id"]

            if (
                status
                in unresolved_statuses
            ):

                unresolved_by_artist.setdefault(
                    artist_id,
                    [],
                ).append({
                    "spotify_track_id":
                        track_id,

                    "title":
                        title,

                    "match_status":
                        status,

                    "whosampled_url":
                        ws_url,
                })

                raw_profile_evidence = clean(
                    row.get(
                        "artist_profile_evidence_json"
                    )
                )

                if raw_profile_evidence:

                    try:
                        profile_evidence = (
                            json.loads(
                                raw_profile_evidence
                            )
                        )
                    except Exception:
                        profile_evidence = []

                    for evidence in profile_evidence:

                        if not isinstance(
                            evidence,
                            dict,
                        ):
                            continue

                        evidence_artist = clean(
                            evidence.get(
                                "spotify_artist_name"
                            )
                        )

                        # A multi-artist track may encounter a
                        # different profile for each Spotify artist.
                        # Never assign one artist's observation to
                        # another merely because they share a track.
                        if (
                            normalize(
                                evidence_artist
                            )
                            != normalize(
                                artist["name"]
                            )
                        ):
                            continue

                        observed_profiles_by_artist.setdefault(
                            artist_id,
                            [],
                        ).append(
                            evidence
                        )

            elif (
                status == "matched"
                and ws_url
            ):

                matched_tracks_by_artist.setdefault(
                    artist_id,
                    [],
                ).append({
                    "title":
                        title,

                    "whosampled_url":
                        ws_url,
                })

    review_rows = []

    for (
        spotify_artist_id,
        unresolved_tracks,
    ) in unresolved_by_artist.items():

        artist = spotify_artists.get(
            spotify_artist_id
        )

        if not artist:
            continue

        artist_name = (
            artist[
                "spotify_artist_name"
            ]
        )

        candidates = []

        # ----------------------------------------------------
        # 1. Previously learned WhoSampled artist slug.
        # ----------------------------------------------------

        learned_slugs = (
            artist_slug_cache.get(
                normalize(
                    artist_name
                ),
                [],
            )
        )

        if isinstance(
            learned_slugs,
            str,
        ):
            learned_slugs = [
                learned_slugs
            ]

        for slug in learned_slugs:

            add_candidate(
                candidates,
                profile_url_from_slug(
                    slug
                ),
                "learned_slug",
                (
                    "Previously learned from "
                    "WhoSampled resolution"
                ),
            )

        # ----------------------------------------------------
        # 2. Profile inferred from already verified track pages.
        #
        # This is supporting evidence, not automatic approval.
        # ----------------------------------------------------

        verified_tracks = (
            matched_tracks_by_artist.get(
                spotify_artist_id,
                [],
            )
        )

        # Only tracks whose WhoSampled URL owner can actually be
        # attributed to this Spotify artist should be displayed as
        # verified artist-identity evidence.
        attributable_verified_tracks = []

        for matched_track in verified_tracks:

            profile_url = (
                profile_url_from_track_url(
                    matched_track[
                        "whosampled_url"
                    ]
                )
            )

            if not profile_url:
                continue

            profile_slug = (
                candidate_slug_from_url(
                    profile_url
                )
            )

            profile_slug_norm = (
                normalize(
                    profile_slug.replace(
                        "-",
                        " "
                    )
                )
            )

            acceptable_slug_norms = {
                normalize(
                    slug.replace(
                        "-",
                        " "
                    )
                )
                for slug in (
                    list(
                        learned_slugs
                    )
                    + artist_slug_variants(
                        artist_name
                    )
                )
                if clean(slug)
            }

            if (
                profile_slug_norm
                not in acceptable_slug_norms
            ):

                print(
                    "IGNORING CROSS-ARTIST "
                    "TRACK-OWNER PROFILE:",
                    artist_name,
                    "!=",
                    profile_url,
                )

                continue

            add_candidate(
                candidates,
                profile_url,
                "verified_track_match",
                (
                    "Derived from matched track: "
                    + matched_track[
                        "title"
                    ]
                ),
            )

            attributable_verified_tracks.append(
                matched_track
            )

        # ----------------------------------------------------
        # 3. Actual artist-profile landings observed during
        #    Phase 1.
        #
        # This proves the WhoSampled profile exists, but still does
        # NOT authorize catalog crawling until contributor review.
        # ----------------------------------------------------

        for evidence in (
            observed_profiles_by_artist.get(
                spotify_artist_id,
                [],
            )
        ):

            profile_url = clean(
                evidence.get(
                    "whosampled_artist_profile_url"
                )
            )

            if not profile_url:
                continue

            add_candidate(
                candidates,
                profile_url,
                "observed_artist_profile",
                (
                    "Live Phase-1 artist-profile landing"
                    + (
                        " via "
                        + clean(
                            evidence.get(
                                "source"
                            )
                        )
                        if clean(
                            evidence.get(
                                "source"
                            )
                        )
                        else ""
                    )
                ),
            )

        # ----------------------------------------------------
        # 4. Purely generated candidate URLs.
        #
        # These exist so an unresolved artist can still be
        # reviewed manually before any expensive crawl.
        # ----------------------------------------------------

        for slug in artist_slug_variants(
            artist_name
        ):

            add_candidate(
                candidates,
                profile_url_from_slug(
                    slug
                ),
                "generated_artist_slug",
                (
                    "Generated from Spotify "
                    "artist name; not verified"
                ),
            )

        review_rows.append({
            **artist,


            "review_key":
                (
                    "spotify:"
                    + clean(
                        artist.get(
                            "spotify_artist_id"
                        )
                    )
                ),

            "identity_namespace":
                "spotify",

            "related_artist_evidence":
                [],

"unresolved_tracks":
                unresolved_tracks,

            "verified_tracks":
                attributable_verified_tracks,

            "candidates":
                candidates,
        })
    # --------------------------------------------------------
    # Related-track artist profiles.
    #
    # These profile URLs were explicitly present on verified
    # archived WhoSampled track pages. They remain subject to
    # contributor review before catalog discovery.
    # --------------------------------------------------------

    if related_artist_candidates_file:

        related_artist_candidates_file = Path(
            related_artist_candidates_file
        )

        if related_artist_candidates_file.exists():

            related_df = pd.read_csv(
                related_artist_candidates_file
            ).fillna("")

            grouped = {}

            # ------------------------------------------------
            # LOCAL SPOTIFY ARTIST COMPARISON POOL
            #
            # Source 1:
            #   artists already present in spotify_tracks.csv
            #
            # Source 2:
            #   structured Spotify artists preserved on
            #   accepted/matched related-track resolutions in
            #   relationships_enriched.csv
            #
            # No Spotify requests are made here.
            # ------------------------------------------------

            spotify_by_normalized_name = {}

            def add_local_spotify_artist(artist):

                if not isinstance(
                    artist,
                    dict,
                ):
                    return

                spotify_artist_id = clean(
                    artist.get(
                        "spotify_artist_id"
                    )
                    or artist.get(
                        "id"
                    )
                )

                spotify_artist_name = clean(
                    artist.get(
                        "spotify_artist_name"
                    )
                    or artist.get(
                        "name"
                    )
                )

                if (
                    not spotify_artist_id
                    or not spotify_artist_name
                ):
                    return

                identity = {
                    "spotify_artist_id":
                        spotify_artist_id,

                    "spotify_artist_name":
                        spotify_artist_name,

                    "spotify_uri":
                        clean(
                            artist.get(
                                "spotify_uri"
                            )
                            or artist.get(
                                "uri"
                            )
                        ),

                    "spotify_url":
                        clean(
                            artist.get(
                                "spotify_url"
                            )
                        ),
                }

                key = normalize(
                    spotify_artist_name
                )

                existing = (
                    spotify_by_normalized_name
                    .setdefault(
                        key,
                        [],
                    )
                )

                if not any(
                    clean(
                        item.get(
                            "spotify_artist_id"
                        )
                    )
                    == spotify_artist_id
                    for item in existing
                ):
                    existing.append(
                        identity
                    )


            # Existing playlist Spotify artists.
            for spotify_artist in (
                spotify_artists.values()
            ):
                add_local_spotify_artist(
                    spotify_artist
                )


            # Additional artists recovered from accepted/matched
            # related-track Spotify resolution evidence.
            enriched_file = (
                related_artist_candidates_file
                .parent
                .parent
                / "relationships_enriched.csv"
            )

            if enriched_file.exists():

                enriched_df = pd.read_csv(
                    enriched_file
                ).fillna("")

                for (
                    _,
                    enriched_row,
                ) in enriched_df.iterrows():

                    match_status = clean(
                        enriched_row.get(
                            "spotify_match_status"
                        )
                    ).casefold()

                    review_decision = clean(
                        enriched_row.get(
                            "spotify_review_decision"
                        )
                    ).casefold()

                    if (
                        match_status
                        != "matched"
                    ):
                        continue

                    if (
                        review_decision
                        == "rejected"
                    ):
                        continue

                    raw_artists = clean(
                        enriched_row.get(
                            "spotify_artists_json"
                        )
                    )

                    if not raw_artists:
                        continue

                    try:
                        parsed_artists = (
                            json.loads(
                                raw_artists
                            )
                        )
                    except Exception:
                        parsed_artists = []

                    if not isinstance(
                        parsed_artists,
                        list,
                    ):
                        continue

                    for artist in parsed_artists:
                        add_local_spotify_artist(
                            artist
                        )


            for _, evidence_row in related_df.iterrows():

                provisional_artist_id = clean(
                    evidence_row.get(
                        "provisional_artist_id"
                    )
                )

                artist_name = clean(
                    evidence_row.get(
                        "artist_name"
                    )
                )

                ws_url = clean(
                    evidence_row.get(
                        "whosampled_url"
                    )
                )

                if (
                    not provisional_artist_id
                    or not artist_name
                    or not ws_url
                ):
                    continue

                group_key = (
                    provisional_artist_id,
                    ws_url.rstrip("/").casefold(),
                )

                grouped_row = grouped.setdefault(
                    group_key,
                    {
                        "provisional_artist_id":
                            provisional_artist_id,

                        "artist_name":
                            artist_name,

                        "whosampled_url":
                            ws_url,

                        "evidence":
                            [],
                    },
                )

                evidence = {
                    "evidence_type":
                        clean(
                            evidence_row.get(
                                "evidence_type"
                            )
                        ),

                    "evidence_recording_id":
                        clean(
                            evidence_row.get(
                                "evidence_recording_id"
                            )
                        ),

                    "evidence_recording_url":
                        clean(
                            evidence_row.get(
                                "evidence_recording_url"
                            )
                        ),
                }

                if (
                    evidence
                    not in grouped_row["evidence"]
                ):
                    grouped_row[
                        "evidence"
                    ].append(
                        evidence
                    )

            for grouped_row in grouped.values():

                candidates = []

                local_spotify_matches = (
                    spotify_by_normalized_name.get(
                        normalize(
                            grouped_row[
                                "artist_name"
                            ]
                        ),
                        [],
                    )
                )

                # Exact normalized-name equality proposes a
                # comparison candidate only. It does NOT merge
                # identities automatically.
                if len(
                    local_spotify_matches
                ) == 1:

                    spotify_candidate = (
                        local_spotify_matches[
                            0
                        ]
                    )

                    spotify_candidate_status = (
                        "local_exact_name"
                    )

                elif len(
                    local_spotify_matches
                ) > 1:

                    spotify_candidate = {}

                    spotify_candidate_status = (
                        "local_name_ambiguous"
                    )

                else:

                    spotify_candidate = {}

                    spotify_candidate_status = (
                        "spotify_search_deferred"
                    )

                add_candidate(
                    candidates,
                    grouped_row[
                        "whosampled_url"
                    ],
                    "related_track_profile",
                    (
                        "Explicit WhoSampled artist profile "
                        "linked from archived related-track page"
                    ),
                )

                review_rows.append({

                    # No Spotify identity is asserted here.
                    "spotify_artist_id":
                        "",

                    # Existing browser UI already uses this
                    # field as the display name.
                    "spotify_artist_name":
                        grouped_row[
                            "artist_name"
                        ],

                    "spotify_uri":
                        "",

                    "spotify_url":
                        "",

                    # Proposed cross-source comparison.
                    # This remains unmerged until contributor
                    # acceptance in the review UI.
                    "spotify_candidate_artist_id":
                        clean(
                            spotify_candidate.get(
                                "spotify_artist_id"
                            )
                        ),

                    "spotify_candidate_artist_name":
                        clean(
                            spotify_candidate.get(
                                "spotify_artist_name"
                            )
                        ),

                    "spotify_candidate_uri":
                        clean(
                            spotify_candidate.get(
                                "spotify_uri"
                            )
                        ),

                    "spotify_candidate_url":
                        clean(
                            spotify_candidate.get(
                                "spotify_url"
                            )
                        ),

                    "spotify_candidate_status":
                        spotify_candidate_status,

                    "spotify_candidate_match_basis":
                        (
                            "normalized_exact_name"
                            if spotify_candidate
                            else ""
                        ),

                    "review_key":
                        (
                            "whosampled:"
                            + grouped_row[
                                "provisional_artist_id"
                            ]
                        ),

                    "identity_namespace":
                        "whosampled",

                    "provisional_artist_id":
                        grouped_row[
                            "provisional_artist_id"
                        ],

                    "unresolved_tracks":
                        [],

                    "verified_tracks":
                        [],

                    "related_artist_evidence":
                        grouped_row[
                            "evidence"
                        ],

                    "candidates":
                        candidates,
                })



    review_rows.sort(
        key=lambda item: (
            clean(
                item.get(
                    "spotify_artist_name"
                )
            ).casefold()
        )
    )

    return review_rows


def decision_counts(reviews):
    counts = {
        "accepted": 0,
        "rejected": 0,
        "unresolved": 0,
    }

    for value in reviews.values():

        decision = clean(
            value.get("decision")
        )

        if decision in counts:
            counts[decision] += 1

    return counts


def run_artist_catalog_review(
    match_file,
    spotify_file,
    review_file,
    artist_slug_cache,
):
    """
    Contributor gate for expensive future artist-catalog crawling.

    Commands:

        a N   accept candidate N
        o N   open candidate N in browser
        r     reject proposed WhoSampled identity
        u     leave unresolved
        q     save and quit

    Existing decisions are reused and are not repeatedly prompted.
    """

    review_file = Path(
        review_file
    )

    stored = load_json(
        review_file,
        {},
    )

    candidates = (
        build_artist_review_candidates(
            match_file,
            spotify_file,
            artist_slug_cache,
        )
    )

    pending = [
        item
        for item in candidates
        if (
            item[
                "spotify_artist_id"
            ]
            not in stored
            or clean(
                stored[
                    item[
                        "spotify_artist_id"
                    ]
                ].get(
                    "decision"
                )
            )
            not in DECISIONS
        )
    ]

    print()
    print("=" * 80)
    print("ARTIST CATALOG REVIEW GATE")
    print("=" * 80)

    print(
        "Artists requiring catalog identity:",
        len(candidates),
    )

    print(
        "Previously reviewed:",
        len(candidates) - len(pending),
    )

    print(
        "Pending review:",
        len(pending),
    )

    if not pending:

        counts = decision_counts(
            stored
        )

        print()
        print(
            "Accepted:",
            counts["accepted"],
            "| Rejected:",
            counts["rejected"],
            "| Unresolved:",
            counts["unresolved"],
        )

        return stored

    for position, item in enumerate(
        pending,
        start=1,
    ):

        artist_id = (
            item[
                "spotify_artist_id"
            ]
        )

        artist_name = (
            item[
                "spotify_artist_name"
            ]
        )

        print()
        print("=" * 80)

        print(
            f"ARTIST {position}/{len(pending)}"
        )

        print("=" * 80)

        print(
            "SPOTIFY ARTIST:",
            artist_name,
        )

        print(
            "SPOTIFY ARTIST ID:",
            artist_id,
        )

        print()
        print(
            "UNRESOLVED TRACKS:"
        )

        for track in (
            item[
                "unresolved_tracks"
            ]
        ):

            print(
                "  -",
                track["title"],
                "[",
                track["match_status"],
                "]",
            )

        if item["verified_tracks"]:

            print()
            print(
                "ALREADY VERIFIED TRACK EVIDENCE:"
            )

            for track in (
                item[
                    "verified_tracks"
                ]
            ):

                print(
                    "  -",
                    track["title"],
                    "→",
                    track[
                        "whosampled_url"
                    ],
                )

        print()
        print(
            "WHOSAMPLED ARTIST CANDIDATES:"
        )

        for number, candidate in enumerate(
            item["candidates"],
            start=1,
        ):

            print()

            print(
                f"  {number}.",
                candidate["url"],
            )

            print(
                "     slug:",
                candidate["slug"],
            )

            print(
                "     sources:",
                ", ".join(
                    candidate[
                        "sources"
                    ]
                ),
            )

            for evidence in (
                candidate[
                    "evidence"
                ]
            ):
                print(
                    "     evidence:",
                    evidence,
                )

        while True:

            print()

            raw = input(
                "Decision "
                "[a N=accept, o N=open, "
                "r=reject, u=unresolved, q=quit]: "
            ).strip()

            if not raw:
                continue

            parts = raw.split()

            command = (
                parts[0]
                .casefold()
            )

            if command == "q":

                save_json(
                    review_file,
                    stored,
                )

                print(
                    "Artist review saved."
                )

                return stored

            if command == "o":

                if len(parts) != 2:
                    print(
                        "Use: o N"
                    )
                    continue

                try:
                    number = int(
                        parts[1]
                    )
                except ValueError:
                    print(
                        "Candidate number required."
                    )
                    continue

                if not (
                    1
                    <= number
                    <= len(
                        item[
                            "candidates"
                        ]
                    )
                ):
                    print(
                        "Invalid candidate number."
                    )
                    continue

                url = (
                    item[
                        "candidates"
                    ][
                        number - 1
                    ][
                        "url"
                    ]
                )

                print(
                    "Opening:",
                    url,
                )

                webbrowser.open(
                    url
                )

                continue

            if command == "a":

                if len(parts) != 2:
                    print(
                        "Use: a N"
                    )
                    continue

                try:
                    number = int(
                        parts[1]
                    )
                except ValueError:
                    print(
                        "Candidate number required."
                    )
                    continue

                if not (
                    1
                    <= number
                    <= len(
                        item[
                            "candidates"
                        ]
                    )
                ):
                    print(
                        "Invalid candidate number."
                    )
                    continue

                selected = (
                    item[
                        "candidates"
                    ][
                        number - 1
                    ]
                )

                stored[
                    artist_id
                ] = {
                    "spotify_artist_id":
                        artist_id,

                    "spotify_artist_name":
                        artist_name,

                    "whosampled_artist_url":
                        selected["url"],

                    "whosampled_artist_slug":
                        selected["slug"],

                    "candidate_sources":
                        selected["sources"],

                    "decision":
                        "accepted",

                    "review_note":
                        "",

                    "reviewed_at":
                        now_iso(),
                }

                save_json(
                    review_file,
                    stored,
                )

                break

            if command in {
                "r",
                "u",
            }:

                decision = (
                    "rejected"
                    if command == "r"
                    else "unresolved"
                )

                stored[
                    artist_id
                ] = {
                    "spotify_artist_id":
                        artist_id,

                    "spotify_artist_name":
                        artist_name,

                    "whosampled_artist_url":
                        "",

                    "whosampled_artist_slug":
                        "",

                    "candidate_sources":
                        [],

                    "decision":
                        decision,

                    "review_note":
                        "",

                    "reviewed_at":
                        now_iso(),
                }

                save_json(
                    review_file,
                    stored,
                )

                break

            print(
                "Unknown command."
            )

    counts = decision_counts(
        stored
    )

    print()
    print("=" * 80)
    print("ARTIST CATALOG REVIEW COMPLETE")
    print("=" * 80)

    print(
        "Accepted:",
        counts["accepted"],
    )

    print(
        "Rejected:",
        counts["rejected"],
    )

    print(
        "Unresolved:",
        counts["unresolved"],
    )

    print(
        "Review file:",
        review_file,
    )

    return stored
