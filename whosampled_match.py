import re
import json
import random
import time
import pandas as pd

from pathlib import Path
from urllib.parse import quote
import urllib.parse
import urllib.request
from playwright.sync_api import sync_playwright

INPUT = Path("spotify_playlist_input.csv")
OUTPUT = Path("whosampled_match_ranked.csv")
CANDIDATE_OUTPUT = Path("whosampled_candidates.csv")


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


def artist_variants(artist_names):
    """
    Return individual artist names.

    Spotify stores collaborations as comma-separated names.
    We use the first several names for matching rather than
    requiring every featured artist to appear on WhoSampled.
    """
    artists = [
        a.strip()
        for a in str(artist_names).split(",")
        if a.strip()
    ]

    return artists[:5]


# ============================================================
# WHOSAMPLED TITLE CANDIDATE POLICY
# ============================================================
#
# Candidate construction deliberately separates:
#
#   1. recording-title variants
#   2. punctuation variants
#   3. Unicode / ASCII variants
#
# This prevents slug construction from silently changing
# recording identity.
#
# Example:
#
#   Cinco Minutos (5 Minutos)
#       -> full-title candidates
#       -> safe parenthetical-stripped candidate: Cinco Minutos
#
# but:
#
#   Song (Remix)
#       -> full-title candidates ONLY
#
# because "remix" changes recording identity.
# ============================================================


IDENTITY_CHANGING_TITLE_TERMS = {
    "remix",
    "remixed",
    "mix",
    "cover",
    "covered",
    "dub",
    "rework",
    "edit",
    "radio edit",
    "live",
    "acoustic",
    "instrumental",
    "demo",
    "karaoke",
    "mashup",
    "bootleg",
}


SAFE_EDITION_SUFFIX_TERMS = {
    "version",
    "remaster",
    "remastered",
    "digital remaster",
    "mono",
    "stereo",
}


def normalize_title_descriptor(text):
    import unicodedata

    text = unicodedata.normalize(
        "NFKD",
        str(text or "")
    )

    text = "".join(
        c
        for c in text
        if not unicodedata.combining(c)
    )

    text = text.casefold()

    text = re.sub(
        r"[^\w\s]+",
        " ",
        text,
        flags=re.UNICODE,
    )

    return " ".join(
        text.split()
    )


def descriptor_changes_identity(text):
    """
    Return True when a descriptor indicates a musically distinct
    recording/version that must NOT collapse automatically.
    """

    normalized = normalize_title_descriptor(
        text
    )

    if not normalized:
        return False

    for term in IDENTITY_CHANGING_TITLE_TERMS:

        term_norm = normalize_title_descriptor(
            term
        )

        if re.search(
            r"\b"
            + re.escape(term_norm)
            + r"\b",
            normalized,
        ):
            return True

    return False


def safe_parenthetical_base_title(title):
    """
    Remove parenthetical/bracketed material only when none of the
    removed descriptors contain identity-changing terminology.

    Examples:

        Cinco Minutos (5 Minutos)
            -> Cinco Minutos

        Song (Remix)
            -> unchanged

        Song [Live]
            -> unchanged
    """

    title = str(title or "").strip()

    if not title:
        return ""

    changed = False

    def replace_group(match):

        nonlocal changed

        content = (
            match.group(1)
            or match.group(2)
            or ""
        )

        if descriptor_changes_identity(
            content
        ):
            return match.group(0)

        changed = True
        return ""

    result = re.sub(
        r"\(([^)]*)\)|\[([^\]]*)\]",
        replace_group,
        title,
    )

    result = re.sub(
        r"\s+",
        " ",
        result,
    ).strip()

    # Clean separators left hanging after removal.
    result = re.sub(
        r"\s*[-–—]\s*$",
        "",
        result,
    ).strip()

    if (
        changed
        and result
        and result != title
    ):
        return result

    return ""


def safe_trailing_edition_base_title(title):
    """
    Generate a base candidate for conservative trailing edition
    descriptors.

    This supports cases such as:

        Lotus 72 D - Fast Version
            -> Lotus 72 D

        Song - 2004 Digital Remaster
            -> Song

    Identity-changing descriptors such as Remix, Cover, Dub,
    Rework, Edit, Live, Acoustic, Instrumental, Demo, etc. are
    never collapsed.
    """

    title = str(title or "").strip()

    if not title:
        return ""

    match = re.match(
        r"^(.*?)\s+[-–—]\s+(.+?)\s*$",
        title,
    )

    if not match:
        return ""

    base = match.group(1).strip()
    suffix = match.group(2).strip()

    if not base or not suffix:
        return ""

    if descriptor_changes_identity(
        suffix
    ):
        return ""

    normalized_suffix = (
        normalize_title_descriptor(
            suffix
        )
    )

    safe = False

    for term in SAFE_EDITION_SUFFIX_TERMS:

        term_norm = (
            normalize_title_descriptor(
                term
            )
        )

        if re.search(
            r"\b"
            + re.escape(term_norm)
            + r"\b",
            normalized_suffix,
        ):
            safe = True
            break

    if not safe:
        return ""

    return base


