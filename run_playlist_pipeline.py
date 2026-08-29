import json
import random
import re
import subprocess
import sys
import hashlib
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import pandas as pd
from playwright.sync_api import sync_playwright

from whosampled_match import (
    canonical_url,
    canonical_url_variants,
    load_artist_slug_cache,
    load_wikidata_cache,
    wikidata_artist_resolution,
    ARTIST_SLUG_CACHE_FILE,
)
from parse_whosampled_track import extract_source_metadata, extract_relationships
from spotify_metadata import resolve_track
from whosampled_review_ui import run_whosampled_review
from spotify_review_ui import run_spotify_candidate_review
from whosampled_media import capture_rendered_artwork


PLAYLIST_URL = sys.argv[1]

BLIND_PHASE1_CACHE = (
    "--blind-phase1-cache"
    in sys.argv[2:]
)

STOP_AFTER_STEP4 = (
    "--stop-after-step4"
    in sys.argv[2:]
)

if BLIND_PHASE1_CACHE:
    print(
        "PHASE 1 CACHE BLINDING ENABLED:"
    )
    print(
        "  Structured track cache: IGNORED"
    )
    print(
        "  Learned artist slug cache: IGNORED"
    )
    print(
        "  Persistent HTML archive: IGNORED"
    )

# Use the Spotify playlist ID as the stable run identifier.
# Re-running the same playlist therefore resumes the same run.
playlist_id_match = re.search(
    r"/playlist/([A-Za-z0-9]+)",
    PLAYLIST_URL
)

if playlist_id_match:
    playlist_id = playlist_id_match.group(1)
else:
    playlist_id = re.sub(
        r"[^A-Za-z0-9_-]+",
        "_",
        PLAYLIST_URL
    ).strip("_")

RUN_ID = f"playlist_{playlist_id}"

if BLIND_PHASE1_CACHE:
    RUN_ID += "_blind"

RUN_DIR = Path("runs") / RUN_ID

RUN_DIR.mkdir(parents=True, exist_ok=True)

SPOTIFY_FILE = RUN_DIR / "spotify_tracks.csv"
MATCH_FILE = RUN_DIR / "matched_tracks.csv"
WHO_SAMPLED_REVIEW_FILE = RUN_DIR / "whosampled_review_decisions.json"
RELATIONSHIP_FILE = RUN_DIR / "relationships.csv"
RECORDINGS_FILE = RUN_DIR / "recordings.csv"
CREDITS_FILE = RUN_DIR / "credits.csv"
ARTISTS_FILE = RUN_DIR / "artists.csv"
ENRICHED_FILE = RUN_DIR / "relationships_enriched.csv"
STATE_FILE = RUN_DIR / "state.json"

HTML_DIR = RUN_DIR / "whosampled_pages"
HTML_DIR.mkdir(exist_ok=True)


def normalize(text):
    """
    Unicode-safe comparison normalization.

    This function is ONLY for comparing strings and building
    lookup keys. It must never be used as the canonical artist
    name or WhoSampled slug.

    Examples:
        Lô Borges      -> lô borges
        João Gilberto  -> joão gilberto
        Trio Mocotó    -> trio mocotó
    """

    import unicodedata
    import re

    if text is None:
        return ""

    text = str(text).strip()

    if not text:
        return ""

    # Normalize equivalent Unicode representations.
    text = unicodedata.normalize(
        "NFC",
        text
    )

    # Case-insensitive comparison.
    text = text.casefold()

    # Standardize visually equivalent punctuation.
    text = text.replace(
        "’",
        "'"
    )
    text = text.replace(
        "‘",
        "'"
    )
    text = text.replace(
        "–",
        "-"
    )
    text = text.replace(
        "—",
        "-"
    )

    # Treat punctuation that is irrelevant to comparison as
    # separators, but DO NOT strip Unicode letters.
    text = re.sub(
        r"[^\w\s'-]",
        " ",
        text,
        flags=re.UNICODE
    )

    # Collapse repeated whitespace.
    return " ".join(
        text.split()
    )


def artist_list(value, artists_json=""):
    """
    Return Spotify track artists without guessing boundaries from commas.

    New Spotify exports preserve the structured artist array in
    artists_json. The legacy comma-separated display field is used only
    when structured data is unavailable.
    """

    raw_json = "" if pd.isna(artists_json) else str(artists_json).strip()

    if raw_json:
        try:
            parsed = json.loads(raw_json)

            if isinstance(parsed, list):
                names = [
                    str(item.get("name", "")).strip()
                    for item in parsed
                    if isinstance(item, dict)
                    and str(item.get("name", "")).strip()
                ]

                if names:
                    return names[:5]

        except Exception as exc:
            print(
                "SPOTIFY ARTIST JSON WARNING:",
                repr(exc)
            )

    # Legacy fallback for older cached exports.
    return [
        x.strip()
        for x in str(value).split(",")
        if x.strip()
    ][:5]


def safe_filename(text):
    text = unquote(str(text))
    text = re.sub(r'[\\/:*?"<>|]+', "_", text)
    text = re.sub(r"\s+", "_", text)
    return text[:150]


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(
            state,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )


def load_state():
    if STATE_FILE.exists():
        return json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
        )

    return {
        "playlist_exported": False,
        "tracks_processed": [],
        "relationships_parsed": [],
        "spotify_enriched": False,
        "stopped_on_429": False,
    }


