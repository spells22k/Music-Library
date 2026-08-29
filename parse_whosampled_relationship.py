#!/usr/bin/env python3

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

from bs4 import BeautifulSoup


BASE_URL = "https://www.whosampled.com/"


# ============================================================
# BASIC HELPERS
# ============================================================

def clean_text(value):
    if value is None:
        return None

    if hasattr(value, "get_text"):
        value = value.get_text(" ", strip=True)

    value = unquote(str(value))
    value = re.sub(r"\s+", " ", value).strip()

    return value or None


def normalize_url(url):
    """
    Convert relative WhoSampled URLs to absolute canonical URLs.

    Also removes:
      - whitespace
      - fragments
      - query strings
      - accidental duplicate slashes
    """

    if not url:
        return None

    url = clean_text(url)

    if not url:
        return None

    url = urljoin(BASE_URL, url)

    parsed = urlparse(url)

    path = re.sub(r"/+", "/", parsed.path)

    if path != "/" and not path.endswith("/"):
        path += "/"

    return f"https://www.whosampled.com{path}"


def parse_year(value):
    if not value:
        return None

    match = re.search(r"\b(19|20)\d{2}\b", value)

    if match:
        return int(match.group(0))

    return None


def parse_duration_seconds(value):
    """
    Parse ISO 8601 durations such as:

        PT0H6M14S
        PT3M11S
        PT5S
    """

    if not value:
        return None

    value = str(value)

    match = re.match(
        r"^PT"
        r"(?:(\d+)H)?"
        r"(?:(\d+)M)?"
        r"(?:(\d+(?:\.\d+)?)S)?$",
        value
    )

    if not match:
        return None

    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = float(match.group(3) or 0)

    total = hours * 3600 + minutes * 60 + seconds

    return int(total) if total.is_integer() else total


# ============================================================
# ARTISTS
# ============================================================

def extract_artists(container):
    """
    Extract ALL artists from the byArtist block.

    WhoSampled can encode multiple artists using multiple
    itemprop=name elements inside itemprop=byArtist.

    Example:

        <div itemprop="byArtist">
            <meta itemprop="name" content="DJ Marky">
            <meta itemprop="name" content="XRS">
            <meta itemprop="name" content="Stamina MC">
        </div>
    """

    artists = []

    artist_blocks = container.select('[itemprop="byArtist"]')

    for block in artist_blocks:

        # Preferred: explicit schema.org name values.
        for node in block.select('[itemprop="name"]'):

            value = node.get("content")

            if not value:
                value = clean_text(node)

            if value and value not in artists:
                artists.append(value)

        # Fallback if no explicit names were found.
        if not artists:
            links = block.select("a")

            for link in links:
                value = clean_text(link)

                if value and value not in artists:
                    artists.append(value)

    return artists


# ============================================================
# PRODUCERS
# ============================================================

def extract_producers(container):
    """
    Extract all producers.

    Producer data is specifically located using:

        [itemprop="producer"]

    Producer names may be represented by:

        <span itemprop="name">Dave Angel</span>

    or:

        <a itemprop="url">...</a>
        <span itemprop="name">Dave Angel</span>
    """

    producers = []

    for producer in container.select('[itemprop="producer"]'):

        # Most precise selector.
        names = producer.select('[itemprop="name"]')

        if names:
            for name_node in names:

                value = name_node.get("content")

                if not value:
                    value = clean_text(name_node)

                if value and value not in producers:
                    producers.append(value)

            continue

        # Fallback: producer link text.
        link = producer.select_one("a")

        if link:
            value = clean_text(link)

            if value and value not in producers:
                producers.append(value)
            continue

        # Final fallback.
        value = clean_text(producer)

        if value and value not in producers:
            producers.append(value)

    return producers


# ============================================================
# ALBUM / RELEASE DATA
# ============================================================

def extract_album(container):
    album = container.select_one(
        '[itemprop="inAlbum"] [itemprop="name"]'
    )

    if not album:
        return None

    # Avoid accidentally selecting nested artist metadata.
    value = album.get("content")

    if not value:
        value = clean_text(album)

    return value


def extract_label(container):
    node = container.select_one(
        '[itemprop="inAlbum"] [itemprop="recordLabel"]'
    )

    if not node:
        return None

    value = node.get("content")

    if not value:
        value = clean_text(node)

    return value


