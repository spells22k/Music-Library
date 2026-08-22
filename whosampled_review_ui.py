import json
import base64
import mimetypes
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
from bs4 import BeautifulSoup
from flask import (
    Flask,
    redirect,
    render_template_string,
    request,
    url_for,
)


HTML_TEMPLATE = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>WhoSampled Review</title>
<style>
body {
    font-family:
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;
    background: #f3f4f6;
    margin: 0;
}
.wrap {
    max-width: 1050px;
    margin: 28px auto;
    padding: 0 22px;
}
.progress {
    color: #666;
    font-size: 14px;
    margin-bottom: 12px;
}
.card {
    background: white;
    border-radius: 16px;
    padding: 28px;
    box-shadow: 0 6px 28px rgba(0,0,0,.08);
}
h1 {
    margin-top: 0;
}
.panel {
    border: 1px solid #e3e3e3;
    border-radius: 12px;
    padding: 22px;
}
.artwork-wrap {
    display: flex;
    justify-content: center;
    margin: 4px 0 20px;
}
.artwork {
    width: 240px;
    height: 240px;
    object-fit: cover;
    border-radius: 10px;
    box-shadow: 0 4px 18px rgba(0,0,0,.14);
    background: #eee;
}
.label {
    color: #666;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: .08em;
    margin-top: 16px;
}
.value {
    font-size: 25px;
    font-weight: 750;
    margin-top: 4px;
    line-height: 1.25;
}
.artist {
    font-size: 18px;
    color: #555;
    margin-top: 5px;
}
.meta {
    margin-top: 14px;
    color: #555;
    line-height: 1.6;
}
.links {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    margin-top: 20px;
}
.link {
    display: inline-block;
    padding: 10px 13px;
    border-radius: 8px;
    text-decoration: none;
    background: #eef4ff;
    color: #174ea6;
    font-weight: 650;
}
.reason {
    margin-top: 20px;
    padding: 14px;
    background: #f5f5f5;
    border-radius: 10px;
    color: #444;
}
.actions {
    display: flex;
    gap: 12px;
    margin-top: 28px;
}
button {
    flex: 1;
    border: 0;
    border-radius: 10px;
    padding: 17px 22px;
    font-size: 17px;
    font-weight: 800;
    cursor: pointer;
}
.accept {
    background: #d9f7df;
}
.reject {
    background: #ffdede;
}
.unresolved {
    background: #fff0c9;
}
.hint {
    text-align: center;
    color: #666;
    margin-top: 12px;
    font-size: 14px;
}
.complete {
    text-align: center;
    padding: 60px 20px;
}
.complete h1 {
    color: #137333;
}
@media (max-width: 760px) {
    .actions {
        flex-direction: column;
    }
}
</style>
</head>

<body>
<div class="wrap">

    <div class="progress">
        Candidate {{ index }} of {{ total }}
    </div>

    <div class="card">

        <div class="panel">

            <h1>WhoSampled Candidate Review</h1>

            {% if row.get("whosampled_thumbnail_url") %}
            <div class="artwork-wrap">
                <img
                    class="artwork"
                    src="{{ row.get('whosampled_thumbnail_data_uri') }}"
                    alt="WhoSampled artwork">
            </div>
            {% endif %}

            <div class="label">
                Playlist track
            </div>

            <div class="value">
                {{ row.get("playlist_title", "") }}
            </div>

            <div class="artist">
                {{ row.get("playlist_artists", "") }}
            </div>

            <div class="label">
                WhoSampled candidate
            </div>

            <div class="value">
                {{ row.get("whosampled_title", "") }}
            </div>

            <div class="artist">
                {{ row.get("whosampled_artists", "") }}
            </div>

            <div class="reason">

                <strong>Resolution source:</strong>
                {{ row.get("match_method", "") }}

                <br>

                <strong>WhoSampled URL:</strong>
                {{ row.get("whosampled_url", "") }}

                {% if row.get("archived_html") %}
                <br>
                <strong>Archived HTML:</strong>
                {{ row.get("archived_html", "") }}
                {% endif %}

            </div>

            <div class="links">

                {% if row.get("whosampled_url") %}
                <a
                    class="link"
                    href="{{ row.get('whosampled_url') }}"
                    target="_blank"
                    rel="noopener">
                    Open WhoSampled
                </a>
                {% endif %}

                {% if row.get("whosampled_youtube_url") %}
                <a
                    class="link"
                    href="{{ row.get('whosampled_youtube_url') }}"
                    target="_blank"
                    rel="noopener">
                    ▶ Listen on YouTube
                </a>
                {% endif %}

            </div>

            <form method="post" action="{{ url_for('decide') }}">

                <input
                    type="hidden"
                    name="decision"
                    id="decision">

                <div class="actions">

                    <button
                        class="accept"
                        type="submit"
                        onclick="
                            document.getElementById(
                                'decision'
                            ).value='y'
                        ">
                        ACCEPT (Y)
                    </button>

                    <button
                        class="reject"
                        type="submit"
                        onclick="
                            document.getElementById(
                                'decision'
                            ).value='n'
                        ">
                        REJECT (N)
                    </button>

                    <button
                        class="unresolved"
                        type="submit"
                        onclick="
                            document.getElementById(
                                'decision'
                            ).value='m'
                        ">
                        UNRESOLVED (M)
                    </button>

                </div>

            </form>

            <div class="hint">
                Press Y, N, or M.
                Links open in a separate tab.
            </div>

        </div>

    </div>