def save_artist_slug_cache(data):
    ARTIST_SLUG_CACHE_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    ARTIST_SLUG_CACHE_FILE.write_text(
        json.dumps(
            data,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )


def write_dataframe(df, path):
    df.to_csv(
        path,
        index=False,
        encoding="utf-8"
    )


def write_normalized_outputs(
    accepted_html_files,
    matched_file,
    spotify_file,
    relationship_rows,
):
    """
    Write the normalized pipeline data model.

    Spotify is the baseline source of identity for playlist seed
    recordings and playlist artists.

    WhoSampled enriches those identities when a verified match exists.

    Outputs:

        recordings.csv
            one row for every Spotify playlist recording,
            plus any future source-only recordings

        artists.csv
            one row for every structured Spotify artist identity,
            plus WhoSampled credit artists not yet reconciled to Spotify

        credits.csv
            recording-to-artist role assertions with source provenance

        relationships.csv
            recording-to-recording musical relationships

    This function uses only already-collected local HTML and CSV files.
    It performs no network requests.
    """

    import hashlib
    import json

    from bs4 import BeautifulSoup

    from parse_whosampled_track import (
        extract_source_metadata,
    )

    matched_df = pd.read_csv(
        matched_file
    )

    spotify_df = pd.read_csv(
        spotify_file
    )

    # --------------------------------------------------------
    # Helpers.
    # --------------------------------------------------------

    def clean(value):
        if pd.isna(value):
            return ""
        return str(value).strip()

    def stable_id(prefix, namespace, value):
        raw = (
            f"{namespace}:"
            f"{value}"
        )

        return (
            prefix
            + hashlib.sha1(
                raw.encode("utf-8")
            ).hexdigest()[:16]
        )

    def ws_url_key(url):
        value = clean(url)

        if not value:
            return ""

        return (
            unquote(value)
            .rstrip("/")
            .casefold()
        )

    def parse_spotify_artists(row):
        """
        Prefer the structured artists_json field.

        A legacy fallback is retained for old cached Spotify exports,
        but new exports should always contain artists_json.
        """

        raw_json = clean(
            row.get(
                "artists_json",
                ""
            )
        )

        if raw_json:
            try:
                parsed = json.loads(
                    raw_json
                )

                if isinstance(
                    parsed,
                    list
                ):
                    return [
                        item
                        for item in parsed
                        if isinstance(
                            item,
                            dict
                        )
                    ]

            except Exception as exc:
                print(
                    "SPOTIFY ARTIST JSON WARNING:",
                    repr(exc),
                )

        # Legacy fallback.
        ids = [
            value.strip()
            for value in clean(
                row.get(
                    "artist_ids",
                    ""
                )
            ).split(",")
            if value.strip()
        ]

        names_raw = clean(
            row.get(
                "artist_names",
                ""
            )
        )

        # One Spotify artist ID means the whole display string belongs
        # to that one artist, even if the artist name contains commas.
        if len(ids) == 1:

            return [{
                "id": ids[0],
                "name": names_raw,
                "uri": "",
                "spotify_url": "",
            }]

        # This fallback is inherently ambiguous for multi-artist legacy
        # rows whose artist names themselves contain commas. It exists
        # only so older cached exports do not crash.
        names = [
            value.strip()
            for value in names_raw.split(",")
            if value.strip()
        ]

        if ids and len(ids) == len(names):

            return [
                {
                    "id": artist_id,
                    "name": artist_name,
                    "uri": "",
                    "spotify_url": (
                        "https://open.spotify.com/artist/"
                        + artist_id
                    ),
                }
                for artist_id, artist_name
                in zip(ids, names)
            ]

        if names_raw:

            print(
                "LEGACY SPOTIFY ARTIST WARNING:",
                clean(
                    row.get(
                        "title",
                        ""
                    )
                ),
                "could not safely reconstruct "
                "structured artist identities."
            )

        return []

    # --------------------------------------------------------
    # WhoSampled match lookups.
    # --------------------------------------------------------

    match_by_spotify_id = {}
    match_by_url = {}

    for _, row in matched_df.iterrows():

        spotify_id = clean(
            row.get(
                "spotify_track_id",
                ""
            )
        )

        if spotify_id:
            match_by_spotify_id[
                spotify_id
            ] = row.to_dict()

        url_key = ws_url_key(
            row.get(
                "whosampled_url",
                ""
            )
        )

        if url_key:
            match_by_url[
                url_key
            ] = row.to_dict()

    # --------------------------------------------------------
    # Canonical in-memory stores.
    # --------------------------------------------------------

    recordings_by_id = {}
    recording_ids_by_spotify_id = {}
    recording_ids_by_url = {}

    artists_by_id = {}
    spotify_artist_ids = {}
    spotify_artist_name_ids = {}

    credit_rows = []
    seen_credit_keys = set()

    normalized_relationship_rows = []

    # --------------------------------------------------------
    # Credits helper.
    # --------------------------------------------------------

    def add_credit(
        recording_id,
        artist_id,
        artist_name,
        role,
        source_role,
        source,
        source_url,
        artist_order="",
    ):
        artist_name = clean(
            artist_name
        )

        role = clean(
            role
        )

        if (
            not recording_id
            or not artist_name
            or not role
        ):
            return

        credit_key = (
            f"{recording_id}|"
            f"{artist_id}|"
            f"{artist_name}|"
            f"{role}|"
            f"{source_role}|"
            f"{source}"
        )

        if credit_key in seen_credit_keys:
            return

        seen_credit_keys.add(
            credit_key
        )

        credit_id = stable_id(
            "CRD_",
            "credit",
            credit_key,
        )

        credit_rows.append({
            "credit_id":
                credit_id,

            "recording_id":
                recording_id,

            "artist_id":
                artist_id,

            "artist_name":
                artist_name,

            "role":
                role,

            "source_role":
                clean(
                    source_role
                ),

            "artist_order":
                artist_order,

            "source":
                clean(
                    source
                ),

            "source_url":
                clean(
                    source_url
                ),
        })

    # --------------------------------------------------------
    # STEP A
    #
    # Every Spotify playlist track becomes a canonical recording.
    # Every structured Spotify artist becomes a canonical artist.
    # --------------------------------------------------------

    for _, row in spotify_df.iterrows():

        spotify_track_id = clean(
            row.get(
                "spotify_track_id",
                ""
            )
        )

        if not spotify_track_id:
            continue

        recording_id = stable_id(
            "REC_",
            "spotify",
            spotify_track_id,
        )

        recording_ids_by_spotify_id[
            spotify_track_id
        ] = recording_id

        match_row = (
            match_by_spotify_id.get(
                spotify_track_id,
                {}
            )
        )

        whosampled_url = clean(
            match_row.get(
                "whosampled_url",
                ""
            )
        )

        whosampled_status = clean(
            match_row.get(
                "match_status",
                ""
            )
        )

        if whosampled_url:
            recording_ids_by_url[
                ws_url_key(
                    whosampled_url
                )
            ] = recording_id

        album_release_date = clean(
            row.get(
                "album_release_date",
                ""
            )
        )

        release_year = (
            album_release_date[:4]
            if album_release_date
            else ""
        )

        recordings_by_id[
            recording_id
        ] = {
            "recording_id":
                recording_id,

            # Canonical working fields.
            # Spotify initializes them for seed recordings.
            "title":
                clean(
                    row.get(
                        "title",
                        ""
                    )
                ),

            "artist_names":
                clean(
                    row.get(
                        "artist_names",
                        ""
                    )
                ),

            "album":
                clean(
                    row.get(
                        "album_name",
                        ""
                    )
                ),

            "label":
                clean(
                    row.get(
                        "album_label",
                        ""
                    )
                ),

            "release_year":
                release_year,

            "duration":
                clean(
                    row.get(
                        "duration_ms",
                        ""
                    )
                ),

            "genre":
                "",

            "keywords":
                "",

            # Spotify provenance.
            "spotify_track_id":
                spotify_track_id,

            "spotify_uri":
                clean(
                    row.get(
                        "spotify_uri",
                        ""
                    )
                ),

            "spotify_url":
                clean(
                    row.get(
                        "spotify_url",
                        ""
                    )
                ),

            "spotify_isrc":
                clean(
                    row.get(
                        "isrc",
                        ""
                    )
                ),

            "spotify_album_name":
                clean(
                    row.get(
                        "album_name",
                        ""
                    )
                ),

            "spotify_album_id":
                clean(
                    row.get(
                        "album_id",
                        ""
                    )
                ),

            "spotify_album_release_date":
                album_release_date,

            "spotify_album_release_precision":
                clean(
                    row.get(
                        "album_release_precision",
                        ""
                    )
                ),

            "spotify_album_image_url":
                clean(
                    row.get(
                        "album_image_url",
                        ""
                    )
                ),

            "spotify_album_label":
                clean(
                    row.get(
                        "album_label",
                        ""
                    )
                ),

            "spotify_duration_ms":
                clean(
                    row.get(
                        "duration_ms",
                        ""
                    )
                ),

            # Phase-1 WhoSampled resolution state exists even if
            # no WhoSampled page was found.
            "whosampled_match_status":
                whosampled_status,

            "whosampled_url":
                whosampled_url,

            # WhoSampled enrichment fields.
            "whosampled_title":
                "",

            "whosampled_artist_names":
                "",

            "whosampled_album":
                "",

            "whosampled_label":
                "",

            "whosampled_release_year":
                "",

            "whosampled_duration":
                "",

            "whosampled_genre":
                "",

            "whosampled_keywords":
                "",

            "whosampled_thumbnail_url":
                "",

            "whosampled_thumbnail_path":
                "",

            "whosampled_thumbnail_status":
                "unavailable",

            "youtube_video_id":
                "",

            "youtube_url":
                "",

            "youtube_thumbnail_url":
                "",

            # Future enrichment source slots.
            "musicbrainz_recording_id":
                "",

            "musicbrainz_release_id":
                "",

            "musicbrainz_country":
                "",

            "musicbrainz_label":
                "",
        }

        spotify_artists = (
            parse_spotify_artists(
                row
            )
        )

        for artist_order, artist in enumerate(
            spotify_artists,
            start=1,
        ):

            spotify_artist_id = clean(
                artist.get(
                    "id",
                    ""
                )
            )

            artist_name = clean(
                artist.get(
                    "name",
                    ""
                )
            )

            if not (
                spotify_artist_id
                and artist_name
            ):
                continue

            artist_id = stable_id(
                "ART_",
                "spotify",
                spotify_artist_id,
            )

            spotify_artist_ids[
                spotify_artist_id
            ] = artist_id

            name_key = normalize(
                artist_name
            )

            if name_key:
                spotify_artist_name_ids.setdefault(
                    name_key,
                    []
                )

                if (
                    artist_id
                    not in spotify_artist_name_ids[
                        name_key
                    ]
                ):
                    spotify_artist_name_ids[
                        name_key
                    ].append(
                        artist_id
                    )

            artists_by_id[
                artist_id
            ] = {
                "artist_id":
                    artist_id,

                "canonical_name":
                    artist_name,

                "spotify_artist_id":
                    spotify_artist_id,

                "spotify_uri":
                    clean(
                        artist.get(
                            "uri",
                            ""
                        )
                    ),

                "spotify_url":
                    clean(
                        artist.get(
                            "spotify_url",
                            ""
                        )
                    ),

                "whosampled_name":
                    "",

                "whosampled_url":
                    "",

                "wikidata_qid":
                    "",

                "musicbrainz_artist_id":
                    "",
            }

            add_credit(
                recording_id=
                    recording_id,

                artist_id=
                    artist_id,

                artist_name=
                    artist_name,

                role=
                    "performer",

                source_role=
                    "Spotify track artist",

                source=
                    "Spotify",

                source_url=
                    clean(
                        row.get(
                            "spotify_url",
                            ""
                        )
                    ),

                artist_order=
                    artist_order,
            )

    # --------------------------------------------------------
    # STEP B
    #
    # Approved WhoSampled primary pages enrich existing Spotify
    # recordings. They no longer determine whether a recording exists.
    # --------------------------------------------------------

    for html_file in accepted_html_files:

        html_path = (
            HTML_DIR
            / html_file
        )

        if not html_path.exists():
            continue

        try:

            soup = BeautifulSoup(
                html_path.read_text(
                    encoding="utf-8",
                    errors="ignore",
                ),
                "html.parser",
            )

            source = (
                extract_source_metadata(
                    soup
                )
            )

        except Exception as exc:

            print(
                "NORMALIZED PRIMARY PARSE ERROR:",
                html_path,
                repr(exc),
            )

            continue

        source_url = clean(
            source.get(
                "source_url",
                ""
            )
        ).rstrip("/")

        if not source_url:
            continue

        source_url_key = (
            ws_url_key(
                source_url
            )
        )

        matched_row = (
            match_by_url.get(
                source_url_key,
                {}
            )
        )

        spotify_track_id = clean(
            matched_row.get(
                "spotify_track_id",
                ""
            )
        )

        recording_id = (
            recording_ids_by_spotify_id.get(
                spotify_track_id,
                ""
            )
        )

        # Defensive fallback:
        # preserve a verified WhoSampled recording even if its Spotify
        # bridge is unexpectedly absent. This should be rare for seed
        # pages but prevents silent data loss.
        if not recording_id:

            recording_id = stable_id(
                "REC_",
                "whosampled",
                source_url_key,
            )

            if recording_id not in recordings_by_id:

                recordings_by_id[
                    recording_id
                ] = {
                    "recording_id":
                        recording_id,

                    "title":
                        clean(
                            source.get(
                                "source_title",
                                ""
                            )
                        ),

                    "artist_names":
                        clean(
                            source.get(
                                "source_artists",
                                ""
                            )
                        ),

                    "album":
                        clean(
                            source.get(
                                "source_album",
                                ""
                            )
                        ),

                    "label":
                        clean(
                            source.get(
                                "source_label",
                                ""
                            )
                        ),

                    "release_year":
                        clean(
                            source.get(
                                "source_release_year",
                                ""
                            )
                        ),

                    "duration":
                        clean(
                            source.get(
                                "source_duration",
                                ""
                            )
                        ),

                    "genre":
                        clean(
                            source.get(
                                "source_genre",
                                ""
                            )
                        ),

                    "keywords":
                        clean(
                            source.get(
                                "source_keywords",
                                ""
                            )
                        ),

                    "spotify_track_id": "",
                    "spotify_uri": "",
                    "spotify_url": "",
                    "spotify_isrc": "",
                    "spotify_album_name": "",
                    "spotify_album_id": "",
                    "spotify_album_release_date": "",
                    "spotify_album_release_precision": "",
                    "spotify_album_image_url": "",
                    "spotify_album_label": "",
                    "spotify_duration_ms": "",

                    "whosampled_match_status":
                        "matched",

                    "whosampled_url":
                        source_url,

                    "whosampled_title": "",
                    "whosampled_artist_names": "",
                    "whosampled_album": "",
                    "whosampled_label": "",
                    "whosampled_release_year": "",
                    "whosampled_duration": "",
                    "whosampled_genre": "",
                    "whosampled_keywords": "",
                    "whosampled_thumbnail_url": "",
                    "whosampled_thumbnail_path": "",
                    "whosampled_thumbnail_status":
                        "unavailable",

                    "youtube_video_id": "",
                    "youtube_url": "",
                    "youtube_thumbnail_url": "",

                    "musicbrainz_recording_id": "",
                    "musicbrainz_release_id": "",
                    "musicbrainz_country": "",
                    "musicbrainz_label": "",
                }

        recording_ids_by_url[
            source_url_key
        ] = recording_id

        recording = (
            recordings_by_id[
                recording_id
            ]
        )

        local_thumbnail = (
            HTML_DIR.parent
            / "whosampled_media"
            / (
                html_path.stem
                + ".png"
            )
        )

        recording.update({
            "whosampled_match_status":
                "matched",

            "whosampled_url":
                source_url,

            "whosampled_title":
                clean(
                    source.get(
                        "source_title",
                        ""
                    )
                ),

            "whosampled_artist_names":
                clean(
                    source.get(
                        "source_artists",
                        ""
                    )
                ),

            "whosampled_album":
                clean(
                    source.get(
                        "source_album",
                        ""
                    )
                ),

            "whosampled_label":
                clean(
                    source.get(
                        "source_label",
                        ""
                    )
                ),

            "whosampled_release_year":
                clean(
                    source.get(
                        "source_release_year",
                        ""
                    )
                ),

            "whosampled_duration":
                clean(
                    source.get(
                        "source_duration",
                        ""
                    )
                ),

            "whosampled_genre":
                clean(
                    source.get(
                        "source_genre",
                        ""
                    )
                ),

            "whosampled_keywords":
                clean(
                    source.get(
                        "source_keywords",
                        ""
                    )
                ),

            "whosampled_thumbnail_url":
                clean(
                    source.get(
                        "source_thumbnail_url",
                        ""
                    )
                ),

            "whosampled_thumbnail_path":
                (
                    str(
                        local_thumbnail
                    )
                    if local_thumbnail.exists()
                    else ""
                ),

            "whosampled_thumbnail_status":
                (
                    "captured"
                    if local_thumbnail.exists()
                    else "unavailable"
                ),

            "youtube_video_id":
                clean(
                    source.get(
                        "source_youtube_video_id",
                        ""
                    )
                ),

            "youtube_url":
                clean(
                    source.get(
                        "source_youtube_url",
                        ""
                    )
                ),

            "youtube_thumbnail_url":
                clean(
                    source.get(
                        "source_youtube_thumbnail_url",
                        ""
                    )
                ),
        })

        # If this is a WhoSampled-only fallback recording, allow its
        # source metadata to initialize otherwise-empty canonical fields.
        for canonical_field, ws_field in (
            ("title", "whosampled_title"),
            ("artist_names", "whosampled_artist_names"),
            ("album", "whosampled_album"),
            ("label", "whosampled_label"),
            ("release_year", "whosampled_release_year"),
            ("duration", "whosampled_duration"),
            ("genre", "whosampled_genre"),
            ("keywords", "whosampled_keywords"),
        ):

            if not clean(
                recording.get(
                    canonical_field,
                    ""
                )
            ):
                recording[
                    canonical_field
                ] = clean(
                    recording.get(
                        ws_field,
                        ""
                    )
                )

        # ----------------------------------------------------
        # Literal WhoSampled primary-artist profile identities.
        #
        # extract_source_metadata() preserves the exact artist
        # profile links present on the verified WhoSampled track
        # page in source_artist_profiles. These URLs are direct
        # WhoSampled evidence and must not be reconstructed from
        # Spotify/Wikidata spelling.
        #
        # Reconciliation policy:
        #   1. Reuse an artist already carrying this exact
        #      WhoSampled profile URL.
        #   2. Otherwise enrich a Spotify artist only when the
        #      normalized WhoSampled name maps uniquely in this run.
        #   3. Otherwise create a deterministic WhoSampled-profile
        #      artist identity keyed by the literal profile URL.
        # ----------------------------------------------------

        raw_artist_profiles = source.get(
            "source_artist_profiles",
            "[]",
        )

        try:

            parsed_artist_profiles = json.loads(
                raw_artist_profiles
                or "[]"
            )

        except Exception:

            parsed_artist_profiles = []

        if not isinstance(
            parsed_artist_profiles,
            list,
        ):

            parsed_artist_profiles = []

        for artist_order, profile in enumerate(
            parsed_artist_profiles,
            start=1,
        ):

            if not isinstance(
                profile,
                dict,
            ):
                continue

            artist_name = clean(
                profile.get(
                    "artist",
                    profile.get(
                        "name",
                        "",
                    ),
                )
            )

            artist_profile_url = clean(
                profile.get(
                    "url",
                    "",
                )
            )

            if not (
                artist_name
                and artist_profile_url
            ):
                continue

            artist_profile_url_key = ws_url_key(
                artist_profile_url
            )

            # First preference: an artist already carrying this
            # exact literal WhoSampled profile URL.
            artist_id = ""

            for existing_artist_id, existing_artist in (
                artists_by_id.items()
            ):

                existing_whosampled_url = clean(
                    existing_artist.get(
                        "whosampled_url",
                        "",
                    )
                )

                if (
                    existing_whosampled_url
                    and ws_url_key(
                        existing_whosampled_url
                    )
                    == artist_profile_url_key
                ):

                    artist_id = existing_artist_id
                    break

            # Second preference: uniquely attributable Spotify
            # artist identity by normalized name.
            if not artist_id:

                artist_name_key = normalize(
                    artist_name
                )

                possible_spotify_ids = (
                    spotify_artist_name_ids.get(
                        artist_name_key,
                        [],
                    )
                )

                if len(
                    possible_spotify_ids
                ) == 1:

                    artist_id = (
                        possible_spotify_ids[0]
                    )

            # Third preference: reconcile the literal WhoSampled
            # profile to a Spotify-backed artist through an already
            # learned Spotify-name -> WhoSampled-slug mapping.
            #
            # This handles legitimate cross-source naming differences
            # such as Spotify "Jorge Ben Jor" versus WhoSampled
            # "Jorge Ben". The literal profile URL remains the
            # authoritative WhoSampled identity; the learned slug is
            # used only as cross-source reconciliation evidence.
            #
            # Require the learned evidence to identify exactly one
            # Spotify-backed artist in this run. Ambiguous evidence
            # falls through to the deterministic WhoSampled-profile
            # identity rather than guessing.
            if not artist_id:

                try:
                    profile_parts = [
                        part
                        for part in urlparse(
                            artist_profile_url
                        ).path.split("/")
                        if part
                    ]

                    literal_profile_slug = (
                        unquote(
                            profile_parts[0]
                        )
                        if len(profile_parts) == 1
                        else ""
                    )

                except Exception:
                    literal_profile_slug = ""

                learned_spotify_artist_ids = []

                if literal_profile_slug:

                    literal_slug_key = normalize(
                        literal_profile_slug.replace(
                            "-",
                            " ",
                        )
                    )

                    for (
                        learned_artist_name_key,
                        learned_slugs,
                    ) in artist_slug_cache.items():

                        if isinstance(
                            learned_slugs,
                            str,
                        ):
                            learned_slugs = [
                                learned_slugs
                            ]

                        if not isinstance(
                            learned_slugs,
                            (list, tuple, set),
                        ):
                            continue

                        learned_slug_matches = any(
                            normalize(
                                str(slug).replace(
                                    "-",
                                    " ",
                                )
                            )
                            == literal_slug_key
                            for slug in learned_slugs
                            if clean(slug)
                        )

                        if not learned_slug_matches:
                            continue

                        for possible_artist_id in (
                            spotify_artist_name_ids.get(
                                normalize(
                                    learned_artist_name_key
                                ),
                                [],
                            )
                        ):

                            if (
                                possible_artist_id
                                not in learned_spotify_artist_ids
                            ):
                                learned_spotify_artist_ids.append(
                                    possible_artist_id
                                )

                if len(
                    learned_spotify_artist_ids
                ) == 1:

                    artist_id = (
                        learned_spotify_artist_ids[0]
                    )

                    print(
                        "RECONCILED LITERAL WHOSAMPLED PROFILE "
                        "TO SPOTIFY ARTIST VIA LEARNED SLUG:",
                        artist_name,
                        "->",
                        artist_profile_url,
                        "->",
                        artists_by_id.get(
                            artist_id,
                            {},
                        ).get(
                            "canonical_name",
                            artist_id,
                        ),
                    )

            # Otherwise preserve the literal WhoSampled profile as
            # its own deterministic artist identity. The external
            # URL, rather than the display spelling, is the key.
            if not artist_id:

                artist_id = stable_id(
                    "ART_",
                    "whosampled-profile",
                    artist_profile_url_key,
                )

            if artist_id not in artists_by_id:

                artists_by_id[
                    artist_id
                ] = {
                    "artist_id":
                        artist_id,

                    "canonical_name":
                        artist_name,

                    "spotify_artist_id":
                        "",

                    "spotify_uri":
                        "",

                    "spotify_url":
                        "",

                    "whosampled_name":
                        artist_name,

                    "whosampled_url":
                        artist_profile_url,

                    "wikidata_qid":
                        "",

                    "musicbrainz_artist_id":
                        "",
                }

            else:

                artist_record = (
                    artists_by_id[
                        artist_id
                    ]
                )

                if not clean(
                    artist_record.get(
                        "whosampled_name",
                        "",
                    )
                ):

                    artist_record[
                        "whosampled_name"
                    ] = artist_name

                if not clean(
                    artist_record.get(
                        "whosampled_url",
                        "",
                    )
                ):

                    artist_record[
                        "whosampled_url"
                    ] = artist_profile_url

            add_credit(
                recording_id=
                    recording_id,

                artist_id=
                    artist_id,

                artist_name=
                    artist_name,

                role=
                    "performer",

                source_role=
                    "WhoSampled primary artist",

                source=
                    "WhoSampled",

                source_url=
                    source_url,

                artist_order=
                    artist_order,
            )

        # ----------------------------------------------------
        # WhoSampled structured credits.
        # ----------------------------------------------------

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
                    ""
                )
            )

            role = clean(
                credit.get(
                    "role",
                    ""
                )
            )

            source_role = clean(
                credit.get(
                    "source_role",
                    ""
                )
            )

            if not (
                artist_name
                and role
            ):
                continue

            artist_name_key = normalize(
                artist_name
            )

            possible_spotify_ids = (
                spotify_artist_name_ids.get(
                    artist_name_key,
                    []
                )
            )

            # Reuse a Spotify artist identity only when the normalized
            # name maps uniquely within this run. Otherwise preserve the
            # WhoSampled observation as its own unresolved artist identity.
            if len(possible_spotify_ids) == 1:

                artist_id = (
                    possible_spotify_ids[0]
                )

                artists_by_id[
                    artist_id
                ][
                    "whosampled_name"
                ] = artist_name

            else:

                artist_id = stable_id(
                    "ART_",
                    "whosampled-name",
                    artist_name_key,
                )

                if artist_id not in artists_by_id:

                    artists_by_id[
                        artist_id
                    ] = {
                        "artist_id":
                            artist_id,

                        "canonical_name":
                            artist_name,

                        "spotify_artist_id":
                            "",

                        "spotify_uri":
                            "",

                        "spotify_url":
                            "",

                        "whosampled_name":
                            artist_name,

                        "whosampled_url":
                            "",

                        "wikidata_qid":
                            "",

                        "musicbrainz_artist_id":
                            "",
                    }

            add_credit(
                recording_id=
                    recording_id,

                artist_id=
                    artist_id,

                artist_name=
                    artist_name,

                role=
                    role,

                source_role=
                    source_role,

                source=
                    "WhoSampled",

                source_url=
                    source_url,
            )

    # --------------------------------------------------------
    # STEP C
    #
    # Normalize track-to-track relationships.
    # --------------------------------------------------------

    for row in relationship_rows:

        source_url = clean(
            row.get(
                "source_url",
                ""
            )
        )

        source_recording_id = (
            recording_ids_by_url.get(
                ws_url_key(
                    source_url
                ),
                ""
            )
        )

        relationship_key = (
            f"{source_recording_id}|"
            f"{row.get('relationship_type', '')}|"
            f"{row.get('related_track', '')}|"
            f"{row.get('related_artist', '')}|"
            f"{row.get('whosampled_relationship_url', '')}"
        )

        relationship_id = stable_id(
            "REL_",
            "relationship",
            relationship_key,
        )

        normalized_relationship_rows.append({
            "relationship_id":
                relationship_id,

            "source_recording_id":
                source_recording_id,

            "target_recording_id":
                "",

            "relationship_type":
                row.get(
                    "relationship_type",
                    "",
                ),

            "related_track":
                row.get(
                    "related_track",
                    "",
                ),

            "related_artist":
                row.get(
                    "related_artist",
                    "",
                ),

            "year":
                row.get(
                    "year",
                    "",
                ),

            "whosampled_relationship_url":
                row.get(
                    "whosampled_relationship_url",
                    "",
                ),

            "detail":
                row.get(
                    "detail",
                    "",
                ),
        })

    # --------------------------------------------------------
    # Write outputs.
    # --------------------------------------------------------

    recordings_df = pd.DataFrame(
        recordings_by_id.values()
    )

    artists_df = pd.DataFrame(
        artists_by_id.values()
    )

    credits_df = pd.DataFrame(
        credit_rows
    )

    relationships_df = pd.DataFrame(
        normalized_relationship_rows
    )

    write_dataframe(
        recordings_df,
        RECORDINGS_FILE
    )

    write_dataframe(
        artists_df,
        ARTISTS_FILE
    )

    write_dataframe(
        credits_df,
        CREDITS_FILE
    )

    write_dataframe(
        relationships_df,
        RELATIONSHIP_FILE
    )

    print()
    print("=" * 80)
    print("NORMALIZED OUTPUTS")
    print("=" * 80)

    print(
        "Recordings:",
        len(recordings_df),
    )

    print(
        "Artists:",
        len(artists_df),
    )

    print(
        "Credits:",
        len(credits_df),
    )

    print(
        "Relationships:",
        len(relationships_df),
    )