def title_text_variants(title):
    """
    Return recording-title candidates in preferred order.

    Full Spotify title always comes first.

    Safe alternate/base candidates are additive; the original title
    is never destroyed.
    """

    title = str(title or "").strip()

    variants = []

    def add(value, strategy):

        value = str(
            value or ""
        ).strip()

        if not value:
            return

        key = (
            value.casefold()
        )

        if any(
            existing["key"] == key
            for existing in variants
        ):
            return

        variants.append({
            "text": value,
            "strategy": strategy,
            "key": key,
        })

    add(
        title,
        "full_title",
    )

    add(
        safe_parenthetical_base_title(
            title
        ),
        "safe_parenthetical_base",
    )

    add(
        safe_trailing_edition_base_title(
            title
        ),
        "safe_edition_base",
    )

    # A safe parenthetical removal may expose a safe trailing edition
    # descriptor, so permit the combination as a final conservative
    # variant.
    parenthetical_base = (
        safe_parenthetical_base_title(
            title
        )
    )

    if parenthetical_base:

        add(
            safe_trailing_edition_base_title(
                parenthetical_base
            ),
            "safe_parenthetical_and_edition_base",
        )

    return variants


def punctuation_preserving_slugify(
    text,
    ascii_only=False,
):
    """
    Preserve meaningful title punctuation while converting spaces
    to WhoSampled-style hyphens.
    """

    import unicodedata

    text = str(text or "").strip()

    if not text:
        return ""

    text = unicodedata.normalize(
        "NFC",
        text,
    )

    text = text.replace("’", "'")
    text = text.replace("‘", "'")
    text = text.replace("–", "-")
    text = text.replace("—", "-")

    if ascii_only:

        text = (
            unicodedata.normalize(
                "NFKD",
                text,
            )
            .encode(
                "ascii",
                "ignore",
            )
            .decode(
                "ascii"
            )
        )

    text = re.sub(
        r"\s+",
        "-",
        text,
    )

    text = re.sub(
        r"-+",
        "-",
        text,
    )

    return text.strip("-")


def slugify(text):
    """
    Unicode, punctuation-reduced title slug.

    Parenthetical material is NOT removed here. Recording-title
    variants must be generated explicitly by title_text_variants().
    """

    text = str(text or "").strip()

    text = text.replace("’", "'")

    text = re.sub(
        r"[^\w]+",
        "-",
        text,
        flags=re.UNICODE,
    )

    text = text.replace(
        "_",
        "-",
    )

    text = re.sub(
        r"-+",
        "-",
        text,
    )

    return text.strip("-")


def ascii_slugify(text):
    """
    ASCII, punctuation-reduced title slug.

    Parenthetical material is intentionally preserved at this stage;
    safe title reduction happens before slug construction.
    """

    import unicodedata

    text = str(text or "").strip()

    text = text.replace(
        "’",
        "'",
    )

    text = (
        unicodedata.normalize(
            "NFKD",
            text,
        )
        .encode(
            "ascii",
            "ignore",
        )
        .decode(
            "ascii"
        )
    )

    text = re.sub(
        r"[^A-Za-z0-9]+",
        "-",
        text,
    )

    text = re.sub(
        r"-+",
        "-",
        text,
    )

    return text.strip("-")


def artist_slugify(text):
    """
    Create a Unicode-preserving WhoSampled artist slug.

    Artist punctuation is preserved because WhoSampled commonly
    retains meaningful punctuation in artist URLs.
    """

    import unicodedata

    text = str(text).strip()

    if not text:
        return ""

    text = unicodedata.normalize(
        "NFC",
        text,
    )

    text = text.replace("’", "'")
    text = text.replace("‘", "'")
    text = text.replace("–", "-")
    text = text.replace("—", "-")

    text = re.sub(
        r"\s+",
        "-",
        text,
    )

    text = re.sub(
        r"-+",
        "-",
        text,
    )

    return text.strip("-")


def ascii_artist_slugify(text):
    """
    ASCII-transliterated artist slug while preserving punctuation
    that survives transliteration.
    """

    import unicodedata

    text = str(text).strip()

    if not text:
        return ""

    text = text.replace("’", "'")
    text = text.replace("‘", "'")
    text = text.replace("–", "-")
    text = text.replace("—", "-")

    text = (
        unicodedata.normalize(
            "NFKD",
            text,
        )
        .encode(
            "ascii",
            "ignore",
        )
        .decode(
            "ascii"
        )
    )

    text = re.sub(
        r"\s+",
        "-",
        text,
    )

    text = re.sub(
        r"-+",
        "-",
        text,
    )

    return text.strip("-")


