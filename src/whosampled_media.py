import base64
import mimetypes
import re
from pathlib import Path

from bs4 import BeautifulSoup


def safe_filename(value):
    value = str(value or "").strip()

    if not value:
        value = "track"

    value = re.sub(
        r'[\\/:*?"<>|]+',
        "_",
        value,
    )

    value = re.sub(
        r"\s+",
        "_",
        value,
    )

    return value[:180]


def extract_track_artwork_candidates(page):
    """
    Inspect the already-loaded WhoSampled page.

    This does not issue any HTTP requests.

    Returns image URLs in priority order.
    """

    html = page.content()

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    candidates = []

    # Prefer OpenGraph artwork.
    for attrs in (
        {"property": "og:image"},
        {"name": "twitter:image"},
        {"property": "twitter:image"},
    ):

        tag = soup.find(
            "meta",
            attrs=attrs,
        )

        if tag:

            value = (
                tag.get("content")
                or ""
            ).strip()

            if value and value not in candidates:
                candidates.append(value)

    # Track-page image candidates.
    for image in soup.select(
        "img[src]"
    ):

        src = (
            image.get("src")
            or ""
        ).strip()

        if not src:
            continue

        lowered = src.lower()

        if any(
            marker in lowered
            for marker in (
                "track_images",
                "track_images_200",
                "album",
                "cover",
                "artwork",
            )
        ):

            if src not in candidates:
                candidates.append(src)

    return candidates


def capture_rendered_artwork(
    page,
    title,
    output_dir,
):
    """
    Try to capture artwork from the already-rendered page.

    IMPORTANT:
    This uses the existing Playwright page. It does not
    make a separate WhoSampled request.

    The first successfully rendered candidate is saved.

    Returns a metadata dictionary.
    """

    output_dir = Path(
        output_dir
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    candidates = extract_track_artwork_candidates(
        page
    )

    if not candidates:
        return {
            "whosampled_thumbnail_url": "",
            "whosampled_thumbnail_path": "",
            "whosampled_thumbnail_status":
                "unavailable",
        }

    filename = (
        safe_filename(title)
        + ".png"
    )

    destination = (
        output_dir
        / filename
    )

    # Try actual rendered <img> elements first.
    for selector in (
        "img[src*='track_images_200']",
        "img[src*='track_images']",
        "img[src*='album']",
        "img[src*='cover']",
        "img[src*='artwork']",
    ):

        locator = page.locator(
            selector
        ).first

        try:

            if locator.count() == 0:
                continue

            if not locator.is_visible():
                continue

            locator.screenshot(
                path=str(
                    destination
                )
            )

            if destination.exists():

                return {
                    "whosampled_thumbnail_url":
                        candidates[0],
                    "whosampled_thumbnail_path":
                        str(
                            destination
                        ),
                    "whosampled_thumbnail_status":
                        "captured",
                }

        except Exception:
            continue

    return {
        "whosampled_thumbnail_url":
            candidates[0],
        "whosampled_thumbnail_path": "",
        "whosampled_thumbnail_status":
            "unavailable",
    }


def image_file_to_data_uri(path):
    """
    Convert a locally cached image into a data URI so the
    localhost review UI can display it without requesting the
    original WhoSampled image URL.
    """

    if not path:
        return ""

    path = Path(path)

    if not path.exists():
        return ""

    mime_type, _ = mimetypes.guess_type(
        path.name
    )

    if not mime_type:
        mime_type = "image/png"

    try:

        encoded = base64.b64encode(
            path.read_bytes()
        ).decode(
            "ascii"
        )

    except Exception:
        return ""

    return (
        f"data:{mime_type};base64,"
        f"{encoded}"
    )