def integrate_step5_spotify_targets(enriched_df):
    """
    Fold accepted Step-5 Spotify resolutions back into the canonical
    recordings/artists/credits/relationships outputs.

    Only rows whose final spotify_match_status is "matched" are integrated.
    Review rows therefore enter the graph only after explicit acceptance by
    spotify_review_ui. Unmatched/not_found/unresolved rows remain unchanged.

    Identity policy:
      * Recording identity is deterministic from Spotify track ID.
      * Existing playlist Recording IDs are reused automatically because the
        same stable-ID namespace is used by write_normalized_outputs().
      * Multiple relationship rows resolving to one Spotify track reuse one
        Recording node.
      * Spotify artists are materialized only when artist IDs are available;
        display-name parsing is never used to invent artist identity.
    """

    def clean(value):
        if pd.isna(value):
            return ""
        return str(value).strip()

    def stable_id(prefix, namespace, value):
        raw = f"{namespace}:{value}"
        return prefix + hashlib.sha1(
            raw.encode("utf-8")
        ).hexdigest()[:16]

    if enriched_df is None or enriched_df.empty:
        print("STEP 5 CANONICAL INTEGRATION: no enriched rows")
        return

    recordings_df = pd.read_csv(RECORDINGS_FILE)
    artists_df = pd.read_csv(ARTISTS_FILE)
    credits_df = pd.read_csv(CREDITS_FILE)
    canonical_relationships_df = pd.read_csv(RELATIONSHIP_FILE)

    # Relationship IDs are string graph identifiers. Force object dtype so
    # filling previously blank target_recording_id cells never relies on
    # pandas' float/NaN inference.
    if "target_recording_id" in canonical_relationships_df.columns:
        canonical_relationships_df["target_recording_id"] = (
            canonical_relationships_df["target_recording_id"]
            .fillna("")
            .astype(object)
        )

    recording_columns = list(recordings_df.columns)
    artist_columns = list(artists_df.columns)
    credit_columns = list(credits_df.columns)

    recordings = {
        clean(row.get("recording_id")): row.to_dict()
        for _, row in recordings_df.iterrows()
        if clean(row.get("recording_id"))
    }
    recording_id_by_spotify = {
        clean(row.get("spotify_track_id")): clean(row.get("recording_id"))
        for _, row in recordings_df.iterrows()
        if clean(row.get("spotify_track_id"))
    }

    artists = {
        clean(row.get("artist_id")): row.to_dict()
        for _, row in artists_df.iterrows()
        if clean(row.get("artist_id"))
    }
    artist_id_by_spotify = {
        clean(row.get("spotify_artist_id")): clean(row.get("artist_id"))
        for _, row in artists_df.iterrows()
        if clean(row.get("spotify_artist_id"))
    }

    credits = [row.to_dict() for _, row in credits_df.iterrows()]
    credit_keys = {
        (
            clean(row.get("recording_id")),
            clean(row.get("artist_id")),
            clean(row.get("role")),
            clean(row.get("source_role")),
            clean(row.get("source")),
            clean(row.get("source_url")),
        )
        for row in credits
    }

    relationship_index = {
        clean(row.get("whosampled_relationship_url")): index
        for index, row in canonical_relationships_df.iterrows()
        if clean(row.get("whosampled_relationship_url"))
    }

    integrated_relationships = 0
    created_recordings = 0
    reused_recordings = 0
    created_artists = 0
    created_credits = 0
    matched_without_artist_ids = 0

    for _, row in enriched_df.iterrows():

        if clean(row.get("spotify_match_status")).lower() != "matched":
            continue

        spotify_track_id = clean(row.get("spotify_track_id"))
        relationship_url = clean(row.get("whosampled_relationship_url"))

        if not spotify_track_id or not relationship_url:
            continue

        recording_id = recording_id_by_spotify.get(spotify_track_id)

        if recording_id:
            reused_recordings += 1
        else:
            recording_id = stable_id(
                "REC_",
                "spotify",
                spotify_track_id,
            )
            recording_id_by_spotify[spotify_track_id] = recording_id

            release_date = clean(row.get("spotify_album_release_date"))
            spotify_url = clean(row.get("spotify_url")) or (
                "https://open.spotify.com/track/" + spotify_track_id
            )

            new_recording = {column: "" for column in recording_columns}
            new_recording.update({
                "recording_id": recording_id,
                "title": clean(row.get("spotify_title")) or clean(row.get("related_track")),
                "artist_names": clean(row.get("spotify_artist_names")) or clean(row.get("related_artist")),
                "album": clean(row.get("spotify_album_name")),
                "label": clean(row.get("spotify_album_label")),
                "release_year": release_date[:4] if release_date else clean(row.get("year"))[:4],
                "duration": clean(row.get("spotify_duration_ms")),
                "spotify_track_id": spotify_track_id,
                "spotify_uri": clean(row.get("spotify_uri")),
                "spotify_url": spotify_url,
                "spotify_isrc": clean(row.get("spotify_isrc")),
                "spotify_album_name": clean(row.get("spotify_album_name")),
                "spotify_album_id": clean(row.get("spotify_album_id")),
                "spotify_album_release_date": release_date,
                "spotify_album_release_precision": clean(row.get("spotify_album_release_precision")),
                "spotify_album_image_url": clean(row.get("spotify_album_image_url")),
                "spotify_album_label": clean(row.get("spotify_album_label")),
                "spotify_duration_ms": clean(row.get("spotify_duration_ms")),
                "whosampled_match_status": "relationship_evidence",
                "whosampled_title": clean(row.get("related_track")),
                "whosampled_artist_names": clean(row.get("related_artist")),
            })
            recordings[recording_id] = new_recording
            created_recordings += 1

        relationship_row_index = relationship_index.get(relationship_url)
        if relationship_row_index is not None:
            canonical_relationships_df.at[
                relationship_row_index,
                "target_recording_id",
            ] = recording_id
            integrated_relationships += 1

        raw_artist_ids = clean(row.get("spotify_artist_ids"))
        raw_artist_names = clean(row.get("spotify_artist_names"))

        artist_ids = [
            value.strip()
            for value in raw_artist_ids.split(",")
            if value.strip()
        ]
        artist_names = [
            value.strip()
            for value in raw_artist_names.split(",")
            if value.strip()
        ]

        if not artist_ids:
            matched_without_artist_ids += 1
            continue

        if len(artist_ids) != len(artist_names):
            print(
                "STEP 5 ARTIST STRUCTURE WARNING:",
                clean(row.get("spotify_title")),
                "artist IDs/names could not be paired safely; credits skipped."
            )
            matched_without_artist_ids += 1
            continue

        spotify_track_url = clean(row.get("spotify_url")) or (
            "https://open.spotify.com/track/" + spotify_track_id
        )

        for artist_order, (spotify_artist_id, artist_name) in enumerate(
            zip(artist_ids, artist_names),
            start=1,
        ):
            artist_id = artist_id_by_spotify.get(spotify_artist_id)

            if not artist_id:
                artist_id = stable_id(
                    "ART_",
                    "spotify",
                    spotify_artist_id,
                )
                artist_id_by_spotify[spotify_artist_id] = artist_id

                new_artist = {column: "" for column in artist_columns}
                new_artist.update({
                    "artist_id": artist_id,
                    "canonical_name": artist_name,
                    "spotify_artist_id": spotify_artist_id,
                    "spotify_uri": "spotify:artist:" + spotify_artist_id,
                    "spotify_url": "https://open.spotify.com/artist/" + spotify_artist_id,
                })
                artists[artist_id] = new_artist
                created_artists += 1

            credit_key = (
                recording_id,
                artist_id,
                "performer",
                "Spotify track artist",
                "Spotify",
                spotify_track_url,
            )

            if credit_key in credit_keys:
                continue

            credit_id = stable_id(
                "CRD_",
                "credit",
                "|".join(str(value) for value in credit_key),
            )
            new_credit = {column: "" for column in credit_columns}
            new_credit.update({
                "credit_id": credit_id,
                "recording_id": recording_id,
                "artist_id": artist_id,
                "artist_name": artist_name,
                "role": "performer",
                "source_role": "Spotify track artist",
                "artist_order": artist_order,
                "source": "Spotify",
                "source_url": spotify_track_url,
            })
            credits.append(new_credit)
            credit_keys.add(credit_key)
            created_credits += 1

    final_recordings_df = pd.DataFrame(
        list(recordings.values()),
        columns=recording_columns,
    )
    final_artists_df = pd.DataFrame(
        list(artists.values()),
        columns=artist_columns,
    )
    final_credits_df = pd.DataFrame(
        credits,
        columns=credit_columns,
    )

    write_dataframe(final_recordings_df, RECORDINGS_FILE)
    write_dataframe(final_artists_df, ARTISTS_FILE)
    write_dataframe(final_credits_df, CREDITS_FILE)
    write_dataframe(canonical_relationships_df, RELATIONSHIP_FILE)

    print()
    print("=" * 80)
    print("STEP 5 — CANONICAL TARGET INTEGRATION")
    print("=" * 80)
    print("Matched relationship targets integrated:", integrated_relationships)
    print("New canonical recordings:", created_recordings)
    print("Existing canonical recordings reused:", reused_recordings)
    print("New Spotify artists:", created_artists)
    print("New Spotify performer credits:", created_credits)
    if matched_without_artist_ids:
        print(
            "Matched targets without safely structured Spotify artist IDs:",
            matched_without_artist_ids,
        )