def artist_slug_variants(text):
    """
    Punctuation-preserving Unicode first, ASCII fallback second.
    """

    variants = []

    for value in [
        artist_slugify(text),
        ascii_artist_slugify(text),
    ]:

        if (
            value
            and value not in variants
        ):
            variants.append(value)

    return variants


def title_slug_candidates(title):
    """
    Build title slugs in this order:

      1. Unicode + punctuation preserved
      2. Unicode + punctuation reduced
      3. ASCII + punctuation preserved
      4. ASCII + punctuation reduced

    for each recording-title candidate, with the full title preceding
    any safe base-title candidate.
    """

    candidates = []
    seen = set()

    for title_variant in title_text_variants(
        title
    ):

        text = title_variant["text"]

        forms = [
            (
                punctuation_preserving_slugify(
                    text,
                    ascii_only=False,
                ),
                "unicode_punctuation_preserved",
            ),
            (
                slugify(text),
                "unicode_punctuation_reduced",
            ),
            (
                punctuation_preserving_slugify(
                    text,
                    ascii_only=True,
                ),
                "ascii_punctuation_preserved",
            ),
            (
                ascii_slugify(text),
                "ascii_punctuation_reduced",
            ),
        ]

        for slug, slug_strategy in forms:

            if not slug:
                continue

            key = slug.casefold()

            if key in seen:
                continue

            seen.add(key)

            candidates.append({
                "slug": slug,
                "match_title": text,
                "title_strategy":
                    title_variant[
                        "strategy"
                    ],
                "slug_strategy":
                    slug_strategy,
            })

    return candidates


def slug_variants(text):
    """
    Backward-compatible URL-slug list.

    New code should prefer title_slug_candidates() when it needs
    strategy or match-title metadata.
    """

    return [
        candidate["slug"]
        for candidate in title_slug_candidates(
            text
        )
    ]


def canonical_url(artist, title):

    candidates = canonical_url_candidates(
        artist,
        title,
    )

    if candidates:
        return candidates[0]["url"]

    return ""


def canonical_url_candidates(
    artist,
    title,
):
    """
    Return URL candidates together with the exact title variant that
    should be used to verify that URL.
    """

    candidates = []
    seen = set()

    for artist_slug in artist_slug_variants(
        artist
    ):

        for title_candidate in (
            title_slug_candidates(
                title
            )
        ):

            url = (
                "https://www.whosampled.com/"
                + quote(
                    artist_slug,
                    safe="-",
                )
                + "/"
                + quote(
                    title_candidate["slug"],
                    safe="-",
                )
                + "/"
            )

            if url in seen:
                continue

            seen.add(url)

            candidates.append({
                "url":
                    url,

                "match_title":
                    title_candidate[
                        "match_title"
                    ],

                "title_strategy":
                    title_candidate[
                        "title_strategy"
                    ],

                "slug_strategy":
                    title_candidate[
                        "slug_strategy"
                    ],

                "artist_slug":
                    artist_slug,
            })

    return candidates


def canonical_url_variants(
    artist,
    title,
):
    """
    Backward-compatible URL-only wrapper.
    """

    return [
        candidate["url"]
        for candidate in canonical_url_candidates(
            artist,
            title,
        )
    ]


def is_canonical_track_url(url):
    """
    We only want WhoSampled's canonical track pages.

    Reject relationship pages such as:
        /sample/...
        /cover/...
        /remix/...
    """
    if not url:
        return False

    url = url.lower()

    if "/sample/" in url:
        return False

    if "/cover/" in url:
        return False

    if "/remix/" in url:
        return False

    if "/interpolation/" in url:
        return False

    if "/search/" in url:
        return False

    # Canonical track URLs should have exactly two path
    # components after the domain.
    path = url.split("whosampled.com", 1)[-1]

    parts = [
        p for p in path.strip("/").split("/")
        if p
    ]

    return len(parts) == 2


def verify_track_page(page, url, title, artist_names):
    """
    Open a candidate URL and determine whether it is the
    requested WhoSampled track page.
    """
    if not is_canonical_track_url(url):
        return None

    try:
        response = page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60000
        )

        time.sleep(1)

        if not response:
            return None

        if response.status >= 400:
            return None

        final_url = page.url

        if not is_canonical_track_url(final_url):
            print(
                "  200 RESPONSE BUT NOT A CANONICAL TRACK:",
                final_url
            )
            return None

        page_title = page.title()
        html = page.content()

        title_norm = normalize(title)
        page_title_norm = normalize(page_title)

        # First check title.
        title_match = (
            title_norm
            and title_norm in page_title_norm
        )

        # Then check the artist.
        artist_match = False
        matched_artist = ""

        combined = normalize(
            page_title + " " + html[:120000]
        )

        for artist in artist_variants(artist_names):
            artist_norm = normalize(artist)

            if (
                artist_norm
                and len(artist_norm) >= 3
                and artist_norm in combined
            ):
                artist_match = True
                matched_artist = artist
                break

        if title_match and artist_match:
            return {
                "url": final_url,
                "page_title": page_title,
                "score": 100,
                "reason": (
                    "canonical track URL; "
                    "exact title in page title; "
                    f"artist match: {matched_artist}"
                ),
            }

    except Exception:
        return None

    return None