def extract_year(container):
    node = container.select_one(
        '[itemprop="inAlbum"] [itemprop="datePublished"]'
    )

    if node:
        value = node.get("content")

        if not value:
            value = clean_text(node)

        year = parse_year(value)

        if year:
            return year

    # Fallback: search within the album block.
    album_block = container.select_one('[itemprop="inAlbum"]')

    if album_block:
        year = parse_year(clean_text(album_block))

        if year:
            return year

    return None


# ============================================================
# TRACK URL
# ============================================================

def extract_track_url(container):
    """
    Track URL is the itemprop=url link directly associated with
    the track container.

    We intentionally prefer the first track-level URL rather than
    grabbing arbitrary artist URLs.
    """

    link = container.select_one('a[itemprop="url"]')

    if link and link.get("href"):
        return normalize_url(link["href"])

    return None


# ============================================================
# TRACK NAME
# ============================================================

def extract_track_name(container):
    node = container.select_one('[itemprop="name"]')

    if not node:
        return None

    value = node.get("content")

    if not value:
        value = clean_text(node)

    return value


# ============================================================
# TIMING
# ============================================================

def extract_timestamp(container, relationship_type):
    """
    Relationship pages contain timing information outside the
    normal track metadata.

    We search broadly but only inside the appropriate destination
    or source container first.

    Known structures include classes containing:

        sample-dest-timing
        sample-source-timing

    """

    if relationship_type != "sampled":
        return None

    # First look inside the track container.
    timing_selectors = [
        '[class*="sample-dest-timing"]',
        '[class*="sample-source-timing"]',
        '[id*="sample-dest-timing"]',
        '[id*="sample-source-timing"]',
        '[class*="sampleTiming"]',
        '[class*="sample-timing"]',
    ]

    for selector in timing_selectors:

        node = container.select_one(selector)

        if node:
            text = clean_text(node)

            seconds = extract_seconds_from_text(text)

            if seconds is not None:
                return seconds

    # The timing markup may live outside the track metadata
    # container, so return None here and let the page-level
    # extractor handle it.
    return None


def extract_seconds_from_text(text):
    if not text:
        return None

    text = clean_text(text)

    # Examples:
    # 1:33
    # 0:05
    # 93 seconds
    # 5 sec
    # 1m 33s

    match = re.search(r"\b(\d+):(\d{2})\b", text)

    if match:
        minutes = int(match.group(1))
        seconds = int(match.group(2))

        return minutes * 60 + seconds

    match = re.search(
        r"\b(?:(\d+)\s*m(?:in(?:ute)?s?)?\s*)?"
        r"(\d+(?:\.\d+)?)\s*s(?:ec(?:ond)?s?)?\b",
        text,
        re.I
    )

    if match:
        minutes = int(match.group(1) or 0)
        seconds = float(match.group(2))

        total = minutes * 60 + seconds

        return int(total) if total.is_integer() else total

    return None


def extract_page_timestamps(soup):
    """
    Page-level fallback for sample timing.

    Based on the confirmed raw HTML locations:

        sample-dest-timing
        sample-source-timing
    """

    result = {
        "dest": None,
        "source": None,
    }

    selectors = {
        "dest": [
            '[class*="sample-dest-timing"]',
            '[id*="sample-dest-timing"]',
        ],
        "source": [
            '[class*="sample-source-timing"]',
            '[id*="sample-source-timing"]',
        ],
    }

    for side, side_selectors in selectors.items():

        for selector in side_selectors:

            node = soup.select_one(selector)

            if not node:
                continue

            # Check attributes as well as visible text.
            candidates = [
                node.get("data-time"),
                node.get("data-timestamp"),
                node.get("data-seconds"),
                node.get("content"),
                clean_text(node),
            ]

            for candidate in candidates:

                seconds = extract_seconds_from_text(candidate)

                if seconds is not None:
                    result[side] = seconds
                    break

            if result[side] is not None:
                break

    return result


# ============================================================
# SAMPLE / RELATIONSHIP TYPE
# ============================================================

def extract_sample_type(soup):
    """
    Find the descriptive relationship text.

    Examples seen in the HTML:

        Direct Sample of Hook / Riff
        Cover Version
        Remix
    """

    selectors = [
        '[itemprop="sampleType"]',
        '[itemprop="relationshipType"]',
        '.sample-type',
        '.sampleType',
        '[class*="sample-type"]',
        '[class*="sampleType"]',
    ]

    for selector in selectors:

        nodes = soup.select(selector)

        for node in nodes:

            value = node.get("content")

            if not value:
                value = clean_text(node)

            if value:
                return value

    # Broad fallback based on relationship-specific language.
    page_text = clean_text(soup)

    if page_text:

        patterns = [
            r"Direct Sample of[^<\n]*",
            r"Cover Version",
            r"Remix",
            r"Interpolat(?:es|ed|ion)[^<\n]*",
        ]

        for pattern in patterns:

            match = re.search(pattern, page_text, re.I)

            if match:
                return clean_text(match.group(0))

    return None