state = load_state()

if BLIND_PHASE1_CACHE:

    state = {
        "playlist_exported": False,
        "tracks_processed": [],
        "relationships_parsed": [],
        "spotify_enriched": False,
        "stopped_on_429": False,
        "secondary_pages_processed": [],
    }

    save_state(
        state
    )

# Shared WhoSampled identity caches.
artist_slug_cache = load_artist_slug_cache()
wikidata_cache = load_wikidata_cache()

if BLIND_PHASE1_CACHE:

    artist_slug_cache = {}

    print(
        "Learned artist slug cache: IGNORED FOR PHASE 1"
    )


# ============================================================
# STEP 1 — SPOTIFY PLAYLIST
# ============================================================

if not state["playlist_exported"]:

    print("\n" + "=" * 80)
    print("STEP 1 — SPOTIFY PLAYLIST")
    print("=" * 80)

    result = subprocess.run([
        sys.executable,
        "spotify_playlist_export.py",
        PLAYLIST_URL,
        "--output",
        str(SPOTIFY_FILE),
    ])

    if result.returncode != 0:
        raise SystemExit(
            "Spotify playlist export failed."
        )

    state["playlist_exported"] = True
    save_state(state)

else:
    print(
        "Spotify playlist already exported; "
        "reusing existing file."
    )


spotify = pd.read_csv(SPOTIFY_FILE)

print(
    "\nPlaylist tracks:",
    len(spotify)
)


# ============================================================
# STEP 2 — LOAD LOCAL WHOSAMPLED CACHE
# ============================================================

cache_file = Path(
    "kanye_whosampled_track_index.csv"
)

if cache_file.exists():

    cache = pd.read_csv(
        cache_file
    )

else:

    cache = pd.DataFrame(
        columns=[
            "track_artist",
            "track_title",
            "whosampled_track_url",
        ]
    )

if BLIND_PHASE1_CACHE:

    cache = pd.DataFrame(
        columns=[
            "track_artist",
            "track_title",
            "whosampled_track_url",
        ]
    )

    print(
        "Local WhoSampled cache: IGNORED FOR PHASE 1"
    )

else:

    print(
        "Local WhoSampled cache:",
        len(cache)
    )



def verify_loaded_page(page, expected_title, expected_artists):
    """
    Verify the WhoSampled page that is already loaded.

    This function NEVER calls page.goto().
    """

    from bs4 import BeautifulSoup

    try:
        html = page.content()

        soup = BeautifulSoup(
            html,
            "html.parser"
        )

        source = extract_source_metadata(
            soup
        )

        actual_title = normalize(
            source.get(
                "source_title",
                ""
            )
        )

        actual_artists = [
            normalize(a)
            for a in str(
                source.get(
                    "source_artists",
                    ""
                )
            ).split(",")
            if str(a).strip()
        ]

        expected_title_norm = normalize(
            expected_title
        )

        expected_artist_norms = [
            normalize(a)
            for a in expected_artists
            if str(a).strip()
        ]

        title_match = (
            actual_title == expected_title_norm
        )

        artist_match = any(
            ea == aa
            or ea in aa
            or aa in ea
            for ea in expected_artist_norms
            for aa in actual_artists
        )

        canonical_url = source.get(
            "source_url"
        )

        if not canonical_url:
            return {
                "status": "review",
                "url": "",
                "source_title": source.get(
                    "source_title"
                ),
                "source_artists": source.get(
                    "source_artists"
                ),
            }

        # Reject obvious non-track relationship URLs.
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
            return {
                "status": "rejected",
                "url": canonical_url,
                "source_title": source.get(
                    "source_title"
                ),
                "source_artists": source.get(
                    "source_artists"
                ),
            }

        if title_match and artist_match:
            status = "matched"
        else:
            status = "review"

        return {
            "status": status,
            "url": canonical_url,
            "source_title": source.get(
                "source_title"
            ),
            "source_artists": source.get(
                "source_artists"
            ),
        }

    except Exception as e:

        print(
            "verify_loaded_page error:",
            repr(e)
        )

        return {
            "status": "review",
            "url": "",
            "source_title": "",
            "source_artists": "",
        }


# ============================================================
# STEP 3 — SPOTIFY → WHOSAMPLED
# ============================================================

if BLIND_PHASE1_CACHE:

    processed = set()
    matched_rows = []

    print(
        "BLIND PHASE 1:"
    )

    print(
        "  Existing track checkpoint: IGNORED"
    )

    print(
        "  Starting with 0 processed tracks"
    )

else:

    processed = set(
        state.get(
            "tracks_processed",
            []
        )
    )

    # Preserve previously completed Phase 1 results when resuming a
    # checkpointed run. Otherwise a run where every track is skipped
    # would overwrite matched_tracks.csv with an empty file.
    matched_rows = []

    if MATCH_FILE.exists():

        try:

            existing_matches_df = pd.read_csv(
                MATCH_FILE
            )

            if not existing_matches_df.empty:

                restored_rows = (
                    existing_matches_df
                    .to_dict("records")
                )

                # Historical checkpoints may contain more than one row
                # for the same Spotify track when a later decision
                # superseded an earlier unresolved/review result.
                # Treat spotify_track_id as the checkpoint identity and
                # keep the last assertion for each track.
                restored_by_spotify_id = {}
                restored_without_spotify_id = []

                for restored_row in restored_rows:

                    restored_spotify_id = str(
                        restored_row.get(
                            "spotify_track_id",
                            ""
                        )
                        or ""
                    ).strip()

                    if restored_spotify_id:
                        restored_by_spotify_id[
                            restored_spotify_id
                        ] = restored_row
                    else:
                        restored_without_spotify_id.append(
                            restored_row
                        )

                matched_rows = (
                    list(restored_by_spotify_id.values())
                    + restored_without_spotify_id
                )

                duplicate_count = (
                    len(restored_rows)
                    - len(matched_rows)
                )

                print(
                    "RESTORED MATCH CHECKPOINT:",
                    len(matched_rows),
                    "unique track results"
                )

                if duplicate_count:
                    print(
                        "COLLAPSED HISTORICAL MATCH CHECKPOINT DUPLICATES:",
                        duplicate_count
                    )

        except Exception as e:

            print(
                "MATCH CHECKPOINT RESTORE WARNING:",
                repr(e)
            )