</div>

<script>
document.addEventListener(
    "keydown",
    function(event) {

        if (
            event.target &&
            (
                event.target.tagName === "INPUT" ||
                event.target.tagName === "TEXTAREA"
            )
        ) {
            return;
        }

        const key =
            event.key.toLowerCase();

        if (
            key === "y" ||
            key === "n" ||
            key === "m"
        ) {
            document.getElementById(
                "decision"
            ).value = key;

            document.forms[0].submit();
        }
    }
);
</script>

</body>
</html>
"""



def local_image_data_uri(path):
    """
    Convert an archived local image into a data URI.

    The review browser therefore reads the image from the local
    archive instead of requesting the original WhoSampled CDN URL.
    """

    if not path:
        return ""

    path = Path(path)

    if not path.exists():
        return ""

    try:
        data = path.read_bytes()
    except Exception:
        return ""

    mime_type, _ = mimetypes.guess_type(
        path.name
    )

    if not mime_type:
        mime_type = "image/png"

    encoded = base64.b64encode(
        data
    ).decode(
        "ascii"
    )

    return (
        f"data:{mime_type};base64,"
        f"{encoded}"
    )


def clean(value):
    if value is None:
        return ""

    if pd.isna(value):
        return ""

    return str(value).strip()


def normalize_url(value):
    value = clean(value)

    if not value:
        return ""

    value = value.rstrip("/")

    return value


def open_chrome(url):
    subprocess.Popen(
        [
            "open",
            "-a",
            "Google Chrome",
            url,
        ]
    )


def looks_like_youtube_url(url):
    url = clean(url)

    if not url:
        return False

    host = urlparse(url).netloc.lower()

    return (
        "youtube.com" in host
        or "youtu.be" in host
        or "youtube-nocookie.com" in host
    )


def normalize_youtube_url(url):
    """
    Keep only a usable YouTube watch/embed URL.
    Do not invent a YouTube URL when the archive does not
    contain one.
    """

    url = clean(url)

    if not looks_like_youtube_url(url):
        return ""

    return url


def extract_archive_media(
    html_file,
    expected_url,
):
    """
    Extract artwork and YouTube evidence from an already-saved
    WhoSampled HTML file.

    This function performs NO network request.

    Priority for artwork:
        1. og:image
        2. twitter:image
        3. relevant image tags

    Priority for YouTube:
        1. explicit YouTube hrefs
        2. YouTube URLs embedded elsewhere in the HTML
    """

    result = {
        "thumbnail_url": "",
        "youtube_url": "",
        "canonical_url": "",
    }

    html_file = Path(html_file)

    if not html_file.exists():
        return result

    try:
        html = html_file.read_text(
            encoding="utf-8",
            errors="ignore",
        )
    except Exception:
        return result

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    # --------------------------------------------------------
    # Canonical URL.
    # --------------------------------------------------------

    canonical = soup.find(
        "link",
        rel=lambda value: (
            value
            and (
                "canonical" in value
                if isinstance(value, str)
                else "canonical" in value
            )
        ),
    )

    if canonical:

        result[
            "canonical_url"
        ] = clean(
            canonical.get("href")
        )

    # Also inspect og:url.
    if not result["canonical_url"]:

        og_url = soup.find(
            "meta",
            attrs={
                "property": "og:url"
            },
        )

        if og_url:

            result[
                "canonical_url"
            ] = clean(
                og_url.get("content")
            )

    # --------------------------------------------------------
    # Artwork.
    # --------------------------------------------------------

    image_selectors = [
        (
            "meta",
            {
                "property": "og:image"
            },
        ),
        (
            "meta",
            {
                "name": "twitter:image"
            },
        ),
        (
            "meta",
            {
                "property": "twitter:image"
            },
        ),
    ]

    for tag_name, attrs in image_selectors:

        tag = soup.find(
            tag_name,
            attrs=attrs,
        )

        if not tag:
            continue

        image_url = clean(
            tag.get("content")
        )

        if image_url:
            result[
                "thumbnail_url"
            ] = image_url
            break

    if not result["thumbnail_url"]:

        for image in soup.select(
            "img[src]"
        ):

            src = clean(
                image.get("src")
            )

            if not src:
                continue

            lowered = src.lower()

            if any(
                marker in lowered
                for marker in (
                    "album",
                    "cover",
                    "artwork",
                    "track",
                    "thumb",
                    "image",
                )
            ):

                result[
                    "thumbnail_url"
                ] = src

                break

    # --------------------------------------------------------
    # Direct YouTube hrefs.
    # --------------------------------------------------------

    for link in soup.select(
        "a[href]"
    ):

        href = clean(
            link.get("href")
        )

        if looks_like_youtube_url(
            href
        ):

            result[
                "youtube_url"
            ] = normalize_youtube_url(
                href
            )

            break

    # --------------------------------------------------------
    # Embedded YouTube URLs.
    # --------------------------------------------------------

    if not result["youtube_url"]:

        patterns = [
            r'https?://(?:www\.)?youtube\.com/watch\?[^"\'>\s]+',
            r'https?://(?:www\.)?youtube\.com/embed/[^"\'>\s]+',
            r'https?://youtu\.be/[^"\'>\s]+',
        ]

        for pattern in patterns:

            match = re.search(
                pattern,
                html,
                flags=re.IGNORECASE,
            )

            if not match:
                continue

            result[
                "youtube_url"
            ] = normalize_youtube_url(
                match.group(0)
            )

            break

    # --------------------------------------------------------
    # If canonical URL was found, confirm it corresponds to
    # the candidate whenever possible.
    #
    # We don't reject the media simply because the canonical
    # string formatting differs; normalize both sides first.
    # --------------------------------------------------------

    expected = normalize_url(
        expected_url
    )

    actual = normalize_url(
        result["canonical_url"]
    )

    if (
        expected
        and actual
        and expected != actual
    ):
        # The file may contain a normalized/redirected URL.
        # Keep the extracted media but do not manufacture any
        # stronger verification claim.
        pass

    return result


def find_archived_html(
    match_file,
    whosampled_url,
):
    """
    Find the archived HTML corresponding to a WhoSampled URL.

    Matching is based on canonical/og:url from the archived HTML,
    not on the filename.
    """

    match_file = Path(
        match_file
    )

    archive_dir = (
        match_file.parent
        / "whosampled_pages"
    )

    if not archive_dir.exists():
        return None

    expected = normalize_url(
        whosampled_url
    )

    if not expected:
        return None

    for html_file in archive_dir.glob(
        "*.html"
    ):

        try:

            html = html_file.read_text(
                encoding="utf-8",
                errors="ignore",
            )

        except Exception:
            continue

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        canonical = ""

        tag = soup.find(
            "link",
            rel=lambda value: (
                value
                and (
                    "canonical" in value
                    if isinstance(value, str)
                    else "canonical" in value
                )
            ),
        )

        if tag:
            canonical = clean(
                tag.get("href")
            )

        if not canonical:

            tag = soup.find(
                "meta",
                attrs={
                    "property": "og:url"
                },
            )

            if tag:
                canonical = clean(
                    tag.get("content")
                )

        if normalize_url(
            canonical
        ) == expected:

            return html_file

    return None


def prepare_review_candidate(
    row,
    match_file,
):
    """
    Enrich one review candidate using ONLY local data.

    No WhoSampled request is made here.
    """

    row = dict(row)

    whosampled_url = clean(
        row.get("whosampled_url")
    )

    archive_file = find_archived_html(
        match_file,
        whosampled_url,
    )

    media = {
        "thumbnail_url": "",
        "youtube_url": "",
        "canonical_url": "",
    }

    if archive_file is not None:

        media = extract_archive_media(
            archive_file,
            whosampled_url,
        )

        row[
            "archived_html"
        ] = str(
            archive_file
        )

    else:

        row[
            "archived_html"
        ] = ""

    row[
        "whosampled_thumbnail_url"
    ] = media.get(
        "thumbnail_url",
        "",
    )

    row[
        "whosampled_thumbnail_path"
    ] = ""

    row[
        "whosampled_thumbnail_data_uri"
    ] = ""

    thumbnail_path = (
        match_file.parent
        / "whosampled_media"
        / (
            Path(
                clean(
                    row.get(
                        "archived_html",
                        "",
                    )
                )
            ).stem
            + ".png"
            if row.get(
                "archived_html",
                "",
            )
            else ""
        )
    )

    if thumbnail_path.exists():

        row[
            "whosampled_thumbnail_path"
        ] = str(
            thumbnail_path
        )

        row[
            "whosampled_thumbnail_data_uri"
        ] = local_image_data_uri(
            thumbnail_path
        )

    row[
        "whosampled_youtube_url"
    ] = media.get(
        "youtube_url",
        "",
    )

    return row


def run_whosampled_review(
    match_file,
    review_file,
):
    """
    Pause the pipeline and ask the contributor to resolve all
    WhoSampled pages that Phase 1 classified as 'review'.

    Y = accepted page
    N = rejected page
    M = unresolved / defer

    Decisions are checkpointed after every candidate.

    Artwork and YouTube links are extracted from already-saved
    HTML. The review UI itself never requests WhoSampled.
    """

    match_file = Path(
        match_file
    )

    review_file = Path(
        review_file
    )

    df = pd.read_csv(
        match_file
    )

    if "match_status" not in df.columns:
        raise RuntimeError(
            "matched_tracks.csv is missing match_status."
        )

    for column in [
        "whosampled_review_decision",
        "whosampled_reviewed_at",
    ]:

        if column not in df.columns:
            df[column] = ""

    review_candidates = df[
        df["match_status"].eq(
            "review"
        )
        &
        df[
            "whosampled_url"
        ].fillna("").ne("")
    ].copy()

    if review_candidates.empty:

        print()
        print("=" * 80)
        print("WHO SAMPLED REVIEW")
        print("=" * 80)
        print(
            "No WhoSampled review candidates."
        )
        print()

        return df

    existing_decisions = {}

    if review_file.exists():

        try:

            existing_decisions = json.loads(
                review_file.read_text(
                    encoding="utf-8"
                )
            )

        except Exception:

            existing_decisions = {}

    decisions = (
        existing_decisions.copy()
    )

    app = Flask(
        __name__
    )

    pending = []

    for row in (
        review_candidates
        .to_dict("records")
    ):

        track_key = str(
            row.get(
                "spotify_track_id",
                ""
            )
        )

        if track_key in decisions:
            continue

        pending.append(
            prepare_review_candidate(
                row,
                match_file,
            )
        )

    if not pending:

        print()
        print("=" * 80)
        print("WHO SAMPLED REVIEW")
        print("=" * 80)
        print(
            "All WhoSampled review candidates "
            "already resolved."
        )

    else:

        print()
        print("=" * 80)
        print(
            "WHO SAMPLED REVIEW CANDIDATES:",
            len(pending),
        )
        print(
            "Y = accept | N = reject | M = unresolved"
        )
        print(
            "Pipeline is paused until review is complete."
        )

    index_ref = {
        "value": 0
    }

    def current_row():

        idx = index_ref[
            "value"
        ]

        if idx >= len(
            pending
        ):
            return None

        return pending[
            idx
        ]

    @app.route(
        "/",
        methods=["GET"]
    )
    def index():

        row = current_row()

        if row is None:

            return redirect(
                url_for(
                    "complete"
                )
            )

        # Best-effort display fields.
        #
        # The match CSV uses different names depending on the
        # exact pipeline version, so keep the UI tolerant.
        row.setdefault(
            "playlist_title",
            row.get(
                "spotify_title",
                ""
            ),
        )

        row.setdefault(
            "playlist_artists",
            row.get(
                "spotify_artists",
                ""
            ),
        )

        row.setdefault(
            "whosampled_title",
            row.get(
                "title",
                row.get(
                    "spotify_title",
                    ""
                ),
            ),
        )

        row.setdefault(
            "whosampled_artists",
            row.get(
                "source_artists",
                row.get(
                    "spotify_artists",
                    ""
                ),
            ),
        )

        return render_template_string(
            HTML_TEMPLATE,
            row=row,
            index=(
                index_ref["value"]
                + 1
            ),
            total=len(pending),
        )

    @app.route(
        "/decide",
        methods=["POST"]
    )
    def decide():

        row = current_row()

        if row is None:

            return redirect(
                url_for(
                    "complete"
                )
            )

        decision = (
            request.form.get(
                "decision",
                ""
            )
            .strip()
            .lower()
        )

        if decision not in {
            "y",
            "n",
            "m",
        }:

            return redirect(
                url_for(
                    "index"
                )
            )

        track_key = str(
            row.get(
                "spotify_track_id",
                ""
            )
        )

        mapped = {
            "y": "accepted",
            "n": "rejected",
            "m": "unresolved",
        }

        decisions[
            track_key
        ] = {
            "decision":
                mapped[
                    decision
                ],

            "reviewed_at":
                datetime.now(
                    timezone.utc
                ).isoformat(),

            "whosampled_url":
                row.get(
                    "whosampled_url"
                ),

            "archived_html":
                row.get(
                    "archived_html",
                    "",
                ),

            "whosampled_thumbnail_url":
                row.get(
                    "whosampled_thumbnail_url",
                    "",
                ),

            "whosampled_youtube_url":
                row.get(
                    "whosampled_youtube_url",
                    "",
                ),
        }

        review_file.write_text(
            json.dumps(
                decisions,
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        index_ref[
            "value"
        ] += 1

        return redirect(
            url_for(
                "index"
            )
        )

    @app.route(
        "/complete"
    )
    def complete():

        return """
        <!doctype html>
        <html>
        <head>
        <meta charset="utf-8">
        <title>WhoSampled Review Complete</title>
        </head>
        <body style="
            font-family:
                -apple-system,
                BlinkMacSystemFont,
                'Segoe UI',
                sans-serif;
            max-width:700px;
            margin:60px auto;
            padding:0 24px;">
            <h1>
                WHO SAMPLED REVIEW COMPLETE
            </h1>
            <p>
                The pipeline can resume.
            </p>
        </body>
        </html>
        """

    from werkzeug.serving import (
        make_server
    )

    server = make_server(
        "127.0.0.1",
        0,
        app,
    )

    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
    )

    thread.start()

    url = (
        f"http://127.0.0.1:"
        f"{server.server_port}/"
    )

    print()
    print(
        "Browser:",
        url,
    )
    print(
        "Y = accept | N = reject | M = unresolved"
    )
    print(
        "Pipeline is paused until review is complete."
    )

    open_chrome(
        url
    )

    while (
        index_ref["value"]
        < len(pending)
    ):
        time.sleep(
            0.25
        )

    server.shutdown()

    thread.join(
        timeout=2
    )

    for (
        track_key,
        result
    ) in decisions.items():

        mask = (
            df[
                "spotify_track_id"
            ]
            .astype(str)
            == str(track_key)
        )

        if not mask.any():
            continue

        decision = result.get(
            "decision"
        )

        df.loc[
            mask,
            "whosampled_review_decision"
        ] = decision

        df.loc[
            mask,
            "whosampled_reviewed_at"
        ] = result.get(
            "reviewed_at",
            "",
        )

        if decision == "accepted":

            df.loc[
                mask,
                "match_status"
            ] = "matched"

        elif decision == "rejected":

            df.loc[
                mask,
                "match_status"
            ] = "rejected"

        elif decision == "unresolved":

            df.loc[
                mask,
                "match_status"
            ] = "unresolved"

    df.to_csv(
        match_file,
        index=False,
        encoding="utf-8",
    )

    print()
    print(
        "WHO SAMPLED REVIEW COMPLETE."
    )

    print(
        pd.Series(
            [
                value.get(
                    "decision"
                )
                for value in (
                    decisions.values()
                )
            ]
        ).value_counts()
    )

    return df