# ============================================================
# RELATIONSHIP TYPE
# ============================================================

def detect_relationship_type(url, soup=None):
    if url:
        path = urlparse(url).path.lower()

        if "/sample/" in path:
            return "sampled"

        if "/cover/" in path:
            return "covers"

        if "/remix/" in path:
            return "remix"

        if "/interpolation/" in path:
            return "interpolates"

    if soup:
        text = clean_text(soup).lower()

        # Prefer specific relationship semantics before the
        # generic word "sample", which may appear elsewhere
        # on remix/cover/interpolation pages.
        if "remix" in text:
            return "remix"

        if "cover" in text:
            return "covers"

        if (
            "interpolates" in text
            or "interpolated" in text
            or "interpolation" in text
        ):
            return "interpolates"

        if "sample" in text:
            return "sampled"

    return None


# ============================================================
# TRACK EXTRACTION
# ============================================================

def extract_track(container, relationship_type, timestamp=None):

    duration_node = container.select_one('[itemprop="duration"]')

    duration = None

    if duration_node:
        duration = parse_duration_seconds(
            duration_node.get("content")
        )

    return {
        "name": extract_track_name(container),
        "artists": extract_artists(container),
        "year": extract_year(container),
        "album": extract_album(container),
        "label": extract_label(container),
        "url": extract_track_url(container),
        "producers": extract_producers(container),
        "sample_timestamp_seconds": (
            timestamp
            if timestamp is not None
            else extract_timestamp(container, relationship_type)
        ),

        # Duration is already parsed above from Schema.org
        # itemprop="duration"; preserve it in the returned
        # track object instead of discarding it.
        "duration":
            duration,
    }


# ============================================================
# MAIN PARSER
# ============================================================

def parse_relationship(html_path, supplied_url=None):

    html_path = Path(html_path)

    if not html_path.exists():
        raise FileNotFoundError(html_path)

    html = html_path.read_text(
        encoding="utf-8",
        errors="replace"
    )

    soup = BeautifulSoup(html, "html.parser")

    url = normalize_url(supplied_url) if supplied_url else None

    relationship_type = detect_relationship_type(
        url,
        soup
    )

    dest = soup.select_one("#sampleWrap_dest")
    source = soup.select_one("#sampleWrap_source")

    if not dest:
        raise RuntimeError(
            'Could not find destination container: #sampleWrap_dest'
        )

    if not source:
        raise RuntimeError(
            'Could not find source container: #sampleWrap_source'
        )

    timestamps = extract_page_timestamps(soup)

    track_1 = extract_track(
        dest,
        relationship_type,
        timestamps["dest"]
    )

    track_2 = extract_track(
        source,
        relationship_type,
        timestamps["source"]
    )

    # Relationship metadata.
    whosampled_id = None

    if url:
        match = re.search(
            r"/(?:sample|cover|remix|interpolation)/(\d+)/",
            url
        )

        if match:
            whosampled_id = int(match.group(1))

    if relationship_type == "sampled":
        sample_type = extract_sample_type(soup)
    elif relationship_type == "covers":
        sample_type = extract_sample_type(soup) or "Cover Version"
    elif relationship_type == "remix":
        sample_type = extract_sample_type(soup) or "Remix"
    else:
        sample_type = extract_sample_type(soup)

    return {
        "relationship_type": relationship_type,
        "whosampled_id": whosampled_id,
        "whosampled_url": url,
        "track_1": track_1,
        "track_2": track_2,
        "sample_type": sample_type,
    }


# ============================================================
# CLI
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description="Parse a saved WhoSampled relationship page."
    )

    parser.add_argument(
        "html",
        help="Saved WhoSampled relationship HTML"
    )

    parser.add_argument(
        "--url",
        help="Original WhoSampled relationship URL"
    )

    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON"
    )

    parser.add_argument(
        "--no-clipboard",
        action="store_true",
        help="Ignored; retained for CLI compatibility"
    )

    args = parser.parse_args()

    result = parse_relationship(
        args.html,
        args.url
    )

    if args.pretty:
        print(
            json.dumps(
                result,
                indent=2,
                ensure_ascii=False
            )
        )
    else:
        print(
            json.dumps(
                result,
                ensure_ascii=False
            )
        )


if __name__ == "__main__":
    main()