def upsert_matched_row(new_row):
    """Store exactly one current checkpoint assertion per Spotify track."""

    spotify_track_id = str(
        new_row.get(
            "spotify_track_id",
            ""
        )
        or ""
    ).strip()

    if not spotify_track_id:
        matched_rows.append(
            new_row
        )
        return

    for index, existing_row in enumerate(
        matched_rows
    ):

        existing_spotify_track_id = str(
            existing_row.get(
                "spotify_track_id",
                ""
            )
            or ""
        ).strip()

        if existing_spotify_track_id != spotify_track_id:
            continue

        # Preserve fields added by later review/canonical-identity
        # phases while replacing the current Phase-1 assertion fields.
        merged_row = dict(
            existing_row
        )
        merged_row.update(
            new_row
        )
        matched_rows[index] = merged_row
        return

    matched_rows.append(
        new_row
    )


def is_whosampled_artist_profile(url):
    """
    True for /Artist/ and false for /Artist/Track/.
    """

    if not url:
        return False

    path = (
        str(url)
        .split(
            "whosampled.com",
            1
        )[-1]
        .strip("/")
    )

    parts = [
        part
        for part in path.split("/")
        if part
    ]

    return len(parts) == 1


def verify_cached_html_file(
    html_file,
    expected_title,
    expected_artists,
):
    """
    Verify cached WhoSampled HTML without making a network request.
    """

    from bs4 import BeautifulSoup

    try:

        soup = BeautifulSoup(
            Path(html_file).read_text(
                encoding="utf-8",
                errors="ignore"
            ),
            "html.parser"
        )

        source = extract_source_metadata(
            soup
        )

        source_url = (
            source.get(
                "source_url",
                ""
            )
            or ""
        )

        if not source_url:
            return {
                "status": "rejected",
                "url": "",
                "source_title": "",
                "source_artists": "",
            }

        path = (
            source_url
            .split(
                "whosampled.com",
                1
            )[-1]
            .strip("/")
        )

        parts = [
            part
            for part in path.split("/")
            if part
        ]

        if len(parts) != 2:
            return {
                "status": "rejected",
                "url": source_url,
                "source_title": source.get(
                    "source_title",
                    ""
                ),
                "source_artists": source.get(
                    "source_artists",
                    ""
                ),
            }

        actual_title = normalize(
            source.get(
                "source_title",
                ""
            )
        )

        actual_artists = [
            normalize(a)
            for a in str(
                source.get(
                    "source_artists",
                    ""
                )
            ).split(",")
            if str(a).strip()
        ]

        expected_title = normalize(
            expected_title
        )

        expected_artists = [
            normalize(a)
            for a in expected_artists
            if str(a).strip()
        ]

        title_match = (
            actual_title
            == expected_title
        )

        artist_match = any(
            expected_artist == actual_artist
            or expected_artist in actual_artist
            or actual_artist in expected_artist
            for expected_artist in expected_artists
            for actual_artist in actual_artists
        )

        return {
            "status": (
                "matched"
                if title_match and artist_match
                else "review"
            ),
            "url": source_url,
            "source_title": source.get(
                "source_title",
                ""
            ),
            "source_artists": source.get(
                "source_artists",
                ""
            ),
        }

    except Exception as e:

        print(
            "CACHED HTML VERIFICATION ERROR:",
            html_file,
            repr(e)
        )

        return {
            "status": "rejected",
            "url": "",
            "source_title": "",
            "source_artists": "",
        }


def find_local_page(title, artists):
    """
    Locate a potentially relevant archived page.
    Actual acceptance happens through verification.
    """

    from bs4 import BeautifulSoup

    archive_dir = Path(
        "whosampled_html_archive"
    )

    if not archive_dir.exists():
        return None, None

    target_title = normalize(
        title
    )

    target_artists = [
        normalize(a)
        for a in artists
        if str(a).strip()
    ]

    for html_file in archive_dir.glob(
        "*.html"
    ):

        try:

            soup = BeautifulSoup(
                html_file.read_text(
                    encoding="utf-8",
                    errors="ignore"
                ),
                "html.parser"
            )

            source = extract_source_metadata(
                soup
            )

            saved_title = normalize(
                source.get(
                    "source_title",
                    ""
                )
            )

            if saved_title != target_title:
                continue

            saved_artists = [
                normalize(a)
                for a in str(
                    source.get(
                        "source_artists",
                        ""
                    )
                ).split(",")
                if str(a).strip()
            ]

            if any(
                target_artist == saved_artist
                or target_artist in saved_artist
                or saved_artist in target_artist
                for target_artist in target_artists
                for saved_artist in saved_artists
            ):

                return (
                    html_file,
                    source.get(
                        "source_url"
                    )
                )

        except Exception as e:

            print(
                "LOCAL ARCHIVE ERROR:",
                html_file,
                repr(e)
            )

    return None, None


def learn_artist_slug(
    spotify_artists,
    verified_url,
    source_artists,
    cache,
):
    """
    Learn a canonical WhoSampled artist slug only when exactly
    one Spotify artist corresponds to the WhoSampled artist.
    """

    if not verified_url:
        return

    path = (
        str(verified_url)
        .split(
            "whosampled.com",
            1
        )[-1]
        .strip("/")
    )

    parts = [
        part
        for part in path.split("/")
        if part
    ]

    if len(parts) != 2:
        return

    # Store the canonical WhoSampled slug in its raw Unicode form.
    # Example:
    #   /L%C3%B4-Borges/  ->  Lô-Borges
    canonical_slug = unquote(
        parts[0]
    )

    ws_artists = [
        normalize(a)
        for a in str(
            source_artists
            or ""
        ).split(",")
        if str(a).strip()
    ]

    if not ws_artists:
        print(
            "SLUG LEARNING SKIPPED:",
            "WhoSampled source artist metadata unavailable"
        )
        return

    matches = []

    for spotify_artist in spotify_artists:

        spotify_norm = normalize(
            spotify_artist
        )

        if any(
            spotify_norm == ws_artist
            or spotify_norm in ws_artist
            or ws_artist in spotify_norm
            for ws_artist in ws_artists
        ):

            matches.append(
                spotify_artist
            )

    if len(matches) != 1:

        print(
            "SLUG LEARNING SKIPPED:",
            "WhoSampled artist did not map uniquely",
            source_artists,
            "←",
            spotify_artists
        )

        return

    artist = matches[0]
    key = normalize(artist)

    current = cache.get(
        key,
        []
    )

    if isinstance(
        current,
        str
    ):
        current = [current]

    current = [
        str(x)
        for x in current
        if str(x).strip()
    ]

    updated = [canonical_slug]

    for slug in current:

        if normalize(slug) != normalize(
            canonical_slug
        ):

            updated.append(slug)

    if updated != current:

        cache[key] = updated

        save_artist_slug_cache(
            cache
        )

        print(
            "LEARNED WHO SAMPLED ARTIST SLUG:",
            artist,
            "→",
            canonical_slug
        )


def title_urls_for_slug(
    artist_slug,
    title,
):
    """
    Generate several plausible learned-slug title candidates.

    WhoSampled's canonical title slugs are not reliably reproducible
    with one universal punctuation rule, so candidate generation is
    deliberately inclusive. Actual acceptance is still determined by
    page-content verification.

    Order:
        1. Existing Unicode slugify candidate
        2. Unicode punctuation-preserving candidate
        3. Existing ASCII slugify candidate
        4. ASCII punctuation-preserving candidate
    """

    import re
    import unicodedata
    from urllib.parse import quote

    from whosampled_match import (
        slugify,
        ascii_slugify,
    )

    artist_slug = unquote(
        str(artist_slug)
    ).strip("/")

    title = str(title).strip()

    candidates = []
    seen = set()

    def add_candidate(title_slug, label):

        if not title_slug:
            return

        url = (
            "https://www.whosampled.com/"
            + artist_slug
            + "/"
            + quote(
                title_slug,
                safe="-"
            )
            + "/"
        )

        if url not in seen:

            seen.add(url)

            candidates.append({
                "label": label,
                "url": url,
            })

    # 1. Existing Unicode slugifier.
    add_candidate(
        slugify(title),
        "unicode_slugify"
    )

    # 2. Preserve Unicode punctuation while converting spaces
    #    to hyphens.
    unicode_punctuation = unicodedata.normalize(
        "NFC",
        title
    )

    unicode_punctuation = re.sub(
        r"\s+",
        "-",
        unicode_punctuation
    )

    unicode_punctuation = re.sub(
        r"-+",
        "-",
        unicode_punctuation
    ).strip("-")

    add_candidate(
        unicode_punctuation,
        "unicode_punctuation_preserved"
    )

    # 3. Existing ASCII slugifier.
    add_candidate(
        ascii_slugify(title),
        "ascii_slugify"
    )

    # 4. Preserve ASCII punctuation while transliterating
    #    accented characters.
    ascii_punctuation = (
        unicodedata.normalize(
            "NFKD",
            title
        )
        .encode(
            "ascii",
            "ignore"
        )
        .decode(
            "ascii"
        )
    )

    ascii_punctuation = re.sub(
        r"\s+",
        "-",
        ascii_punctuation
    )

    ascii_punctuation = re.sub(
        r"-+",
        "-",
        ascii_punctuation
    ).strip("-")

    add_candidate(
        ascii_punctuation,
        "ascii_punctuation_preserved"
    )

    print(
        "TITLE URL CANDIDATES:",
        title
    )

    for item in candidates:
        print(
            "  ",
            item["label"],
            "→",
            item["url"]
        )

    return [
        item["url"]
        for item in candidates
    ]



def discover_track_url_from_artist_page(
    page,
    artist_slug,
    title,
):
    """
    Ask the known WhoSampled artist page for the exact canonical
    track URL instead of assuming a universal title-slug rule.

    Returns:
        canonical track URL or None
    """

    from bs4 import BeautifulSoup
    from urllib.parse import urljoin

    artist_slug = unquote(
        str(artist_slug)
    ).strip("/")

    if not artist_slug:
        return None

    artist_url = (
        "https://www.whosampled.com/"
        + artist_slug
        + "/"
    )

    print()
    print(
        "ARTIST PAGE DISCOVERY:",
        artist_url
    )

    try:

        response = page.goto(
            artist_url,
            wait_until="domcontentloaded",
            timeout=60000
        )

    except Exception as e:

        print(
            "ARTIST PAGE DISCOVERY ERROR:",
            repr(e)
        )

        return None

    status = (
        response.status
        if response
        else None
    )

    print(
        "ARTIST PAGE DISCOVERY STATUS:",
        status
    )

    if status == 429:

        state_message = (
            "Stopped safely on HTTP 429 "
            "while discovering artist track URL."
        )

        raise SystemExit(
            state_message
        )

    print(
        "Waiting 12 seconds "
        "before the next WhoSampled request..."
    )

    time.sleep(12)

    if status != 200:
        return None

    soup = BeautifulSoup(
        page.content(),
        "html.parser"
    )

    target_title = normalize(
        title
    )

    exact_matches = []
    fallback_matches = []

    for link in soup.select(
        "a[href]"
    ):

        href = (
            link.get(
                "href",
                ""
            )
            or ""
        ).strip()

        if not href:
            continue

        absolute_url = urljoin(
            artist_url,
            href
        )

        if not absolute_url.startswith(
            "https://www.whosampled.com/"
        ):
            continue

        if not is_canonical_track_url(
            absolute_url
        ):
            continue

        link_text = normalize(
            link.get_text(
                " ",
                strip=True
            )
        )

        if not link_text:
            continue

        if link_text == target_title:

            exact_matches.append(
                absolute_url
            )

        elif (
            target_title in link_text
            or link_text in target_title
        ):

            fallback_matches.append(
                absolute_url
            )

    # Exact title match is preferred.
    if exact_matches:

        url = exact_matches[0]

        print(
            "CANONICAL TRACK LINK DISCOVERED:",
            url
        )

        return url

    # Then allow a conservative normalized containment match.
    if fallback_matches:

        url = fallback_matches[0]

        print(
            "POTENTIAL TRACK LINK DISCOVERED:",
            url
        )

        return url

    print(
        "ARTIST PAGE DID NOT CONTAIN TRACK:",
        title
    )

    return None