WIKIDATA_CACHE_FILE = (
    Path("whosampled_html_archive")
    / "wikidata_artist_aliases.json"
)

WIKIDATA_API = (
    "https://www.wikidata.org/w/api.php"
)

_wikidata_last_request = 0.0

WIKIDATA_HUMAN = "Q5"
WIKIDATA_MUSICAL_GROUP = "Q215380"

WIKIDATA_MUSIC_OCCUPATIONS = {
    "Q639669",
    "Q177220",
    "Q36834",
    "Q488205",
    "Q753110",
    "Q158852",
    "Q130857",
    "Q855091",
}

WIKIDATA_MUSIC_DESCRIPTION_TERMS = {
    "musician",
    "recording artist",
    "singer",
    "songwriter",
    "rapper",
    "producer",
    "composer",
    "disc jockey",
    "vocalist",
    "band",
    "musical group",
    "music group",
    "rock band",
    "rock group",
    "duo",
    "trio",
}


def load_wikidata_cache():
    if not WIKIDATA_CACHE_FILE.exists():
        return {}

    try:
        return json.loads(
            WIKIDATA_CACHE_FILE.read_text(
                encoding="utf-8"
            )
        )
    except Exception:
        return {}


def save_wikidata_cache(data):
    WIKIDATA_CACHE_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    WIKIDATA_CACHE_FILE.write_text(
        json.dumps(
            data,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )


def wikidata_wait():
    global _wikidata_last_request

    # No artificial delay for Wikidata.
    # Actual HTTP 429 responses still use exponential backoff.
    _wikidata_last_request = time.monotonic()


def wikidata_api_get(
    params,
    retries=5
):
    url = (
        WIKIDATA_API
        + "?"
        + urllib.parse.urlencode(params)
    )

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent":
                "MusicLibraryPipeline/1.0 "
                "(artist alias research)"
        }
    )

    for attempt in range(retries):

        wikidata_wait()

        try:

            with urllib.request.urlopen(
                request,
                timeout=30
            ) as response:

                return json.load(
                    response
                )

        except Exception as e:

            if "429" not in str(e):
                raise

            backoff = min(
                180,
                20 * (2 ** attempt)
            )

            print(
                f"  Wikidata 429; "
                f"backing off {backoff}s..."
            )

            time.sleep(
                backoff
            )

    return {}


def wikidata_claim_qids(
    entity,
    property_id
):
    values = []

    for claim in (
        entity
        .get("claims", {})
        .get(property_id, [])
    ):

        try:

            value = (
                claim["mainsnak"]
                ["datavalue"]
                ["value"]
            )

            if isinstance(
                value,
                dict
            ):

                qid = value.get(
                    "id"
                )

                if qid:
                    values.append(
                        qid
                    )

        except Exception:
            continue

    return values


def wikidata_is_human(
    entity
):
    return (
        WIKIDATA_HUMAN
        in wikidata_claim_qids(
            entity,
            "P31"
        )
    )


def wikidata_is_group(
    entity
):
    return (
        WIKIDATA_MUSICAL_GROUP
        in wikidata_claim_qids(
            entity,
            "P31"
        )
    )


def wikidata_has_music_occupation(
    entity
):
    return bool(
        set(
            wikidata_claim_qids(
                entity,
                "P106"
            )
        )
        & WIKIDATA_MUSIC_OCCUPATIONS
    )


def wikidata_music_description(
    description
):
    d = normalize(
        description
    )

    return any(
        term in d
        for term
        in WIKIDATA_MUSIC_DESCRIPTION_TERMS
    )


