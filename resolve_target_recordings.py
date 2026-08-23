import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pandas as pd
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from parse_whosampled_track import (
    extract_source_metadata,
)
from spotify_metadata import resolve_track
from whosampled_match import (
    ARTIST_SLUG_CACHE_FILE,
    canonical_url_variants,
    load_artist_slug_cache,
    load_wikidata_cache,
    wikidata_artist_resolution,
)
from whosampled_media import (
    capture_rendered_artwork,
)


# ============================================================
# CONFIGURATION
# ============================================================

RUN_ID = "playlist_3XtRerTr3ndS88v51AAixb_blind"

RUN_DIR = Path(
    "runs"
) / RUN_ID

RELATIONSHIP_FILE = (
    RUN_DIR / "relationships.csv"
)

TARGET_RESOLUTION_FILE = (
    RUN_DIR / "target_recording_resolution.csv"
)

TARGET_RECORDINGS_FILE = (
    RUN_DIR / "target_recordings.csv"
)

TARGET_CREDITS_FILE = (
    RUN_DIR / "target_credits.csv"
)

TARGET_HTML_DIR = (
    RUN_DIR / "target_html"
)

TARGET_MEDIA_DIR = (
    RUN_DIR / "target_media"
)

TARGET_STATE_FILE = (
    RUN_DIR / "target_recording_state.json"
)

TARGET_HTML_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

TARGET_MEDIA_DIR.mkdir(
    parents=True,
    exist_ok=True,
)


# ============================================================
# HELPERS
# ============================================================

def clean(value):
    if pd.isna(value):
        return ""
    return str(value).strip()


def normalize(value):
    import unicodedata

    value = clean(value)

    if not value:
        return ""

    value = unicodedata.normalize(
        "NFC",
        value,
    ).casefold()

    value = value.replace(
        "’",
        "'",
    )

    value = value.replace(
        "‘",
        "'",
    )

    value = value.replace(
        "–",
        "-",
    )

    value = value.replace(
        "—",
        "-",
    )

    value = re.sub(
        r"[^\w\s'-]",
        " ",
        value,
        flags=re.UNICODE,
    )

    return " ".join(
        value.split()
    )


def safe_filename(text):
    text = clean(text)
    text = re.sub(
        r'[\\/:*?"<>|]+',
        "_",
        text,
    )
    text = re.sub(
        r"\s+",
        "_",
        text,
    )
    return text[:150]