def save_verified_track_html(
    page,
    title,
):
    """
    Save a verified/reviewed WhoSampled page to:

        1. the current run's HTML archive
        2. the persistent HTML archive
        3. the current run's local artwork cache
        4. the persistent artwork cache

    Artwork capture is opportunistic and uses the already-loaded
    Playwright page. It does not issue a second WhoSampled request.
    """

    html = page.content()

    filename = (
        safe_filename(
            title
        )
        + ".html"
    )

    # --------------------------------------------------------
    # HTML ARCHIVES
    # --------------------------------------------------------

    run_html = (
        HTML_DIR / filename
    )

    archive_dir = Path(
        "whosampled_html_archive"
    )

    archive_dir.mkdir(
        exist_ok=True
    )

    archive_html = (
        archive_dir / filename
    )

    run_html.write_text(
        html,
        encoding="utf-8"
    )

    archive_html.write_text(
        html,
        encoding="utf-8"
    )

    print(
        "HTML SAVED:",
        run_html
    )

    # --------------------------------------------------------
    # ARTWORK ARCHIVE
    # --------------------------------------------------------
    #
    # This uses the existing Playwright page. The helper tries
    # to capture the rendered image rather than asking the
    # browser later to load the remote WhoSampled image URL.
    # --------------------------------------------------------

    run_media_dir = (
        HTML_DIR.parent
        / "whosampled_media"
    )

    persistent_media_dir = Path(
        "whosampled_media"
    )

    run_media_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    persistent_media_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    try:

        artwork = capture_rendered_artwork(
            page=page,
            title=title,
            output_dir=run_media_dir,
        )

    except Exception as exc:

        print(
            "WHO SAMPLED ARTWORK CAPTURE ERROR:",
            repr(exc)
        )

        artwork = {
            "whosampled_thumbnail_url": "",
            "whosampled_thumbnail_path": "",
            "whosampled_thumbnail_status":
                "unavailable",
        }

    run_thumbnail = (
        artwork.get(
            "whosampled_thumbnail_path",
            ""
        )
    )

    if run_thumbnail:

        run_thumbnail_path = Path(
            run_thumbnail
        )

        persistent_thumbnail = (
            persistent_media_dir
            / run_thumbnail_path.name
        )

        try:

            persistent_thumbnail.write_bytes(
                run_thumbnail_path.read_bytes()
            )

            print(
                "WHO SAMPLED ARTWORK SAVED:",
                run_thumbnail_path
            )

            print(
                "WHO SAMPLED ARTWORK ARCHIVED:",
                persistent_thumbnail
            )

            artwork[
                "persistent_thumbnail_path"
            ] = str(
                persistent_thumbnail
            )

        except Exception as exc:

            print(
                "WHO SAMPLED ARTWORK ARCHIVE ERROR:",
                repr(exc)
            )

            artwork[
                "persistent_thumbnail_path"
            ] = ""

    else:

        print(
            "WHO SAMPLED ARTWORK:",
            "unavailable"
        )

        artwork[
            "persistent_thumbnail_path"
        ] = ""

    return {
        "html_path":
            str(run_html),

        "persistent_html_path":
            str(archive_html),

        "thumbnail_path":
            artwork.get(
                "whosampled_thumbnail_path",
                "",
            ),

        "persistent_thumbnail_path":
            artwork.get(
                "persistent_thumbnail_path",
                "",
            ),

        "thumbnail_url":
            artwork.get(
                "whosampled_thumbnail_url",
                "",
            ),

        "thumbnail_status":
            artwork.get(
                "whosampled_thumbnail_status",
                "unavailable",
            ),
    }


def request_whosampled_candidate(
    page,
    candidate_urls,
    title,
    expected_artists,
    source,
    state,
    processed,
    matched_rows,
):
    """
    Try URL variants for ONE artist identity.

    artist profile = stop this identity
    review track = retain and stop this identity
    matched = accept
    404 = continue
    """

    artist_profile_result = None

    for candidate_url in candidate_urls:

        print()
        print(
            "WHO SAMPLED REQUEST:",
            candidate_url
        )

        print(
            "RESOLUTION SOURCE:",
            source
        )

        try:

            response = page.goto(
                candidate_url,
                wait_until="domcontentloaded",
                timeout=60000
            )

        except Exception as e:

            print(
                "REQUEST ERROR:",
                repr(e)
            )

            continue

        status = (
            response.status
            if response
            else None
        )

        print(
            "STATUS:",
            status
        )

        if status == 429:

            print(
                "\nWHO SAMPLED RATE LIMITED."
            )

            state[
                "stopped_on_429"
            ] = True

            state[
                "tracks_processed"
            ] = sorted(
                processed
            )

            save_state(
                state
            )

            # IMPORTANT:
            # Preserve the last valid matched_tracks.csv checkpoint.
            # On a rate limit, matched_rows may contain only the
            # current in-memory batch (or nothing at all). Writing it
            # here can destroy a previously valid checkpoint.
            #
            # The state checkpoint above is still saved, and the
            # existing MATCH_FILE is intentionally left untouched.

            if MATCH_FILE.exists():
                print(
                    "Existing matched_tracks.csv preserved after 429."
                )
            else:
                print(
                    "No existing matched_tracks.csv checkpoint "
                    "available after 429."
                )

            raise SystemExit(
                "Stopped safely on HTTP 429."
            )

        print(
            "Waiting 12 seconds "
            "before the next WhoSampled request..."
        )

        time.sleep(12)

        if status != 200:
            continue

        try:

            verified = verify_loaded_page(
                page,
                title,
                expected_artists
            )

        except Exception as e:

            print(
                "Verification error:",
                repr(e)
            )

            verified = {
                "status": "rejected",
                "url": "",
                "source_title": "",
                "source_artists": "",
            }

        verified_status = verified.get(
            "status",
            "rejected"
        )

        verified_url = (
            verified.get(
                "url"
            )
            or ""
        )

        print(
            "VERIFICATION:",
            verified_status,
            verified_url
        )

        print(
            "BROWSER FINAL URL:",
            page.url
        )

        if verified_status == "matched":

            # Archive every automatically verified primary track page.
            #
            # The Playwright page is already loaded and verified here,
            # so this performs no additional WhoSampled request.
            # Step 4 depends on these saved primary HTML files when
            # producing normalized recordings and relationships.
            save_verified_track_html(
                page,
                title
            )

            return (
                "matched",
                verified
            )

        if (
            verified_status != "rejected"
            and verified_url
            and is_whosampled_artist_profile(
                verified_url
            )
        ):

            print(
                "WHO SAMPLED ARTIST PROFILE ONLY:",
                verified_url
            )

            # This candidate landed on the known artist profile,
            # but there may be another title candidate that points
            # to the actual track page. Remember the artist identity
            # result, then continue through the remaining candidates.
            artist_profile_result = (
                "artist_profile",
                verified
            )

            continue

        if verified_status == "review":

            print(
                "TRACK REVIEW CANDIDATE:",
                verified_url or "(canonical URL unavailable)"
            )

            return (
                "review",
                verified
            )

        print(
            "PAGE REJECTED:",
            verified_url or "(canonical URL unavailable)"
        )

    if artist_profile_result is not None:

        return artist_profile_result

    return (
        "not_found",
        None
    )