def wikidata_artist_resolution(
    artist_name,
    cache
):
    """
    Resolve Spotify artist identity through Wikidata.

    Returns:
        canonical_name
        aliases
        qid
        status
    """

    key = normalize(
        artist_name
    )

    if not key:
        return None

    if key in cache:

        print(
            "WIKIDATA CACHE HIT:",
            artist_name
        )

        return cache[key]

    print(
        "WIKIDATA LOOKUP:",
        artist_name
    )

    search = wikidata_api_get({
        "action":
            "wbsearchentities",
        "search":
            artist_name,
        "language":
            "en",
        "uselang":
            "en",
        "type":
            "item",
        "limit":
            10,
        "format":
            "json",
    })

    results = search.get(
        "search",
        []
    )

    ids = [
        result.get("id")
        for result in results
        if result.get("id")
    ]

    if not ids:
        result = {
            "status":
                "not_found",
            "spotify_name":
                artist_name,
        }

        cache[key] = result
        save_wikidata_cache(
            cache
        )

        return result

    entities_response = (
        wikidata_api_get({
            "action":
                "wbgetentities",
            "ids":
                "|".join(ids),
            "props":
                "labels|aliases|descriptions|claims",
            "languages":
                "en",
            "format":
                "json",
        })
    )

    entities = (
        entities_response
        .get(
            "entities",
            {}
        )
    )

    ranked = []

    target = normalize(
        artist_name
    )

    for search_result in results:

        qid = search_result.get(
            "id"
        )

        entity = entities.get(
            qid,
            {}
        )

        if not entity:
            continue

        label = (
            entity
            .get("labels", {})
            .get("en", {})
            .get("value")
            or search_result.get(
                "label",
                ""
            )
        )

        description = (
            entity
            .get("descriptions", {})
            .get("en", {})
            .get("value", "")
        )

        aliases = [
            item.get(
                "value",
                ""
            )
            for item in (
                entity
                .get(
                    "aliases",
                    {}
                )
                .get(
                    "en",
                    []
                )
            )
        ]

        label_norm = normalize(
            label
        )

        alias_norms = {
            normalize(alias)
            for alias in aliases
        }

        exact_name = (
            target == label_norm
        )

        exact_alias = (
            target
            in alias_norms
        )

        human = (
            wikidata_is_human(
                entity
            )
        )

        group = (
            wikidata_is_group(
                entity
            )
        )

        music_occupation = (
            wikidata_has_music_occupation(
                entity
            )
        )

        music_description = (
            wikidata_music_description(
                description
            )
        )

        score = 0

        if exact_name:
            score += 100

        elif exact_alias:
            score += 95

        if human:
            score += 60

        if group:
            score += 55

        if music_occupation:
            score += 50

        if music_description:
            score += 20

        bad_terms = {
            "album",
            "discography",
            "concert",
            "given name",
            "song",
            "film",
            "book",
            "episode",
            "box set",
        }

        description_words = set(
            normalize(
                description
            ).split()
        )

        if any(
            term in description_words
            for term in bad_terms
        ):
            score -= 100

        # Require actual music evidence.
        strong_group_description = any(
            term in normalize(description)
            for term in {
                "band",
                "musical group",
                "music group",
                "rock band",
                "rock group",
                "duo",
                "trio",
                "quartet",
                "ensemble",
            }
        )

        valid_music_identity = (
            (
                human
                and (
                    music_occupation
                    or music_description
                )
            )
            or group
            or music_occupation
            or strong_group_description
        )

        if not valid_music_identity:
            score -= 120

        ranked.append({
            "qid": qid,
            "label": label,
            "description": description,
            "aliases": aliases,
            "human": human,
            "musical_group": group,
            "music_occupation":
                music_occupation,
            "music_description":
                music_description,
            "score": score,
            "exact_name_or_alias": (
                exact_name
                or exact_alias
            ),
            "valid_music_identity":
                valid_music_identity,
        })

    if not ranked:

        result = {
            "status":
                "not_found",
            "spotify_name":
                artist_name,
        }

    else:

        ranked.sort(
            key=lambda x:
                x["score"],
            reverse=True
        )

        best = ranked[0]

        result = {
            **best,
            "spotify_name":
                artist_name,
            "status":
                (
                    "resolved"
                    if (
                        best[
                            "exact_name_or_alias"
                        ]
                        and best[
                            "valid_music_identity"
                        ]
                    )
                    else "review"
                ),
        }

    cache[key] = result

    save_wikidata_cache(
        cache
    )

    return result


ARTIST_SLUG_CACHE_FILE = (
    Path("whosampled_html_archive")
    / "artist_slug_aliases.json"
)


def load_artist_slug_cache():
    if not ARTIST_SLUG_CACHE_FILE.exists():
        return {}

    try:
        return json.loads(
            ARTIST_SLUG_CACHE_FILE.read_text(
                encoding="utf-8"
            )
        )
    except Exception:
        return {}


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



def artist_archive_path(artist):
    """Persistent cache for a WhoSampled artist page."""
    archive_dir = (
        Path("whosampled_html_archive")
        / "artists"
    )

    archive_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    return (
        archive_dir
        / f"{slugify(artist)}.html"
    )