def save_state(state):
    TARGET_STATE_FILE.write_text(
        json.dumps(
            state,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def load_state():
    if TARGET_STATE_FILE.exists():
        try:
            return json.loads(
                TARGET_STATE_FILE.read_text(
                    encoding="utf-8",
                )
            )
        except Exception:
            pass

    return {
        "targets_processed": [],
        "stopped_on_429": False,
    }


def recording_id_from_url(url):
    return (
        "REC_"
        + hashlib.sha1(
            url.encode(
                "utf-8"
            )
        ).hexdigest()[:16]
    )


def load_relationships():
    if not RELATIONSHIP_FILE.exists():
        raise SystemExit(
            f"Missing {RELATIONSHIP_FILE}"
        )

    df = pd.read_csv(
        RELATIONSHIP_FILE
    )

    required = [
        "relationship_id",
        "source_recording_id",
        "relationship_type",
        "related_track",
        "related_artist",
        "year",
        "whosampled_relationship_url",
    ]

    missing = [
        column
        for column in required
        if column not in df.columns
    ]

    if missing:
        raise SystemExit(
            "relationships.csv missing columns: "
            + ", ".join(missing)
        )

    return df


def target_key(row):
    return "|".join(
        [
            normalize(
                row.get(
                    "related_track",
                    "",
                )
            ),
            normalize(
                row.get(
                    "related_artist",
                    "",
                )
            ),
        ]
    )


def target_html_file(
    recording_id,
    title,
):
    return (
        TARGET_HTML_DIR
        / (
            recording_id
            + "_"
            + safe_filename(title)
            + ".html"
        )
    )


def extract_verified_source(
    page,
    expected_title,
    expected_artist,
):
    """
    Exact Phase-1 verification logic:

        title match + artist match = matched

        valid track page but mismatch = review

        obvious relationship page = rejected
    """

    html = page.content()

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    source = extract_source_metadata(
        soup
    )

    actual_title = normalize(
        source.get(
            "source_title",
            "",
        )
    )

    actual_artists = [
        normalize(value)
        for value in str(
            source.get(
                "source_artists",
                "",
            )
        ).split(",")
        if clean(value)
    ]

    expected_title_norm = normalize(
        expected_title
    )

    expected_artist_norm = normalize(
        expected_artist
    )

    title_match = (
        actual_title
        == expected_title_norm
    )

    artist_match = any(
        expected_artist_norm == artist
        or expected_artist_norm in artist
        or artist in expected_artist_norm
        for artist in actual_artists
    )

    canonical_url = clean(
        source.get(
            "source_url",
            "",
        )
    )

    if not canonical_url:
        return (
            "review",
            source,
        )

    path = canonical_url.lower()

    relationship_markers = (
        "/sample/",
        "/cover/",
        "/interpolation/",
        "/remix/",
        "/search/",
    )

    if any(
        marker in path
        for marker in relationship_markers
    ):
        return (
            "rejected",
            source,
        )

    if title_match and artist_match:
        return (
            "matched",
            source,
        )

    return (
        "review",
        source,
    )


def try_candidate_urls(
    page,
    candidate_urls,
    title,
    artist,
    source,
    state,
):
    """
    Try WhoSampled track URL variants for a target.

    Returns:

        status
        verified source metadata
        page URL

    Raises SystemExit on HTTP 429 after saving state.
    """

    for candidate_url in candidate_urls:

        print()
        print(
            "TARGET WHO SAMPLED REQUEST:",
            candidate_url,
        )

        print(
            "TARGET RESOLUTION SOURCE:",
            source,
        )

        try:

            response = page.goto(
                candidate_url,
                wait_until="domcontentloaded",
                timeout=60000,
            )

        except Exception as exc:

            print(
                "TARGET REQUEST ERROR:",
                repr(exc),
            )

            continue

        status = (
            response.status
            if response
            else None
        )

        print(
            "TARGET STATUS:",
            status,
        )

        if status == 429:

            print(
                "WHO SAMPLED RATE LIMITED "
                "DURING TARGET RESOLUTION."
            )

            state[
                "stopped_on_429"
            ] = True

            save_state(
                state
            )

            raise SystemExit(
                "Stopped safely on HTTP 429."
            )

        print(
            "Waiting 12 seconds "
            "before next WhoSampled request..."
        )

        time.sleep(
            12
        )

        if status != 200:
            continue

        try:

            verification_status, source_data = (
                extract_verified_source(
                    page,
                    title,
                    artist,
                )
            )

        except Exception as exc:

            print(
                "TARGET VERIFICATION ERROR:",
                repr(exc),
            )

            continue

        print(
            "TARGET VERIFICATION:",
            verification_status,
            source_data.get(
                "source_url",
                "",
            ),
        )

        if verification_status in {
            "matched",
            "review",
        }:

            return (
                verification_status,
                source_data,
                page.url,
            )

    return (
        "unresolved",
        None,
        "",
    )


# ============================================================
# TARGET RESOLUTION
# ============================================================

relationships = load_relationships()

# Remove duplicate target observations.
targets = {}

for _, row in relationships.iterrows():

    track = clean(
        row.get(
            "related_track",
            "",
        )
    )

    artist = clean(
        row.get(
            "related_artist",
            "",
        )
    )

    if not track or not artist:
        continue

    key = target_key(
        row
    )

    if key not in targets:

        targets[key] = {
            "target_key": key,
            "title": track,
            "artist": artist,
            "relationship_count": 0,
        }

    targets[key][
        "relationship_count"
    ] += 1


targets = list(
    targets.values()
)

print()
print("=" * 80)
print("PHASE 2 — TARGET RECORDING RESOLUTION")
print("=" * 80)

print(
    "Relationship rows:",
    len(relationships),
)

print(
    "Unique target candidates:",
    len(targets),
)


state = load_state()

processed_targets = set(
    state.get(
        "targets_processed",
        [],
    )
)

existing_resolution = {}

if TARGET_RESOLUTION_FILE.exists():

    try:

        resolution_df = pd.read_csv(
            TARGET_RESOLUTION_FILE
        )

        for _, row in resolution_df.iterrows():

            key = clean(
                row.get(
                    "target_key",
                    "",
                )
            )

            if key:
                existing_resolution[
                    key
                ] = row.to_dict()

    except Exception:
        existing_resolution = {}


artist_slug_cache = (
    load_artist_slug_cache()
)

wikidata_cache = (
    load_wikidata_cache()
)

resolution_rows = []


with sync_playwright() as playwright:

    browser = playwright.chromium.launch(
        headless=False
    )

    page = browser.new_page()

    try:

        for target in targets:

            key = target[
                "target_key"
            ]

            title = target[
                "title"
            ]

            artist = target[
                "artist"
            ]

            print()
            print("=" * 80)
            print(
                "TARGET:",
                title,
                "—",
                artist,
            )

            # ------------------------------------------------
            # Resume checkpoint.
            # ------------------------------------------------

            if key in processed_targets:

                saved = existing_resolution.get(
                    key
                )

                if saved:

                    print(
                        "TARGET CHECKPOINT HIT."
                    )

                    resolution_rows.append(
                        saved
                    )

                continue

            # ------------------------------------------------
            # Existing target resolution checkpoint.
            # ------------------------------------------------

            if key in existing_resolution:

                saved = existing_resolution[
                    key
                ]

                print(
                    "TARGET RESOLUTION CACHE HIT:",
                    saved.get(
                        "status",
                        "",
                    ),
                )

                resolution_rows.append(
                    saved
                )

                processed_targets.add(
                    key
                )

                state[
                    "targets_processed"
                ] = sorted(
                    processed_targets
                )

                save_state(
                    state
                )

                continue

            candidate = None

            # ------------------------------------------------
            # First: learned WhoSampled artist slug.
            # ------------------------------------------------

            learned_candidates = (
                artist_slug_cache.get(
                    artist,
                    []
                )
            )

            if isinstance(
                learned_candidates,
                str,
            ):
                learned_candidates = [
                    learned_candidates
                ]

            for slug in learned_candidates:

                candidate_urls = (
                    canonical_url_variants(
                        slug,
                        title,
                    )
                )

                status, source_data, final_url = (
                    try_candidate_urls(
                        page,
                        candidate_urls,
                        title,
                        artist,
                        "learned_slug",
                        state,
                    )
                )

                if status in {
                    "matched",
                    "review",
                }:

                    candidate = (
                        status,
                        source_data,
                        final_url,
                        "learned_slug",
                    )

                    break

            # ------------------------------------------------
            # Direct artist-name resolution.
            # ------------------------------------------------

            if candidate is None:

                candidate_urls = (
                    canonical_url_variants(
                        artist,
                        title,
                    )
                )

                status, source_data, final_url = (
                    try_candidate_urls(
                        page,
                        candidate_urls,
                        title,
                        artist,
                        "spotify_name",
                        state,
                    )
                )

                if status in {
                    "matched",
                    "review",
                }:

                    candidate = (
                        status,
                        source_data,
                        final_url,
                        "spotify_name",
                    )

            # ------------------------------------------------
            # Wikidata fallback.
            # ------------------------------------------------

            if candidate is None:

                try:

                    wd = (
                        wikidata_artist_resolution(
                            artist,
                            wikidata_cache,
                        )
                    )

                except Exception as exc:

                    print(
                        "WIKIDATA ERROR:",
                        repr(exc),
                    )

                    wd = None

                if wd:

                    print(
                        "WIKIDATA RESULT:",
                        wd.get(
                            "status",
                            "",
                        ),
                        wd.get(
                            "label",
                            "",
                        ),
                    )

                    wikidata_candidates = []

                    canonical_name = clean(
                        wd.get(
                            "label",
                            "",
                        )
                    )

                    if canonical_name:

                        wikidata_candidates.append(
                            (
                                canonical_name,
                                "wikidata_canonical",
                            )
                        )

                    aliases = (
                        wd.get(
                            "aliases",
                            []
                        )
                    )

                    if isinstance(
                        aliases,
                        list,
                    ):

                        for alias in aliases[:3]:

                            alias = clean(
                                alias
                            )

                            if (
                                alias
                                and normalize(alias)
                                != normalize(canonical_name)
                            ):

                                wikidata_candidates.append(
                                    (
                                        alias,
                                        "wikidata_alias",
                                    )
                                )

                    for candidate_name, candidate_source in (
                        wikidata_candidates
                    ):

                        status, source_data, final_url = (
                            try_candidate_urls(
                                page,
                                canonical_url_variants(
                                    candidate_name,
                                    title,
                                ),
                                title,
                                artist,
                                candidate_source,
                                state,
                            )
                        )

                        if status in {
                            "matched",
                            "review",
                        }:

                            candidate = (
                                status,
                                source_data,
                                final_url,
                                candidate_source,
                            )

                            break

            # ------------------------------------------------
            # Final unresolved result.
            # ------------------------------------------------

            if candidate is None:

                result = {
                    "target_key":
                        key,

                    "title":
                        title,

                    "artist":
                        artist,

                    "relationship_count":
                        target[
                            "relationship_count"
                        ],

                    "status":
                        "unresolved",

                    "match_method":
                        "",

                    "recording_id":
                        "",

                    "whosampled_url":
                        "",

                    "source_title":
                        "",

                    "source_artists":
                        "",
                }

            else:

                (
                    status,
                    source_data,
                    final_url,
                    match_method,
                ) = candidate

                canonical_url = clean(
                    source_data.get(
                        "source_url",
                        "",
                    )
                )

                if not canonical_url:
                    canonical_url = clean(
                        final_url
                    )

                recording_id = ""

                if (
                    status
                    == "matched"
                    and canonical_url
                ):

                    recording_id = (
                        recording_id_from_url(
                            canonical_url
                        )
                    )

                result = {
                    "target_key":
                        key,

                    "title":
                        title,

                    "artist":
                        artist,

                    "relationship_count":
                        target[
                            "relationship_count"
                        ],

                    "status":
                        status,

                    "match_method":
                        match_method,

                    "recording_id":
                        recording_id,

                    "whosampled_url":
                        canonical_url,

                    "source_title":
                        clean(
                            source_data.get(
                                "source_title",
                                "",
                            )
                        ),

                    "source_artists":
                        clean(
                            source_data.get(
                                "source_artists",
                                "",
                            )
                        ),
                }

                # ------------------------------------------------
                # Archive verified/review target page.
                # ------------------------------------------------

                if status in {
                    "matched",
                    "review",
                }:

                    html = page.content()

                    html_file = target_html_file(
                        recording_id
                        or (
                            "REVIEW_"
                            + hashlib.sha1(
                                key.encode(
                                    "utf-8"
                                )
                            ).hexdigest()[:16]
                        ),
                        title,
                    )

                    html_file.write_text(
                        html,
                        encoding="utf-8",
                    )

                    print(
                        "TARGET HTML SAVED:",
                        html_file,
                    )

                    # ------------------------------------------------
                    # Capture artwork from the same browser page.
                    # ------------------------------------------------

                    media = (
                        capture_rendered_artwork(
                            page,
                            title,
                            TARGET_MEDIA_DIR,
                        )
                    )

                    print(
                        "TARGET ARTWORK:",
                        media.get(
                            "whosampled_thumbnail_status",
                            "unavailable",
                        ),
                    )

                    result[
                        "whosampled_thumbnail_url"
                    ] = media.get(
                        "whosampled_thumbnail_url",
                        "",
                    )

                    result[
                        "whosampled_thumbnail_path"
                    ] = media.get(
                        "whosampled_thumbnail_path",
                        "",
                    )

                    result[
                        "whosampled_thumbnail_status"
                    ] = media.get(
                        "whosampled_thumbnail_status",
                        "unavailable",
                    )

                    result[
                        "youtube_video_id"
                    ] = source_data.get(
                        "source_youtube_video_id",
                        "",
                    )

                    result[
                        "youtube_url"
                    ] = source_data.get(
                        "source_youtube_url",
                        "",
                    )

            resolution_rows.append(
                result
            )

            existing_resolution[
                key
            ] = result

            processed_targets.add(
                key
            )

            state[
                "targets_processed"
            ] = sorted(
                processed_targets
            )

            save_state(
                state
            )

            # ------------------------------------------------
            # Checkpoint resolution immediately.
            # ------------------------------------------------

            pd.DataFrame(
                resolution_rows
            ).to_csv(
                TARGET_RESOLUTION_FILE,
                index=False,
                encoding="utf-8",
            )

    finally:

        browser.close()


# ============================================================
# BUILD TARGET RECORDINGS + CREDITS
# ============================================================

resolution_df = pd.DataFrame(
    resolution_rows
)

if resolution_df.empty:

    print(
        "No target resolutions produced."
    )

    raise SystemExit(0)


target_recording_rows = []
target_credit_rows = []

for _, row in resolution_df.iterrows():

    if clean(
        row.get(
            "status",
            "",
        )
    ) != "matched":

        continue

    url = clean(
        row.get(
            "whosampled_url",
            "",
        )
    )

    if not url:
        continue

    recording_id = clean(
        row.get(
            "recording_id",
            "",
        )
    )

    html_file_candidates = sorted(
        TARGET_HTML_DIR.glob(
            f"{recording_id}_*.html"
        )
    )

    if not html_file_candidates:
        continue

    html_file = html_file_candidates[
        0
    ]

    soup = BeautifulSoup(
        html_file.read_text(
            encoding="utf-8",
            errors="ignore",
        ),
        "html.parser",
    )

    source = extract_source_metadata(
        soup
    )

    target_recording_rows.append({

        "recording_id":
            recording_id,

        "title":
            source.get(
                "source_title",
                "",
            ),

        "artist_names":
            source.get(
                "source_artists",
                "",
            ),

        "album":
            source.get(
                "source_album",
                "",
            ),

        "label":
            source.get(
                "source_label",
                "",
            ),

        "release_year":
            source.get(
                "source_release_year",
                "",
            ),

        "duration":
            source.get(
                "source_duration",
                "",
            ),

        "genre":
            source.get(
                "source_genre",
                "",
            ),

        "keywords":
            source.get(
                "source_keywords",
                "",
            ),

        "whosampled_url":
            url,

        "whosampled_thumbnail_url":
            source.get(
                "source_thumbnail_url",
                "",
            ),

        "whosampled_thumbnail_path":
            clean(
                row.get(
                    "whosampled_thumbnail_path",
                    "",
                )
            ),

        "youtube_video_id":
            source.get(
                "source_youtube_video_id",
                "",
            ),

        "youtube_url":
            source.get(
                "source_youtube_url",
                "",
            ),

        "youtube_thumbnail_url":
            source.get(
                "source_youtube_thumbnail_url",
                "",
            ),
    })

    # --------------------------------------------------------
    # Structured target credits.
    # --------------------------------------------------------

    raw_credits = source.get(
        "source_credits",
        "[]",
    )

    try:

        parsed_credits = json.loads(
            raw_credits
            or "[]"
        )

    except Exception:

        parsed_credits = []

    for credit in parsed_credits:

        artist_name = clean(
            credit.get(
                "artist",
                "",
            )
        )

        role = clean(
            credit.get(
                "role",
                "",
            )
        )

        source_role = clean(
            credit.get(
                "source_role",
                "",
            )
        )

        if not artist_name or not role:
            continue

        credit_key = (
            f"{recording_id}|"
            f"{artist_name}|"
            f"{role}|"
            f"{source_role}"
        )

        credit_id = (
            "CRD_"
            + hashlib.sha1(
                credit_key.encode(
                    "utf-8"
                )
            ).hexdigest()[:16]
        )

        target_credit_rows.append({

            "credit_id":
                credit_id,

            "recording_id":
                recording_id,

            "artist_name":
                artist_name,

            "role":
                role,

            "source_role":
                source_role,

            "source":
                "WhoSampled",

            "source_url":
                url,
        })


pd.DataFrame(
    target_recording_rows
).to_csv(
    TARGET_RECORDINGS_FILE,
    index=False,
    encoding="utf-8",
)

pd.DataFrame(
    target_credit_rows
).to_csv(
    TARGET_CREDITS_FILE,
    index=False,
    encoding="utf-8",
)


# ============================================================
# UPDATE RELATIONSHIP TARGET IDS
# ============================================================

resolution_by_key = {
    clean(
        row.get(
            "target_key",
            "",
        )
    ): row
    for _, row in resolution_df.iterrows()
}


relationships = pd.read_csv(
    RELATIONSHIP_FILE
)

if "target_recording_id" not in relationships.columns:

    relationships[
        "target_recording_id"
    ] = ""

for idx, row in relationships.iterrows():

    key = target_key(
        row
    )

    result = resolution_by_key.get(
        key
    )

    if not result:
        continue

    if (
        clean(
            result.get(
                "status",
                "",
            )
        )
        == "matched"
    ):

        recording_id = clean(
            result.get(
                "recording_id",
                "",
            )
        )

        if recording_id:

            relationships.at[
                idx,
                "target_recording_id"
            ] = recording_id


relationships.to_csv(
    RELATIONSHIP_FILE,
    index=False,
    encoding="utf-8",
)


# ============================================================
# SPOTIFY ENRICHMENT OF TARGET RECORDINGS
# ============================================================

if TARGET_RECORDINGS_FILE.exists():

    target_recordings = pd.read_csv(
        TARGET_RECORDINGS_FILE
    )

    if not target_recordings.empty:

        spotify_rows = []

        for _, row in target_recordings.iterrows():

            result = resolve_track(
                title=clean(
                    row.get(
                        "title",
                        "",
                    )
                ),
                artists=clean(
                    row.get(
                        "artist_names",
                        "",
                    )
                ),
                year=clean(
                    row.get(
                        "release_year",
                        "",
                    )
                ),
            )

            spotify_rows.append({
                **row.to_dict(),

                "spotify_match_status":
                    result.get(
                        "match_status"
                    ),

                "spotify_match_method":
                    result.get(
                        "match_method"
                    ),

                "spotify_match_score":
                    result.get(
                        "match_score"
                    ),

                "spotify_track_id":
                    result.get(
                        "spotify_track_id"
                    ),

                "spotify_isrc":
                    result.get(
                        "isrc"
                    ),

                "spotify_url":
                    (
                        (
                            "https://open.spotify.com/track/"
                            + clean(
                                result.get(
                                    "spotify_track_id",
                                    "",
                                )
                            )
                        )
                        if clean(
                            result.get(
                                "spotify_track_id",
                                "",
                            )
                        )
                        else ""
                    ),

                "spotify_title":
                    result.get(
                        "title"
                    ),

                "spotify_artist_names":
                    result.get(
                        "artist_names"
                    ),

                "spotify_album_name":
                    result.get(
                        "album_name"
                    ),

                "spotify_album_release_date":
                    result.get(
                        "album_release_date"
                    ),

                "spotify_album_image_url":
                    result.get(
                        "album_image_url"
                    ),

                "spotify_album_label":
                    result.get(
                        "album_label"
                    ),
            })

        pd.DataFrame(
            spotify_rows
        ).to_csv(
            TARGET_RECORDINGS_FILE,
            index=False,
            encoding="utf-8",
        )


# ============================================================
# FINAL SUMMARY
# ============================================================

matched_count = int(
    (
        resolution_df[
            "status"
        ]
        .fillna("")
        .astype(str)
        .eq("matched")
    ).sum()
)

review_count = int(
    (
        resolution_df[
            "status"
        ]
        .fillna("")
        .astype(str)
        .eq("review")
    ).sum()
)

unresolved_count = int(
    (
        resolution_df[
            "status"
        ]
        .fillna("")
        .astype(str)
        .eq("unresolved")
    ).sum()
)

print()
print("=" * 80)
print("PHASE 2 TARGET RESOLUTION COMPLETE")
print("=" * 80)

print(
    "Unique targets:",
    len(resolution_df),
)

print(
    "Matched:",
    matched_count,
)

print(
    "Review:",
    review_count,
)

print(
    "Unresolved:",
    unresolved_count,
)

print()
print(
    "Resolution:",
    TARGET_RESOLUTION_FILE,
)

print(
    "Target recordings:",
    TARGET_RECORDINGS_FILE,
)

print(
    "Target credits:",
    TARGET_CREDITS_FILE,
)

print(
    "Relationships:",
    RELATIONSHIP_FILE,
)

print(
    "State:",
    TARGET_STATE_FILE,
)