with sync_playwright() as p:

    browser = p.chromium.launch(
        headless=False
    )

    context = browser.new_context(
        viewport={
            "width": 1440,
            "height": 900
        }
    )

    page = context.new_page()

    for _, row in spotify.iterrows():

        spotify_id = str(
            row["spotify_track_id"]
        )

        if spotify_id in processed:

            print(
                "SKIPPED TRACK:",
                row["title"],
                "— already processed in checkpoint."
            )

            continue

        title = str(
            row["title"]
        ).strip()

        artists = artist_list(
            row["artist_names"],
            row.get("artists_json", "")
        )

        print()
        print(
            "=" * 80
        )

        print(
            "TRACK:",
            title,
            "—",
            row["artist_names"]
        )

        print(
            "=" * 80
        )

        ws_url = None
        match_method = None
        match_status = "unresolved"
        matched_source_artists = ""

        # ----------------------------------------------------
        # 1. STRUCTURED TRACK CACHE
        # ----------------------------------------------------

        title_norm = normalize(
            title
        )

        candidates = cache[
            cache["track_title"]
            .fillna("")
            .map(normalize)
            == title_norm
        ].copy()

        if (
            BLIND_PHASE1_CACHE
            and not candidates.empty
        ):

            raise RuntimeError(
                "Blind Phase 1 mode unexpectedly found "
                "structured cache candidates."
            )

        if not candidates.empty:

            print(
                "STRUCTURED CACHE CANDIDATES:",
                len(candidates)
            )

            artist_norms = [
                normalize(a)
                for a in artists
            ]

            cached_url = None

            for _, candidate in candidates.iterrows():

                ws_artist = normalize(
                    candidate["track_artist"]
                )

                if any(
                    ws_artist in artist
                    or artist in ws_artist
                    for artist in artist_norms
                ):

                    cached_url = candidate[
                        "whosampled_track_url"
                    ]

                    break

            if cached_url is None:

                cached_url = candidates.iloc[0][
                    "whosampled_track_url"
                ]

            cached_file = None

            archive_dir = Path(
                "whosampled_html_archive"
            )

            if archive_dir.exists():

                from bs4 import BeautifulSoup

                for candidate_file in (
                    archive_dir.glob("*.html")
                ):

                    try:

                        soup = BeautifulSoup(
                            candidate_file.read_text(
                                encoding="utf-8",
                                errors="ignore"
                            ),
                            "html.parser"
                        )

                        source = (
                            extract_source_metadata(
                                soup
                            )
                        )

                        if (
                            source.get(
                                "source_url"
                            )
                            == cached_url
                        ):

                            cached_file = (
                                candidate_file
                            )

                            break

                    except Exception:
                        continue

            if cached_file is not None:

                verified = (
                    verify_cached_html_file(
                        cached_file,
                        title,
                        artists
                    )
                )

                print(
                    "STRUCTURED CACHE VERIFICATION:",
                    verified.get(
                        "status"
                    ),
                    verified.get(
                        "url"
                    )
                )

                if verified.get(
                    "status"
                ) == "matched":

                    ws_url = verified.get(
                        "url"
                    )

                    matched_source_artists = (
                        verified.get(
                            "source_artists",
                            ""
                        )
                    )

                    match_method = (
                        "track_cache"
                    )

                    match_status = (
                        "matched"
                    )

                    print(
                        "SKIPPED WHO SAMPLED REQUEST:"
                    )

                    print(
                        "  SOURCE: structured track cache"
                    )

                    print(
                        "  REASON: archived HTML verified"
                    )

                    print(
                        "  URL:",
                        ws_url
                    )

                else:

                    print(
                        "STRUCTURED CACHE REJECTED:",
                        cached_url
                    )

            else:

                print(
                    "STRUCTURED CACHE HAS NO "
                    "VERIFIABLE HTML:",
                    cached_url
                )

        # ----------------------------------------------------
        # 2. PERSISTENT HTML ARCHIVE
        # ----------------------------------------------------

        if (
            ws_url is None
            and not BLIND_PHASE1_CACHE
        ):

            local_file, local_url = (
                find_local_page(
                    title,
                    artists
                )
            )

            if local_file is not None:

                verified = (
                    verify_cached_html_file(
                        local_file,
                        title,
                        artists
                    )
                )

                print(
                    "LOCAL HTML CACHE VERIFICATION:",
                    verified.get(
                        "status"
                    ),
                    verified.get(
                        "url"
                    )
                )

                if verified.get(
                    "status"
                ) == "matched":

                    ws_url = verified.get(
                        "url"
                    )

                    matched_source_artists = (
                        verified.get(
                            "source_artists",
                            ""
                        )
                    )

                    match_method = (
                        "html_cache"
                    )

                    match_status = (
                        "matched"
                    )

                    print(
                        "SKIPPED WHO SAMPLED REQUEST:"
                    )

                    print(
                        "  SOURCE: persistent HTML archive"
                    )

                    print(
                        "  REASON: cached page verified locally"
                    )

                    print(
                        "  FILE:",
                        local_file
                    )

                    print(
                        "  URL:",
                        ws_url
                    )

                    run_html = (
                        HTML_DIR
                        / local_file.name
                    )

                    run_html.write_text(
                        local_file.read_text(
                            encoding="utf-8",
                            errors="ignore"
                        ),
                        encoding="utf-8"
                    )

                else:

                    print(
                        "LOCAL HTML CACHE REJECTED:",
                        local_file
                    )

        # ----------------------------------------------------
        # 3. NETWORK RESOLUTION
        # ----------------------------------------------------

        if ws_url is None:

            print(
                "NO VERIFIED CACHE HIT."
            )

            print(
                "Proceeding to live WhoSampled resolution."
            )

            review_candidate = None

            for spotify_artist in artists:

                artist_key = normalize(
                    spotify_artist
                )

                # --------------------------------------------
                # A. LEARNED ARTIST SLUG → DIRECT TRACK URL
                #
                # The artist slug has already been verified from a
                # previous successful WhoSampled track match.
                #
                # Try the Unicode title candidate first.
                # Only after it fails do we try the ASCII title.
                #
                # Artist-page pagination is intentionally NOT done
                # here. That happens later in the separate artist
                # enrichment phase.
                # --------------------------------------------

                learned_slugs = (
                    artist_slug_cache.get(
                        artist_key,
                        []
                    )
                )

                if isinstance(
                    learned_slugs,
                    str
                ):

                    learned_slugs = [
                        learned_slugs
                    ]

                learned_slugs = [
                    str(slug)
                    for slug in learned_slugs
                    if str(slug).strip()
                ]

                if learned_slugs:

                    print(
                        "ARTIST SLUG CACHE HIT:",
                        spotify_artist,
                        "→",
                        learned_slugs
                    )

                    for learned_slug in learned_slugs:

                        print(
                            "LEARNED SLUG DIRECT RESOLUTION:",
                            learned_slug
                        )

                        result_status, verified = (
                            request_whosampled_candidate(
                                page,
                                title_urls_for_slug(
                                    learned_slug,
                                    title
                                ),
                                title,
                                artists,
                                "learned_slug",
                                state,
                                processed,
                                matched_rows,
                            )
                        )

                        if result_status == "matched":

                            ws_url = verified.get(
                                "url"
                            ) or page.url

                            matched_source_artists = (
                                verified.get(
                                    "source_artists",
                                    ""
                                )
                            )

                            match_method = (
                                "learned_slug"
                            )

                            match_status = (
                                "matched"
                            )

                            break

                        if result_status == "review":

                            if (
                                review_candidate
                                is None
                            ):

                                review_candidate = (
                                    verified
                                )

                            break

                        if (
                            result_status
                            == "artist_profile"
                        ):

                            print(
                                "LEARNED SLUG RETURNED ARTIST PROFILE:",
                                learned_slug
                            )

                            # The artist identity is already known.
                            # Do not search Wikidata aliases for it.
                            # Continue to the next learned slug, if any.
                            continue

                    if ws_url is not None:
                        break

                    if review_candidate is not None:
                        break

                    # A learned slug is authoritative for this artist.
                    # Do not fall back to the same artist's Spotify name
                    # or Wikidata aliases during this pass.
                    continue

                # --------------------------------------------
                # B. SPOTIFY ARTIST NAME
                # --------------------------------------------

                result_status, verified = (
                    request_whosampled_candidate(
                        page,
                        canonical_url_variants(
                            spotify_artist,
                            title
                        ),
                        title,
                        artists,
                        "spotify_name",
                        state,
                        processed,
                        matched_rows,
                    )
                )

                if result_status == "matched":

                    ws_url = verified.get(
                        "url"
                    ) or page.url

                    matched_source_artists = (
                        verified.get(
                            "source_artists",
                            ""
                        )
                    )

                    match_method = "direct"
                    match_status = "matched"

                    break

                if result_status == "review":

                    if (
                        review_candidate
                        is None
                    ):

                        review_candidate = (
                            verified
                        )

                    break

                if (
                    result_status
                    == "artist_profile"
                ):

                    # WhoSampled already recognizes the artist.
                    # Do not try Wikidata aliases for this identity.
                    continue

                # --------------------------------------------
                # C. WIKIDATA
                #
                # Canonical first.
                # If canonical completely fails, top 3 aliases.
                # --------------------------------------------

                try:

                    wd = (
                        wikidata_artist_resolution(
                            spotify_artist,
                            wikidata_cache
                        )
                    )

                except Exception as e:

                    print(
                        "WIKIDATA ERROR:",
                        repr(e)
                    )

                    wd = None

                if not wd:

                    continue

                print(
                    "WIKIDATA RESULT:",
                    wd.get(
                        "status"
                    ),
                    wd.get(
                        "label"
                    )
                )

                if (
                    wd.get(
                        "status"
                    )
                    != "resolved"
                ):

                    continue

                canonical_name = str(
                    wd.get(
                        "label",
                        ""
                    )
                    or ""
                ).strip()

                wikidata_candidates = []

                if canonical_name:

                    wikidata_candidates.append(
                        (
                            canonical_name,
                            "wikidata_canonical"
                        )
                    )

                # Only top 3 aliases.
                seen = {
                    normalize(
                        canonical_name
                    )
                }

                alias_count = 0

                for alias in wd.get(
                    "aliases",
                    []
                ):

                    if alias_count >= 3:
                        break

                    alias = str(
                        alias
                        or ""
                    ).strip()

                    alias_key = normalize(
                        alias
                    )

                    if (
                        not alias_key
                        or alias_key in seen
                    ):
                        continue

                    seen.add(
                        alias_key
                    )

                    wikidata_candidates.append(
                        (
                            alias,
                            "wikidata_alias"
                        )
                    )

                    alias_count += 1

                for (
                    candidate_name,
                    candidate_source,
                ) in wikidata_candidates:

                    print(
                        "WIKIDATA SLUG ATTEMPT:",
                        candidate_name,
                        "(",
                        candidate_source,
                        ")"
                    )

                    result_status, verified = (
                        request_whosampled_candidate(
                            page,
                            canonical_url_variants(
                                candidate_name,
                                title
                            ),
                            title,
                            artists,
                            candidate_source,
                            state,
                            processed,
                            matched_rows,
                        )
                    )

                    if result_status == "matched":

                        ws_url = verified.get(
                            "url"
                        ) or page.url

                        matched_source_artists = (
                            verified.get(
                                "source_artists",
                                ""
                            )
                        )

                        match_method = "wikidata"
                        match_status = "matched"

                        break

                    if result_status == "review":

                        review_candidate = (
                            review_candidate
                            or verified
                        )

                        break

                    if (
                        result_status
                        == "artist_profile"
                    ):

                        # Canonical WhoSampled artist identity has
                        # been established. Stop alias expansion.
                        break

                if ws_url is not None:
                    break

            # ------------------------------------------------
            # Final result for unresolved/review.
            # ------------------------------------------------

            if (
                ws_url is None
                and review_candidate is not None
            ):

                ws_url = review_candidate.get(
                    "url",
                    ""
                )

                matched_source_artists = (
                    review_candidate.get(
                        "source_artists",
                        ""
                    )
                )

                match_method = "review"
                match_status = "review"

                print(
                    "PAGE SAVED FOR REVIEW:",
                    ws_url
                )

                save_verified_track_html(
                    page,
                    title
                )

            elif ws_url is None:

                match_status = "unresolved"

                print(
                    "UNRESOLVED AFTER DIRECT + WIKIDATA RESOLUTION"
                )

        # ----------------------------------------------------
        # Learn slug ONCE.
        # ----------------------------------------------------

        if (
            ws_url
            and match_status == "matched"
        ):

            learn_artist_slug(
                artists,
                ws_url,
                matched_source_artists,
                artist_slug_cache
            )

        # ----------------------------------------------------
        # Save track-level checkpoint.
        # ----------------------------------------------------

        upsert_matched_row({
            "spotify_track_id":
                row["spotify_track_id"],
            "isrc":
                row["isrc"],
            "spotify_title":
                title,
            "spotify_artists":
                row["artist_names"],
            "whosampled_url":
                ws_url,
            "match_method":
                match_method,
            "match_status":
                match_status,
        })

        processed.add(
            spotify_id
        )

        state[
            "tracks_processed"
        ] = sorted(
            processed
        )

        save_state(
            state
        )

    browser.close()


matched_df = pd.DataFrame(
    matched_rows
)

write_dataframe(
    matched_df,
    MATCH_FILE
)

print()
print(
    "WhoSampled track matching complete."
)

if not matched_df.empty:

    print(
        "Matched:",
        (
            matched_df["match_status"]
            == "matched"
        ).sum()
    )

    print(
        "Review:",
        (
            matched_df["match_status"]
            == "review"
        ).sum()
    )

    print(
        "Unresolved:",
        (
            matched_df["match_status"]
            == "unresolved"
        ).sum()
    )

# ============================================================
# ============================================================
# ============================================================
# WHO SAMPLED CONTRIBUTOR REVIEW GATE
# ============================================================
#
# Phase 1 candidates classified as "review" must be explicitly
# accepted, rejected, or left unresolved before Step 4 can treat
# their WhoSampled page as graph evidence.
#
# The review UI updates matched_tracks.csv. Step 4 then reads that
# updated file, so rejected and unresolved pages remain excluded.
# ============================================================

print()
print("=" * 80)
print("WHO SAMPLED CONTRIBUTOR REVIEW")
print("=" * 80)

matched_pre_review = pd.read_csv(
    MATCH_FILE
)

review_count = int(
    matched_pre_review["match_status"]
    .fillna("")
    .eq("review")
    .sum()
)

if review_count:

    print(
        "WhoSampled review candidates:",
        review_count,
    )

    matched_df = run_whosampled_review(
        MATCH_FILE,
        WHO_SAMPLED_REVIEW_FILE,
    )

    state[
        "whosampled_review_complete"
    ] = True

    save_state(
        state
    )

else:

    print(
        "No WhoSampled review candidates."
    )

print()

# STEP 4 — PARSE SAVED HTML + SECONDARY RELATIONSHIP PAGES
# ============================================================

print()
print("=" * 80)
print("STEP 4 — RELATIONSHIP EXTRACTION")
print("=" * 80)

from bs4 import BeautifulSoup

from parse_whosampled_track import (
    extract_source_metadata,
    extract_relationships,
    extract_secondary_pages,
)


def secondary_archive_file(url):
    """Return the persistent archive filename for a secondary URL."""
    from urllib.parse import urlparse

    parsed = urlparse(url)

    parts = [
        part
        for part in parsed.path.strip("/").split("/")
        if part
    ]

    if not parts:
        return None

    filename = "-".join(
        safe_filename(part)
        for part in parts
    ) + ".html"

    return Path(
        "whosampled_html_archive"
    ) / filename


def load_html_file(path):
    return BeautifulSoup(
        Path(path).read_text(
            encoding="utf-8",
            errors="ignore"
        ),
        "html.parser"
    )


def normalize_secondary_relationship(row, primary_source):
    """Preserve the original track as the relationship source."""
    result = dict(row)

    for key in (
        "source_title",
        "source_url",
        "source_artists",
        "source_producers",
        "source_album",
        "source_label",
        "source_release_year",
    ):
        result[key] = primary_source.get(
            key,
            ""
        )

    return result


all_relationship_rows = []

processed_secondary = set(
    state.get(
        "secondary_pages_processed",
        []
    )
)

primary_html_files = sorted(
    HTML_DIR.glob("*.html")
)

# ------------------------------------------------------------
# Step 4 only extracts relationships from Phase 1 tracks that
# were explicitly verified as MATCHED. Review/unresolved pages
# remain archived for human review but are not graph evidence.
# ------------------------------------------------------------
approved_match_urls = set()