def extract_artist_aliases(
    html,
    artist_name
):
    """
    Extract alternate artist names from the
    Aliases section of a WhoSampled artist page.
    """

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    target = normalize(
        artist_name
    )

    aliases = []
    seen = set()

    # Find the heading containing "Aliases".
    heading = None

    for node in soup.find_all(
        string=lambda s:
        s and normalize(s) == "aliases"
    ):
        heading = node.parent
        break

    if heading is None:
        return aliases

    # Search nearby containers for artist links.
    container = heading

    for _ in range(5):
        container = container.parent

        if container is None:
            break

        links = container.select(
            "a[href]"
        )

        if links:
            for link in links:

                name = link.get_text(
                    " ",
                    strip=True
                )

                if not name:
                    continue

                normalized = normalize(
                    name
                )

                if (
                    normalized
                    and normalized != target
                    and normalized not in seen
                ):
                    seen.add(normalized)
                    aliases.append(name)

            if aliases:
                break

    return aliases


def get_artist_aliases(
    page,
    artist_name
):
    """
    Load artist aliases from cache or fetch the
    WhoSampled artist page once.
    """

    archive_file = artist_archive_path(
        artist_name
    )

    # ------------------------------
    # Cache
    # ------------------------------

    if archive_file.exists():

        print(
            "ARTIST ALIAS CACHE HIT:",
            archive_file
        )

        try:
            html = archive_file.read_text(
                encoding="utf-8",
                errors="ignore"
            )

            return extract_artist_aliases(
                html,
                artist_name
            )

        except Exception as e:

            print(
                "ARTIST ALIAS CACHE ERROR:",
                repr(e)
            )

    # ------------------------------
    # Fetch artist page
    # ------------------------------

    artist_url = (
        "https://www.whosampled.com/"
        + quote(
            artist_slugify(artist_name),
            safe="-"
        )
        + "/"
    )

    print(
        "WHO SAMPLED ARTIST PAGE:",
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
            "ARTIST PAGE REQUEST ERROR:",
            repr(e)
        )

        return []

    status = (
        response.status
        if response
        else None
    )

    print(
        "ARTIST PAGE STATUS:",
        status
    )

    if status == 429:
        raise SystemExit(
            "Stopped safely on HTTP 429 "
            "while fetching artist aliases."
        )

    # The artist-page request is a real WhoSampled request.
    delay = random.uniform(
        15,
        30
    )

    print(
        f"Waiting {delay:.1f} seconds "
        "before the next WhoSampled request..."
    )

    time.sleep(delay)

    if status != 200:
        return []

    html = page.content()

    archive_file.write_text(
        html,
        encoding="utf-8"
    )

    print(
        "ARTIST PAGE SAVED:",
        archive_file
    )

    return extract_artist_aliases(
        html,
        artist_name
    )



def find_search_candidates(page, title, artist_names):
    """
    Search WhoSampled, but ONLY retain canonical track pages.

    Relationship pages such as /sample/123/... are discarded.
    """
    candidates = []

    query = f"{title} {artist_names}"

    search_url = (
        "https://www.whosampled.com/search/?q="
        + quote(query)
    )

    try:
        page.goto(
            search_url,
            wait_until="domcontentloaded",
            timeout=60000
        )

        time.sleep(2)

    except Exception:
        return candidates

    links = page.locator("a[href]").all()

    seen = set()

    for link in links:
        try:
            href = link.get_attribute("href")
            text = link.inner_text().strip()

            if not href:
                continue

            if href.startswith("/"):
                url = (
                    "https://www.whosampled.com"
                    + href
                )
            elif href.startswith(
                "https://www.whosampled.com/"
            ):
                url = href
            else:
                continue

            if not is_canonical_track_url(url):
                continue

            if url in seen:
                continue

            seen.add(url)

            candidates.append({
                "url": url,
                "text": text,
            })

        except Exception:
            continue

    return candidates[:30]


def score_candidate(page, candidate, title, artist_names):
    """
    Verify and score a canonical candidate.
    """
    result = verify_track_page(
        page,
        candidate["url"],
        title,
        artist_names
    )

    if result:
        return result

    return None


