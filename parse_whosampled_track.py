import json
import argparse
import csv
import re
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup


BASE = "https://www.whosampled.com"


def clean(text):
    return " ".join(text.split()) if text else ""


def parse_iso_duration_ms(value):
    """
    Convert the ISO-8601 duration format used by WhoSampled
    track pages into milliseconds.

    Examples:
        PT0H2M0S   -> 120000
        PT0H2M58S  -> 178000
        PT0H6M22S  -> 382000

    Missing or unrecognized values return None rather than 0.
    """

    value = clean(value)

    if not value:
        return None

    match = re.fullmatch(
        r"PT"
        r"(?:(\d+(?:\.\d+)?)H)?"
        r"(?:(\d+(?:\.\d+)?)M)?"
        r"(?:(\d+(?:\.\d+)?)S)?",
        value,
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    hours_raw, minutes_raw, seconds_raw = (
        match.groups()
    )

    # A bare "PT" is not a usable duration.
    if not any(
        (
            hours_raw,
            minutes_raw,
            seconds_raw,
        )
    ):
        return None

    hours = float(
        hours_raw or 0
    )

    minutes = float(
        minutes_raw or 0
    )

    seconds = float(
        seconds_raw or 0
    )

    total_seconds = (
        hours * 3600
        + minutes * 60
        + seconds
    )

    return int(
        round(
            total_seconds * 1000
        )
    )


def relationship_type_from_url(url):
    url_lower = url.lower()

    if "/sample/" in url_lower:
        return "sampled"

    if "/cover/" in url_lower:
        return "covered"

    if "/interpolation/" in url_lower:
        return "interpolated"

    if "/remix/" in url_lower:
        return "remixed"

    return None


def is_artist_link(href):
    if not href:
        return False

    blocked = (
        "/sample/",
        "/cover/",
        "/interpolation/",
        "/remix/",
        "/album/",
        "/browse/",
        "/search/",
        "/user/",
        "/movie/",
        "/tv-show/",
    )

    href_lower = href.lower()

    if any(x in href_lower for x in blocked):
        return False

    return (
        href.startswith("/")
        and href.count("/") == 2
    )


def extract_source_metadata(soup):
    """
    Extract canonical source-recording metadata from an archived
    WhoSampled track page.

    This function performs no network requests.
    """

    source_title = ""

    h1 = soup.select_one(
        "h1"
    )

    if h1:

        title_meta = h1.select_one(
            'meta[itemprop="name"]'
        )

        if (
            title_meta
            and title_meta.get("content")
        ):
            source_title = clean(
                title_meta["content"]
            )
        else:
            source_title = clean(
                h1.get_text(
                    " ",
                    strip=True
                )
            )

    source_url = ""

    canonical = soup.select_one(
        'link[rel="canonical"]'
    )

    if canonical:
        source_url = clean(
            canonical.get("href", "")
        )

    # --------------------------------------------------------
    # Primary artists.
    # --------------------------------------------------------

    source_artists = []
    source_artist_profiles = []

    for artist_link in soup.select(
        ".trackArtistNames a[href]"
    ):

        artist = clean(
            artist_link.get_text(
                " ",
                strip=True
            )
        )

        if (
            artist
            and artist not in source_artists
        ):
            source_artists.append(
                artist
            )

        href = clean(
            artist_link.get(
                "href",
                "",
            )
        )

        if not href:
            continue

        artist_profile_url = urljoin(
            BASE,
            href,
        )

        if not is_artist_link(
            artist_link.get(
                "href",
                "",
            )
        ):
            continue

        profile_item = {
            "artist": artist,
            "url": artist_profile_url,
        }

        if profile_item not in source_artist_profiles:
            source_artist_profiles.append(
                profile_item
            )

    # --------------------------------------------------------
    # Producers.
    # --------------------------------------------------------

    producers = []
    producer_profiles = []

    for producer in soup.select(
        '[itemprop="producer"] [itemprop="name"]'
    ):

        name = clean(
            producer.get_text(
                " ",
                strip=True
            )
        )

        if (
            name
            and name not in producers
        ):
            producers.append(
                name
            )

        producer_link = producer.select_one(
            "a[href]"
        )

        producer_href = (
            clean(
                producer_link.get(
                    "href",
                    "",
                )
            )
            if producer_link
            else ""
        )

        producer_url = (
            urljoin(
                BASE,
                producer_href,
            )
            if producer_href
            else ""
        )

        profile_item = {
            "artist": name,
            "url": producer_url,
        }

        if (
            name
            and profile_item
            not in producer_profiles
        ):
            producer_profiles.append(
                profile_item
            )

    # --------------------------------------------------------
    # Structured track credits.
    #
    # WhoSampled uses track-credit-item containers with labels
    # such as Composer:, Lyricist:, etc. Restricting the search
    # to these containers prevents site/community contributor
    # metadata from being mistaken for music credits.
    # --------------------------------------------------------

    credits = []

    def add_credit(
        artist,
        role,
        source_role=None,
        whosampled_url="",
    ):

        artist = clean(
            artist
        )

        role = clean(
            role
        )

        source_role = (
            clean(source_role)
            if source_role is not None
            else role
        )

        whosampled_url = clean(
            whosampled_url
        )

        if whosampled_url:
            whosampled_url = urljoin(
                BASE,
                whosampled_url,
            )

        if not artist or not role:
            return

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

        canonical_role = canonical_roles.get(
            role.casefold(),
            role.casefold().replace(
                " ",
                "_",
            ),
        )

        item = {
            "artist": artist,
            "role": canonical_role,
            "source_role": source_role,
            "whosampled_url": whosampled_url,
        }

        key = (
            item["artist"],
            item["role"],
            item["source_role"],
        )

        for existing in credits:
            existing_key = (
                existing["artist"],
                existing["role"],
                existing["source_role"],
            )

            if existing_key != key:
                continue

            # Prefer retaining explicit archived profile evidence
            # when a duplicate credit was first observed without it.
            if (
                whosampled_url
                and not clean(
                    existing.get(
                        "whosampled_url"
                    )
                )
            ):
                existing[
                    "whosampled_url"
                ] = whosampled_url

            return

        credits.append(
            item
        )

    for credit_item in soup.select(
        ".track-credit-item"
    ):

        title_node = credit_item.select_one(
            ".track-credit-title"
        )

        if not title_node:
            continue

        source_role = clean(
            title_node.get_text(
                " ",
                strip=True
            )
        ).rstrip(":")

        role = source_role.casefold()

        contributor_nodes = credit_item.select(
            '[itemprop="contributor"] [itemprop="name"]'
        )

        for contributor in contributor_nodes:

            contributor_link = (
                contributor.select_one(
                    "a[href]"
                )
            )

            contributor_href = (
                clean(
                    contributor_link.get(
                        "href",
                        "",
                    )
                )
                if contributor_link
                else ""
            )

            add_credit(
                contributor.get_text(
                    " ",
                    strip=True
                ),
                role,
                source_role,
                contributor_href,
            )

    # Producers may be represented separately through Schema.org.
    for producer_profile in producer_profiles:

        add_credit(
            producer_profile.get(
                "artist",
                ""
            ),
            "produced_by",
            "Producer",
            producer_profile.get(
                "url",
                ""
            ),
        )

    # --------------------------------------------------------
    # Album / label / release / duration / genre / keywords.
    # --------------------------------------------------------

    album_name = ""

    album = soup.select_one(
        '[itemprop="inAlbum"] [itemprop="name"]'
    )

    if album:

        album_name = clean(
            album.get_text(
                " ",
                strip=True
            )
        )

    label = ""

    label_node = soup.select_one(
        '[itemprop="recordLabel"]'
    )

    if label_node:

        label = clean(
            label_node.get_text(
                " ",
                strip=True
            )
        )

    release_year = ""

    date_node = soup.select_one(
        '[itemprop="datePublished"]'
    )

    if date_node:

        release_year = clean(
            date_node.get(
                "content",
                "",
            )
        )

    duration = ""

    duration_node = soup.select_one(
        '[itemprop="duration"]'
    )

    if duration_node:

        duration = clean(
            duration_node.get(
                "content",
                "",
            )
            or duration_node.get_text(
                " ",
                strip=True
            )
        )

    # Preserve WhoSampled's original ISO-8601 value while also
    # producing milliseconds for direct Spotify comparison.
    duration_iso = duration

    duration_ms = parse_iso_duration_ms(
        duration_iso
    )

    genres = []

    for node in soup.select(
        '[itemprop="genre"]'
    ):

        value = clean(
            node.get(
                "content",
                ""
            )
            or node.get_text(
                " ",
                strip=True
            )
        )

        if (
            value
            and value not in genres
        ):
            genres.append(
                value
            )

    keywords = []

    for node in soup.select(
        '[itemprop="keywords"]'
    ):

        value = clean(
            node.get(
                "content",
                ""
            )
            or node.get_text(
                " ",
                strip=True
            )
        )

        if value:
            for keyword in value.split(","):

                keyword = clean(
                    keyword
                )

                if (
                    keyword
                    and keyword not in keywords
                ):
                    keywords.append(
                        keyword
                    )

    # --------------------------------------------------------
    # Artwork.
    # --------------------------------------------------------

    source_thumbnail_url = ""

    thumbnail = soup.select_one(
        '[itemprop="thumbnailUrl"]'
    )

    if thumbnail:

        srcset = clean(
            thumbnail.get(
                "srcset",
                ""
            )
        )

        # Prefer the highest-resolution srcset candidate.
        if srcset:

            candidates = []

            for part in srcset.split(","):

                pieces = part.strip().split()

                if not pieces:
                    continue

                image_url = pieces[0]
                descriptor = (
                    pieces[1]
                    if len(pieces) > 1
                    else ""
                )

                candidates.append(
                    (
                        descriptor,
                        image_url,
                    )
                )

            if candidates:

                source_thumbnail_url = (
                    candidates[-1][1]
                )

        if not source_thumbnail_url:

            source_thumbnail_url = clean(
                thumbnail.get(
                    "src",
                    ""
                )
            )

    if source_thumbnail_url:

        source_thumbnail_url = urljoin(
            BASE,
            source_thumbnail_url,
        )

    # --------------------------------------------------------
    # YouTube recording.
    #
    # WhoSampled embeds the recording using:
    #
    #   .youtube-placeholder[data-id]
    #
    # The data-id is the actual YouTube video ID.
    # --------------------------------------------------------

    source_youtube_video_id = ""

    youtube_node = soup.select_one(
        ".youtube-placeholder[data-id]"
    )

    if youtube_node:

        source_youtube_video_id = clean(
            youtube_node.get(
                "data-id",
                ""
            )
        )

    source_youtube_url = ""

    if source_youtube_video_id:

        source_youtube_url = (
            "https://www.youtube.com/watch?v="
            + source_youtube_video_id
        )

    source_youtube_thumbnail_url = ""

    if source_youtube_video_id:

        source_youtube_thumbnail_url = (
            "https://i.ytimg.com/vi/"
            + source_youtube_video_id
            + "/hqdefault.jpg"
        )

    return {
        "source_title":
            source_title,

        "source_url":
            source_url,

        "source_artists":
            ", ".join(
                source_artists
            ),

        "source_artist_profiles":
            json.dumps(
                source_artist_profiles,
                ensure_ascii=False,
            ),

        "source_producers":
            ", ".join(
                producers
            ),

        "source_credits":
            json.dumps(
                credits,
                ensure_ascii=False,
            ),

        "source_album":
            album_name,

        "source_label":
            label,

        "source_release_year":
            release_year,

        # Backward-compatible raw field.
        "source_duration":
            duration_iso,

        # Explicit source representation.
        "source_duration_iso":
            duration_iso,

        # Normalized value directly comparable with
        # Spotify's duration_ms.
        "source_duration_ms":
            duration_ms,

        "source_genre":
            ", ".join(
                genres
            ),

        "source_keywords":
            ", ".join(
                keywords
            ),

        "source_thumbnail_url":
            source_thumbnail_url,

        "source_youtube_video_id":
            source_youtube_video_id,

        "source_youtube_url":
            source_youtube_url,

        "source_youtube_thumbnail_url":
            source_youtube_thumbnail_url,
    }


def extract_relationships(soup, source):
    rows = []

    links = soup.select(
        'a[href*="/sample/"], '
        'a[href*="/cover/"], '
        'a[href*="/interpolation/"], '
        'a[href*="/remix/"]'
    )

    for relationship_link in links:

        href = relationship_link.get("href")

        if not href:
            continue

        relationship_url = urljoin(
            BASE,
            href
        )

        relationship_type = (
            relationship_type_from_url(
                relationship_url
            )
        )

        if not relationship_type:
            continue

        row = relationship_link.find_parent("tr")

        if row is None:
            continue

        track_link = row.select_one(
            "a.trackName"
        )

        related_track = ""

        if track_link:
            related_track = clean(
                track_link.get_text(
                    " ",
                    strip=True
                )
            )

        related_artist = ""

        for a in row.select("a[href]"):

            a_href = a.get("href", "")
            a_text = clean(
                a.get_text(
                    " ",
                    strip=True
                )
            )

            if (
                a_text
                and a_text != related_track
                and is_artist_link(a_href)
            ):
                related_artist = a_text
                break

        cells = row.select("td")

        cell_texts = [
            clean(
                cell.get_text(
                    " ",
                    strip=True
                )
            )
            for cell in cells
        ]

        year = ""

        for text in cell_texts:
            if (
                text.isdigit()
                and len(text) == 4
                and 1800 <= int(text) <= 2100
            ):
                year = text
                break

        detail = ""

        for text in cell_texts:

            if not text:
                continue

            if text in {
                related_track,
                related_artist,
                year,
            }:
                continue

            if text.startswith("See "):
                continue

            detail = text
            break

        rows.append({
            **source,
            "relationship_type": relationship_type,
            "related_track": related_track,
            "related_artist": related_artist,
            "year": year,
            "whosampled_relationship_url": relationship_url,
            "detail": detail,
        })

    # Deduplicate.
    unique = {}

    for row in rows:

        key = (
            row["relationship_type"],
            row["related_track"],
            row["related_artist"],
            row["whosampled_relationship_url"],
        )

        unique[key] = row

    return list(unique.values())



def extract_secondary_pages(soup):
    """
    Discover secondary WhoSampled relationship-list pages
    exposed by 'see all' links on a track page.

    Returns a list of dictionaries with:
      - relationship_section
      - url
      - link_text
    """

    secondary = []
    seen = set()

    # Relationship-section headings such as:
    # "Sampled in 5 songs"
    # "Covered in 1 song"
    for header in soup.select(
        "h3.section-header-title"
    ):

        section_title = clean(
            header.get_text(
                " ",
                strip=True
            )
        )

        # Find the containing section.
        container = header

        for _ in range(5):
            container = container.parent

            if container is None:
                break

            if container.select_one(
                "a[href]"
            ):
                break

        if container is None:
            continue

        for a in container.find_all(
            "a",
            href=True
        ):

            href = a.get("href", "").strip()

            link_text = clean(
                a.get_text(
                    " ",
                    strip=True
                )
            )

            # WhoSampled's relationship-list links use
            # paths such as /sampled/ or /covered/.
            is_secondary = (
                "/sampled/" in href
                or "/samples/" in href
                or "/covered/" in href
                or "/covers/" in href
                or "/interpolat" in href
                or "/remix" in href
            )

            # Also accept an explicit "see all" link.
            if link_text.casefold() == "see all":
                is_secondary = True

            if not is_secondary:
                continue

            absolute_url = urljoin(
                BASE,
                href
            )

            key = (
                section_title,
                absolute_url
            )

            if key in seen:
                continue

            seen.add(key)

            secondary.append({
                "relationship_section":
                    section_title,
                "url":
                    absolute_url,
                "link_text":
                    link_text,
            })

    return secondary



def main():

    parser = argparse.ArgumentParser(
        description="Parse a saved WhoSampled track HTML page."
    )

    parser.add_argument(
        "html_file",
        help="Path to saved WhoSampled HTML"
    )

    parser.add_argument(
        "--output",
        help="Output CSV filename"
    )

    args = parser.parse_args()

    input_path = Path(args.html_file)

    if not input_path.exists():
        raise FileNotFoundError(
            f"HTML file not found: {input_path}"
        )

    if args.output:
        output_path = Path(args.output)
    else:
        output_path = input_path.with_name(
            input_path.stem + "_relationships.csv"
        )

    html = input_path.read_text(
        encoding="utf-8",
        errors="ignore"
    )

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    source = extract_source_metadata(
        soup
    )

    relationships = extract_relationships(
        soup,
        source
    )

    fieldnames = [
        "source_title",
        "source_url",
        "source_artists",
        "source_artist_profiles",
        "source_producers",
        "source_credits",
        "source_album",
        "source_label",
        "source_release_year",
        "source_duration",
        "source_genre",
        "source_keywords",
        "source_thumbnail_url",
        "source_youtube_video_id",
        "source_youtube_url",
        "source_youtube_thumbnail_url",
        "relationship_type",
        "related_track",
        "related_artist",
        "year",
        "whosampled_relationship_url",
        "detail",
    ]

    with output_path.open(
        "w",
        newline="",
        encoding="utf-8"
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames
        )

        writer.writeheader()
        writer.writerows(relationships)

    print()
    print("WHOSAMPLED TRACK PARSER")
    print("=======================")
    print("Input:", input_path)
    print("Source:", source["source_title"])
    print("Artists:", source["source_artists"])
    print("Album:", source["source_album"])
    print("Label:", source["source_label"])
    print("Relationships:", len(relationships))
    print("Output:", output_path)
    print()

    for row in relationships:
        print(
            f"{row['relationship_type']:14} | "
            f"{row['related_track']:35} | "
            f"{row['related_artist']:30} | "
            f"{row['year']:4} | "
            f"{row['detail']}"
        )


if __name__ == "__main__":
    main()