if MATCH_FILE.exists():

    try:

        matched_source_df = pd.read_csv(
            MATCH_FILE
        )

        for _, match_row in matched_source_df.iterrows():

            if (
                str(
                    match_row.get(
                        "match_status",
                        ""
                    )
                ).strip().lower()
                == "matched"
            ):

                matched_url = str(
                    match_row.get(
                        "whosampled_url",
                        ""
                    )
                    or ""
                ).strip()

                if matched_url:
                    approved_match_urls.add(
                        matched_url.rstrip("/")
                    )

        print(
            "STEP 4 APPROVED MATCHED TRACKS:",
            len(approved_match_urls)
        )

    except Exception as e:

        raise SystemExit(
            "Could not read matched_tracks.csv for Step 4 filtering: "
            + repr(e)
        )

else:

    raise SystemExit(
        "matched_tracks.csv is required for Step 4 filtering."
    )

accepted_primary_html_files = []

# ------------------------------------------------------------
# STEP 4 owns its own Playwright browser.
# STEP 3 has already closed its browser by this point.
# ------------------------------------------------------------

secondary_playwright = None
secondary_browser = None
secondary_page = None

for html_file in primary_html_files:

    if html_file.name.startswith(
        "secondary_"
    ):
        continue

    print()
    print(
        "Parsing primary:",
        html_file.name
    )

    try:

        soup = load_html_file(
            html_file
        )

        primary_source = (
            extract_source_metadata(
                soup
            )
        )

        primary_source_url = str(
            primary_source.get(
                "source_url",
                ""
            )
            or ""
        ).strip().rstrip("/")

        if primary_source_url not in approved_match_urls:

            print(
                "  SKIPPED PRIMARY PAGE:"
            )

            print(
                "    REASON: Phase 1 status was not matched"
            )

            print(
                "    SOURCE URL:",
                primary_source_url
            )

            continue

        accepted_primary_html_files.append(
            html_file.name
        )

        print(
            "  APPROVED PRIMARY PAGE: Phase 1 matched"
        )

        primary_relationships = (
            extract_relationships(
                soup,
                primary_source
            )
        )

        print(
            "  Primary relationships:",
            len(primary_relationships)
        )

        all_relationship_rows.extend(
            primary_relationships
        )

        secondary_pages = (
            extract_secondary_pages(
                soup
            )
        )

        print(
            "  Secondary pages discovered:",
            len(secondary_pages)
        )

        for secondary in secondary_pages:

            secondary_url = secondary["url"]

            archive_file = (
                secondary_archive_file(
                    secondary_url
                )
            )

            secondary_file = None

            # -----------------------------------------------
            # Persistent secondary-page cache.
            # -----------------------------------------------

            if (
                archive_file
                and archive_file.exists()
            ):

                secondary_file = archive_file

                print(
                    "  SECONDARY CACHE HIT:",
                    secondary_file
                )

            # -----------------------------------------------

                # Materialize persistent cache into the current run.
                run_secondary = (
                    HTML_DIR
                    / (
                        "secondary_"
                        + archive_file.name
                    )
                )

                if not run_secondary.exists():
                    run_secondary.write_text(
                        archive_file.read_text(
                            encoding="utf-8",
                            errors="ignore"
                        ),
                        encoding="utf-8"
                    )
                    print(
                        "  SECONDARY RUN CACHE CREATED:",
                        run_secondary
                    )

            # Current run cache fallback.
            # -----------------------------------------------

            if secondary_file is None:

                for candidate in (
                    HTML_DIR.glob(
                        "secondary_*.html"
                    )
                ):

                    try:

                        candidate_soup = (
                            load_html_file(
                                candidate
                            )
                        )

                        candidate_source = (
                            extract_source_metadata(
                                candidate_soup
                            )
                        )

                        if (
                            candidate_source.get(
                                "source_url"
                            )
                            == secondary_url
                        ):

                            secondary_file = candidate
                            break

                    except Exception:
                        continue

            # -----------------------------------------------
            # Network acquisition only if uncached.
            # -----------------------------------------------

            if secondary_file is None:

                if secondary_url in processed_secondary:
                    continue

                print(
                    "  SECONDARY REQUEST:",
                    secondary_url
                )

                try:

                    if secondary_page is None:

                        secondary_playwright = (
                            sync_playwright().start()
                        )

                        secondary_browser = (
                            secondary_playwright.chromium.launch(
                                headless=False
                            )
                        )

                        secondary_page = (
                            secondary_browser.new_page()
                        )

                        print(
                            "  Started dedicated "
                            "secondary-page browser."
                        )

                    response = secondary_page.goto(
                        secondary_url,
                        wait_until="domcontentloaded",
                        timeout=60000
                    )

                except Exception as e:

                    print(
                        "  SECONDARY REQUEST ERROR:",
                        repr(e)
                    )
                    continue

                status = (
                    response.status
                    if response
                    else None
                )

                print(
                    "  SECONDARY STATUS:",
                    status
                )

                if status == 429:

                    state[
                        "stopped_on_429"
                    ] = True

                    save_state(state)

                    raise SystemExit(
                        "Stopped safely on HTTP 429."
                    )

                delay = 12

                print(
                    "  Waiting 12 seconds "
                    "before next WhoSampled request..."
                )

                time.sleep(
                    delay
                )

                if status != 200:
                    continue

                html = secondary_page.content()

                if archive_file is None:
                    continue

                archive_file.parent.mkdir(
                    parents=True,
                    exist_ok=True
                )

                archive_file.write_text(
                    html,
                    encoding="utf-8"
                )

                secondary_file = archive_file

                run_secondary = (
                    HTML_DIR
                    / (
                        "secondary_"
                        + archive_file.name
                    )
                )

                run_secondary.write_text(
                    html,
                    encoding="utf-8"
                )

                print(
                    "  SECONDARY HTML SAVED:",
                    archive_file
                )

            # -----------------------------------------------
            # Parse the secondary page locally.
            # -----------------------------------------------

            try:

                secondary_soup = (
                    load_html_file(
                        secondary_file
                    )
                )

                secondary_source = (
                    extract_source_metadata(
                        secondary_soup
                    )
                )

                secondary_relationships = (
                    extract_relationships(
                        secondary_soup,
                        secondary_source
                    )
                )

                print(
                    "  Secondary relationships:",
                    len(
                        secondary_relationships
                    )
                )

                for row in secondary_relationships:

                    all_relationship_rows.append(
                        normalize_secondary_relationship(
                            row,
                            primary_source
                        )
                    )

                processed_secondary.add(
                    secondary_url
                )

                state[
                    "secondary_pages_processed"
                ] = sorted(
                    processed_secondary
                )

                save_state(state)

            except Exception as e:

                print(
                    "  SECONDARY PARSE ERROR:",
                    repr(e)
                )


    except Exception as e:

        print(
            "PRIMARY PARSE ERROR:",
            html_file,
            repr(e)
        )



# ------------------------------------------------------------
# Close the dedicated Step-4 browser.
# ------------------------------------------------------------

if secondary_browser is not None:

    secondary_browser.close()

if secondary_playwright is not None:

    secondary_playwright.stop()


# ------------------------------------------------------------
# Deduplicate primary + secondary relationships.
# ------------------------------------------------------------

unique = {}

for row in all_relationship_rows:

    key = (
        row.get(
            "relationship_type",
            ""
        ),
        row.get(
            "related_track",
            ""
        ),
        row.get(
            "related_artist",
            ""
        ),
        row.get(
            "whosampled_relationship_url",
            ""
        ),
    )

    unique[key] = row


relationships_df = pd.DataFrame(
    list(unique.values())
)

# ============================================================
# NORMALIZED CANONICAL OUTPUTS
# ============================================================
#
# Spotify seed recordings/artists are the baseline identities.
# Approved WhoSampled pages enrich those identities, and the
# relationship rows are normalized against the same recording IDs.
# ============================================================

write_normalized_outputs(
    accepted_html_files=accepted_primary_html_files,
    matched_file=MATCH_FILE,
    spotify_file=SPOTIFY_FILE,
    relationship_rows=relationships_df.to_dict("records"),
)


print()
print("=" * 80)
print("RELATIONSHIP EXTRACTION COMPLETE")
print("=" * 80)
print(
    "Unique relationships:",
    len(relationships_df)
)

print(
    "Secondary pages processed:",
    len(processed_secondary)
)

state[
    "relationships_parsed"
] = sorted(
    accepted_primary_html_files
)

save_state(state)


if STOP_AFTER_STEP4:
    print()
    print("=" * 80)
    print("STOPPING AFTER STEP 4 AS REQUESTED")
    print("=" * 80)
    print("Normalized production outputs have been written.")
    print("Step 5 Spotify enrichment was not started.")
    raise SystemExit(0)


# STEP 5 — RELATED TRACK → SPOTIFY
# ============================================================

print("\n" + "=" * 80)
print("STEP 5 — SPOTIFY ENRICHMENT")
print("=" * 80)

if not relationships_df.empty:

    enriched_rows = []

    for _, rel in relationships_df.iterrows():

        result = resolve_track(
            title=rel["related_track"],
            artists=rel["related_artist"],
            year=rel["year"],
        )

        enriched_rows.append({

            **rel.to_dict(),

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

            "spotify_uri":
                result.get(
                    "spotify_uri"
                ),

            "spotify_url":
                result.get(
                    "spotify_url"
                ),

            "spotify_isrc":
                result.get(
                    "isrc"
                ),

            "spotify_title":
                result.get(
                    "title"
                ),

            "spotify_artist_names":
                result.get(
                    "artist_names"
                ),

            "spotify_artist_ids":
                result.get(
                    "artist_ids"
                ),

            "spotify_album_name":
                result.get(
                    "album_name"
                ),

            "spotify_album_id":
                result.get(
                    "album_id"
                ),

            "spotify_album_release_date":
                result.get(
                    "album_release_date"
                ),

            "spotify_album_release_precision":
                result.get(
                    "album_release_precision"
                ),

            "spotify_album_image_url":
                result.get(
                    "album_image_url"
                ),

            "spotify_album_label":
                result.get(
                    "album_label"
                ),

            "spotify_duration_ms":
                result.get(
                    "duration_ms"
                ),
        })

    enriched_df = pd.DataFrame(
        enriched_rows
    )

    write_dataframe(
        enriched_df,
        ENRICHED_FILE
    )

    state[
        "spotify_enriched"
    ] = True

    save_state(state)

    print(
        "Enriched relationships:",
        len(enriched_df)
    )

    # ========================================================
    # SPOTIFY CONTRIBUTOR REVIEW GATE
    # ========================================================
    #
    # Spotify candidates classified as "review" must be
    # explicitly accepted, rejected, or left unresolved.
    #
    # The review UI operates on the already-written
    # relationships_enriched.csv and persists every decision.
    # No Spotify matching is performed by the UI itself.
    # ========================================================

    spotify_review_count = int(
        enriched_df[
            "spotify_match_status"
        ]
        .fillna("")
        .eq("review")
        .sum()
    )

    print()
    print("=" * 80)
    print("SPOTIFY CONTRIBUTOR REVIEW")
    print("=" * 80)

    if spotify_review_count:

        print(
            "Spotify review candidates:",
            spotify_review_count,
        )

        enriched_df = run_spotify_candidate_review(
            ENRICHED_FILE,
            SPOTIFY_FILE,
        )

        state[
            "spotify_review_complete"
        ] = True

        save_state(
            state
        )

    else:

        print(
            "No Spotify review candidates."
        )

    # Fold final accepted/matched Spotify resolutions into the canonical
    # graph outputs only after the review gate has finalized decisions.
    integrate_step5_spotify_targets(
        enriched_df
    )

else:

    print(
        "No relationships available yet."
    )


# ============================================================
# FINAL
# ============================================================

print("\n" + "=" * 80)
print("PIPELINE FINISHED / CHECKPOINTED")
print("=" * 80)
print(
    "RUN DIRECTORY:",
    RUN_DIR
)
print(
    "Spotify:",
    SPOTIFY_FILE
)
print(
    "Matches:",
    MATCH_FILE
)
print(
    "HTML:",
    HTML_DIR
)
print(
    "Relationships:",
    RELATIONSHIP_FILE
)
print(
    "Recordings:",
    RECORDINGS_FILE
)
print(
    "Artists:",
    ARTISTS_FILE
)
print(
    "Credits:",
    CREDITS_FILE
)
print(
    "Enriched:",
    ENRICHED_FILE
)
print(
    "State:",
    STATE_FILE
)
