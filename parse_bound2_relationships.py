import csv
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup


INPUT = Path("bound_2_playwright.html")
OUTPUT = Path("bound_2_relationships.csv")
BASE = "https://www.whosampled.com"

SOURCE_TRACK = "Bound 2"


def clean(text):
    return " ".join(text.split()) if text else ""


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


def looks_like_artist_link(href):
    if not href:
        return False

    blocked = [
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
    ]

    href_lower = href.lower()

    if any(x in href_lower for x in blocked):
        return False

    # Artist links look like /Artist-Name/
    return (
        href.startswith("/")
        and href.count("/") == 2
    )


html = INPUT.read_text(
    encoding="utf-8",
    errors="ignore"
)

soup = BeautifulSoup(
    html,
    "html.parser"
)


rows = []


# ------------------------------------------------------------
# Find every relationship link directly from the HTML.
#
# The relationship URL itself tells us whether it is:
# sample / cover / interpolation / remix.
# ------------------------------------------------------------

for relationship_link in soup.select(
    'a[href*="/sample/"], '
    'a[href*="/cover/"], '
    'a[href*="/interpolation/"], '
    'a[href*="/remix/"]'
):

    href = relationship_link.get("href")

    if not href:
        continue

    relationship_url = urljoin(
        BASE,
        href
    )

    relationship_type = relationship_type_from_url(
        relationship_url
    )

    if not relationship_type:
        continue


    # --------------------------------------------------------
    # The parent row contains the structured relationship data.
    # --------------------------------------------------------

    row = relationship_link.find_parent("tr")

    if row is None:
        continue


    # --------------------------------------------------------
    # Related track
    # --------------------------------------------------------

    track_link = row.select_one(
        "a.trackName"
    )

    related_track = (
        clean(track_link.get_text(" ", strip=True))
        if track_link
        else ""
    )


    # --------------------------------------------------------
    # Related artist
    #
    # Look for artist-style links in the same row, excluding
    # the relationship link and unrelated pages.
    # --------------------------------------------------------

    related_artist = ""

    artist_candidates = []

    for a in row.select("a[href]"):

        a_href = a.get("href", "")
        a_text = clean(
            a.get_text(" ", strip=True)
        )

        if (
            a_href
            and a_text
            and looks_like_artist_link(a_href)
            and a_text != related_track
        ):
            artist_candidates.append(a_text)

    if artist_candidates:
        related_artist = artist_candidates[-1]


    # --------------------------------------------------------
    # Year / other detail
    # --------------------------------------------------------

    cells = row.select("td")

    cell_texts = [
        clean(
            cell.get_text(" ", strip=True)
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

    # Keep useful relationship descriptors such as:
    # Multiple Elements
    # Vocals / Lyrics
    # Bass
    # Drums
    # etc.
    for text in cell_texts:

        if not text:
            continue

        if text == related_track:
            continue

        if text == related_artist:
            continue

        if text == year:
            continue

        # Ignore obvious navigation.
        if text.startswith("See "):
            continue

        detail = text
        break


    rows.append({
        "source_track": SOURCE_TRACK,
        "relationship_type": relationship_type,
        "related_track": related_track,
        "related_artist": related_artist,
        "year": year,
        "whosampled_relationship_url": relationship_url,
        "detail": detail,
    })


# ------------------------------------------------------------
# Deduplicate relationship records.
# ------------------------------------------------------------

unique = {}

for row in rows:

    key = (
        row["relationship_type"],
        row["related_track"],
        row["related_artist"],
        row["whosampled_relationship_url"],
    )

    unique[key] = row


rows = list(unique.values())


# ------------------------------------------------------------
# Save CSV
# ------------------------------------------------------------

with OUTPUT.open(
    "w",
    newline="",
    encoding="utf-8"
) as f:

    writer = csv.DictWriter(
        f,
        fieldnames=[
            "source_track",
            "relationship_type",
            "related_track",
            "related_artist",
            "year",
            "whosampled_relationship_url",
            "detail",
        ],
    )

    writer.writeheader()
    writer.writerows(rows)


print()
print("BOUND 2 RELATIONSHIP PARSER")
print("============================")
print("Input:", INPUT)
print("Relationships:", len(rows))
print("Output:", OUTPUT)
print()

for row in rows:

    print(
        f"{row['relationship_type']:14} | "
        f"{row['related_track']:35} | "
        f"{row['related_artist']:30} | "
        f"{row['year']:4} | "
        f"{row['detail']}"
    )