def main():

    df = pd.read_csv(INPUT)

    artist_slug_cache = load_artist_slug_cache()

    wikidata_cache = load_wikidata_cache()

    # Preserve existing columns if present.
    for column in [
        "whosampled_url",
        "whosampled_match_status",
        "whosampled_page_title",
    ]:
        if column not in df.columns:
            df[column] = ""

    candidate_rows = []

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

        for i, row in df.iterrows():

            title = str(row["title"]).strip()
            artist_names = str(
                row["artist_names"]
            ).strip()

            print()
            print("=" * 70)
            print(
                f"{i + 1}/{len(df)}  "
                f"{title} — {artist_names}"
            )
            print("=" * 70)

            # --------------------------------------------------
            # STAGE 1: Try canonical URL directly
            # --------------------------------------------------

            first_artist = artist_variants(
                artist_names
            )

            direct_match = None

            for artist in first_artist:

                # --------------------------------------------------
                # Try WhoSampled artist slugs learned from previous
                # successful search results before generating a slug
                # from Spotify's artist name.
                # --------------------------------------------------

                learned_slugs = (
                    artist_slug_cache.get(
                        normalize(artist),
                        []
                    )
                )

                for learned_slug in learned_slugs:

                    for url in canonical_url_variants(
                        learned_slug,
                        title
                    ):

                        print()
                        print(
                            "LEARNED ARTIST SLUG:",
                            learned_slug,
                            "→",
                            url
                        )

                        match = verify_track_page(
                            page,
                            url,
                            title,
                            artist_names
                        )

                        if match:
                            direct_match = match
                            break

                    if direct_match:
                        break

                if direct_match:
                    break

                # --------------------------------------------------
                # Original direct URL generated from Spotify artist.
                # --------------------------------------------------

                for url in canonical_url_variants(
                    artist,
                    title
                ):

                    print()
                    print(
                        "DIRECT:",
                        url
                    )

                    match = verify_track_page(
                        page,
                        url,
                        title,
                        artist_names
                    )

                    if match:
                        direct_match = match
                        break

                if direct_match:
                    break

            if direct_match:

                df.at[
                    i,
                    "whosampled_url"
                ] = direct_match["url"]

                df.at[
                    i,
                    "whosampled_match_status"
                ] = "matched"

                df.at[
                    i,
                    "whosampled_page_title"
                ] = direct_match["page_title"]

                print()
                print(
                    "DIRECT MATCH:",
                    direct_match["url"]
                )

                time.sleep(3)

                continue

            # --------------------------------------------------
            # STAGE 2: Search WhoSampled
            # --------------------------------------------------

            print()
            print("DIRECT MATCH FAILED")

            # --------------------------------------------------
            # STAGE 2: Wikidata artist identity resolution
            # --------------------------------------------------

            wikidata_names = []

            for artist in first_artist:

                try:

                    wd = wikidata_artist_resolution(
                        artist,
                        wikidata_cache
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

                if wd.get(
                    "status"
                ) != "resolved":

                    continue

                canonical = wd.get(
                    "label",
                    ""
                )

                if canonical:
                    wikidata_names.append(
                        canonical
                    )

                wikidata_names.extend(
                    wd.get(
                        "aliases",
                        []
                    )
                )

            # Deduplicate Wikidata-derived names.
            unique_wikidata_names = []
            seen_wikidata_names = set()

            for name in wikidata_names:

                name_key = normalize(
                    name
                )

                if (
                    name_key
                    and name_key
                    not in seen_wikidata_names
                ):

                    seen_wikidata_names.add(
                        name_key
                    )

                    unique_wikidata_names.append(
                        name
                    )

            wikidata_match = None

            for candidate_artist in (
                unique_wikidata_names
            ):

                for candidate_url in canonical_url_variants(
                    candidate_artist,
                    title
                ):

                    print()
                    print(
                        "WIKIDATA ARTIST CANDIDATE:",
                        candidate_artist,
                        "→",
                        candidate_url
                    )

                    result = verify_track_page(
                        page,
                        candidate_url,
                        title,
                        artist_names
                    )

                    if result:

                        wikidata_match = result
                        break

                if wikidata_match:
                    break

            if wikidata_match:

                df.at[
                    i,
                    "whosampled_url"
                ] = wikidata_match[
                    "url"
                ]

                df.at[
                    i,
                    "whosampled_match_status"
                ] = "matched"

                df.at[
                    i,
                    "whosampled_page_title"
                ] = wikidata_match[
                    "page_title"
                ]

                print()
                print(
                    "WIKIDATA MATCH:",
                    wikidata_match["url"]
                )

                time.sleep(3)

                continue

            # --------------------------------------------------
            # STAGE 3: Resolution exhausted.
            #
            # Direct/learned WhoSampled URLs and Wikidata have
            # already been tried. The old WhoSampled /search/
            # fallback is intentionally disabled because it does
            # not produce usable artist/track results in our
            # automated session.
            # --------------------------------------------------

            # --------------------------------------------------
            # Decide
            # --------------------------------------------------

            if len(verified) == 1:

                match = verified[0]

                df.at[
                    i,
                    "whosampled_url"
                ] = match["url"]

                df.at[
                    i,
                    "whosampled_match_status"
                ] = "matched"

                df.at[
                    i,
                    "whosampled_page_title"
                ] = match["page_title"]

                print()
                print(
                    "MATCH:",
                    match["url"]
                )

            elif len(verified) > 1:

                df.at[
                    i,
                    "whosampled_match_status"
                ] = "review"

                print()
                print(
                    "REVIEW REQUIRED:",
                    len(verified),
                    "canonical candidates"
                )

                for n, candidate in enumerate(
                    verified[:10],
                    start=1
                ):
                    print()
                    print(
                        n,
                        candidate["url"]
                    )

            else:

                df.at[
                    i,
                    "whosampled_match_status"
                ] = "not_found"

                print()
                print("NOT FOUND")

            # Save progress after every track.
            df.to_csv(
                OUTPUT,
                index=False
            )

            time.sleep(5)

        browser.close()

    # Save candidate table.
    pd.DataFrame(
        candidate_rows
    ).to_csv(
        CANDIDATE_OUTPUT,
        index=False
    )

    # Final save.
    df.to_csv(
        OUTPUT,
        index=False
    )

    print()
    print("=" * 70)
    print("WHOSAMPLED MATCH COMPLETE")
    print("=" * 70)

    print(
        "Tracks:",
        len(df)
    )

    print(
        "Matched:",
        (
            df[
                "whosampled_match_status"
            ] == "matched"
        ).sum()
    )

    print(
        "Review:",
        (
            df[
                "whosampled_match_status"
            ] == "review"
        ).sum()
    )

    print(
        "Not found:",
        (
            df[
                "whosampled_match_status"
            ] == "not_found"
        ).sum()
    )

    print()
    print("Main output:", OUTPUT)
    print("Candidate output:", CANDIDATE_OUTPUT)


def run_offline_slug_regression_tests():
    """
    Offline known-answer tests.

    No HTTP requests are made.
    """

    from urllib.parse import unquote

    def url_key(url):
        return (
            unquote(
                str(url)
            )
            .rstrip("/")
            .casefold()
        )

    fixtures = [
        {
            "name":
                "Secos & Molhados — Sangue Latino",

            "artist":
                "Secos & Molhados",

            "title":
                "Sangue Latino",

            "expected":
                (
                    "https://www.whosampled.com/"
                    "Secos-%26-Molhados/"
                    "Sangue-Latino/"
                ),
        },
        {
            "name":
                (
                    "Erasmo Carlos — "
                    "É Preciso Dar Um Jeito, Meu Amigo"
                ),

            "artist":
                "Erasmo Carlos",

            "title":
                (
                    "É Preciso Dar Um Jeito, "
                    "Meu Amigo"
                ),

            "expected":
                (
                    "https://www.whosampled.com/"
                    "Erasmo-Carlos/"
                    "%C3%89-Preciso-Dar-Um-Jeito,"
                    "-Meu-Amigo/"
                ),
        },
        {
            "name":
                (
                    "Milton Nascimento — "
                    "Tudo Que Você Podia Ser"
                ),

            "artist":
                "Milton Nascimento",

            "title":
                "Tudo Que Você Podia Ser",

            "expected":
                (
                    "https://www.whosampled.com/"
                    "Milton-Nascimento/"
                    "Tudo-Que-Voc%C3%AA-Podia-Ser/"
                ),
        },
    ]

    failures = []

    print()
    print("=" * 72)
    print("WHOSAMPLED OFFLINE URL REGRESSION TESTS")
    print("=" * 72)

    for fixture in fixtures:

        generated = (
            canonical_url_variants(
                fixture["artist"],
                fixture["title"],
            )
        )

        generated_keys = {
            url_key(url)
            for url in generated
        }

        passed = (
            url_key(
                fixture["expected"]
            )
            in generated_keys
        )

        print()
        print(
            "PASS"
            if passed
            else "FAIL",
            fixture["name"],
        )

        print(
            "  expected:",
            fixture["expected"],
        )

        if not passed:

            failures.append(
                fixture["name"]
            )

            print(
                "  generated:"
            )

            for url in generated:
                print(
                    "   ",
                    url,
                )

    # --------------------------------------------------------
    # Safety / behavior tests.
    # --------------------------------------------------------

    print()
    print("-" * 72)
    print("TITLE-VARIANT SAFETY TESTS")
    print("-" * 72)

    cinco = {
        item["text"]
        for item in title_text_variants(
            "Cinco Minutos (5 Minutos)"
        )
    }

    lotus = {
        item["text"]
        for item in title_text_variants(
            "Lotus 72 D - Fast Version"
        )
    }

    remix = {
        item["text"]
        for item in title_text_variants(
            "Song (Remix)"
        )
    }

    behavior_tests = [
        (
            "safe parenthetical candidate",
            "Cinco Minutos" in cinco,
        ),
        (
            "safe Fast Version candidate",
            "Lotus 72 D" in lotus,
        ),
        (
            "remix never collapses",
            "Song" not in remix,
        ),
    ]

    for name, passed in behavior_tests:

        print(
            "PASS"
            if passed
            else "FAIL",
            name,
        )

        if not passed:
            failures.append(name)

    print()
    print("=" * 72)

    if failures:

        print(
            "FAILED:",
            len(failures),
        )

        for failure in failures:
            print(
                " -",
                failure,
            )

        raise SystemExit(1)

    print(
        "ALL OFFLINE MATCHING REGRESSIONS PASSED"
    )




if __name__ == "__main__":
    import sys

    if "--self-test-slugs" in sys.argv:
        run_offline_slug_regression_tests()
    else:
        main()
