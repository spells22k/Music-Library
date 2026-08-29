import json
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import pandas as pd


DECISIONS = {
    "y": "accepted",
    "n": "rejected",
    "m": "unresolved",
}


def clean(value):
    if pd.isna(value):
        return ""
    return str(value)


def normalize_for_lookup(value):
    import re
    import unicodedata

    if value is None:
        return ""

    value = unicodedata.normalize(
        "NFC",
        str(value).strip()
    ).casefold()

    value = value.replace("’", "'")
    value = value.replace("‘", "'")
    value = value.replace("–", "-")
    value = value.replace("—", "-")

    value = re.sub(
        r"[^\w\s'-]",
        " ",
        value,
        flags=re.UNICODE,
    )

    return " ".join(value.split())


def row_key(row):
    return "|".join([
        clean(row.get("whosampled_relationship_url")),
        clean(row.get("related_track")),
        clean(row.get("related_artist")),
        clean(row.get("spotify_track_id")),
    ])


def build_source_spotify_lookup(spotify_file):
    spotify = pd.read_csv(
        spotify_file
    )

    lookup = {}

    for _, row in spotify.iterrows():

        key = (
            normalize_for_lookup(
                row.get("title")
            ),
            normalize_for_lookup(
                row.get("artist_names")
            ),
        )

        lookup[key] = {
            "spotify_track_id":
                clean(row.get("spotify_track_id")),
            "spotify_url":
                clean(row.get("spotify_url")),
            "title":
                clean(row.get("title")),
            "artist_names":
                clean(row.get("artist_names")),
            "album_image_url":
                clean(row.get("album_image_url")),
            "album_name":
                clean(row.get("album_name")),
        }

    return lookup


def spotify_url(track_id):
    track_id = clean(track_id)

    if not track_id:
        return ""

    return (
        "https://open.spotify.com/track/"
        + track_id
    )


def youtube_search_url(title, artists):
    title = clean(title)
    artists = clean(artists)

    if not title:
        return ""

    query = " ".join(
        part
        for part in [
            title,
            artists,
        ]
        if part
    )

    return (
        "https://www.youtube.com/results?search_query="
        + quote_plus(query)
    )


def run_spotify_candidate_review(
    enriched_file,
    spotify_file,
):
    enriched_file = Path(
        enriched_file
    )

    spotify_file = Path(
        spotify_file
    )

    review_file = (
        enriched_file.parent
        / "spotify_candidate_reviews.json"
    )

    df = pd.read_csv(
        enriched_file
    )

    # Ensure review columns exist.
    for column in [
        "spotify_review_decision",
        "spotify_review_note",
        "spotify_reviewed_at",
    ]:
        if column not in df.columns:
            df[column] = ""

    source_lookup = (
        build_source_spotify_lookup(
            spotify_file
        )
    )

    saved_reviews = {}

    if review_file.exists():

        try:
            saved_reviews = json.loads(
                review_file.read_text(
                    encoding="utf-8"
                )
            )
        except Exception:
            saved_reviews = {}

    # Restore previous decisions.
    for i, row in df.iterrows():

        saved = saved_reviews.get(
            row_key(row)
        )

        if not saved:
            continue

        decision = saved.get(
            "decision",
            ""
        )

        if decision not in (
            "accepted",
            "rejected",
            "unresolved",
        ):
            continue

        df.at[
            i,
            "spotify_review_decision"
        ] = decision

        df.at[
            i,
            "spotify_review_note"
        ] = saved.get(
            "note",
            ""
        )

        df.at[
            i,
            "spotify_reviewed_at"
        ] = saved.get(
            "reviewed_at",
            ""
        )

        if decision == "accepted":

            df.at[
                i,
                "spotify_match_status"
            ] = "matched"

            df.at[
                i,
                "spotify_isrc"
            ] = row.get(
                "spotify_candidate_isrc"
            )

        elif decision == "rejected":

            df.at[
                i,
                "spotify_match_status"
            ] = "unmatched"

            df.at[
                i,
                "spotify_isrc"
            ] = None

        else:

            df.at[
                i,
                "spotify_match_status"
            ] = "review"

            df.at[
                i,
                "spotify_isrc"
            ] = None

    def persist():
        df.to_csv(
            enriched_file,
            index=False,
            encoding="utf-8",
        )

        saved = {}

        for _, row in df.iterrows():

            decision = clean(
                row.get(
                    "spotify_review_decision"
                )
            )

            if decision not in (
                "accepted",
                "rejected",
                "unresolved",
            ):
                continue

            saved[
                row_key(row)
            ] = {
                "decision": decision,
                "note": clean(
                    row.get(
                        "spotify_review_note"
                    )
                ),
                "reviewed_at": clean(
                    row.get(
                        "spotify_reviewed_at"
                    )
                ),
            }

        review_file.write_text(
            json.dumps(
                saved,
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def undecided_indices():

        mask = (
            df[
                "spotify_match_status"
            ]
            .fillna("")
            .eq("review")
            &
            df[
                "spotify_review_decision"
            ]
            .fillna("")
            .eq("")
        )

        result = list(
            df.loc[
                mask
            ].index
        )

        result.sort(
            key=lambda idx: float(
                pd.to_numeric(
                    df.at[
                        idx,
                        "spotify_match_score"
                    ],
                    errors="coerce"
                )
                if pd.notna(
                    df.at[
                        idx,
                        "spotify_match_score"
                    ]
                )
                else 0.0
            ),
            reverse=True,
        )

        return result

    def get_candidate():

        indices = undecided_indices()

        if not indices:
            return None

        return indices[0]

    def apply_decision(
        decision,
        note="",
    ):

        idx = get_candidate()

        if idx is None:
            return False

        row = df.loc[
            idx
        ].copy()

        now = datetime.now(
            timezone.utc
        ).isoformat()

        df.at[
            idx,
            "spotify_review_decision"
        ] = decision

        df.at[
            idx,
            "spotify_review_note"
        ] = note

        df.at[
            idx,
            "spotify_reviewed_at"
        ] = now

        if decision == "accepted":

            df.at[
                idx,
                "spotify_match_status"
            ] = "matched"

            df.at[
                idx,
                "spotify_isrc"
            ] = row.get(
                "spotify_candidate_isrc"
            )

        elif decision == "rejected":

            df.at[
                idx,
                "spotify_match_status"
            ] = "unmatched"

            df.at[
                idx,
                "spotify_isrc"
            ] = None

        else:

            df.at[
                idx,
                "spotify_match_status"
            ] = "review"

            df.at[
                idx,
                "spotify_isrc"
            ] = None

        persist()

        return True

    def candidate_payload():

        indices = undecided_indices()

        if not indices:
            return {
                "done": True,
                "remaining": 0,
            }

        idx = indices[0]
        row = df.loc[idx]

        total_review_candidates = int(
            (
                df[
                    "spotify_match_status"
                ]
                .fillna("")
                .eq("review")
            ).sum()
        )

        position = (
            total_review_candidates
            - len(indices)
            + 1
        )

        source_key = (
            normalize_for_lookup(
                row.get(
                    "source_title"
                )
            ),
            normalize_for_lookup(
                row.get(
                    "source_artists"
                )
            ),
        )

        source_spotify = (
            source_lookup.get(
                source_key,
                {}
            )
        )

        source_spotify_url = (
            source_spotify.get(
                "spotify_url"
            )
            or spotify_url(
                source_spotify.get(
                    "spotify_track_id"
                )
            )
        )

        candidate_spotify_url = (
            spotify_url(
                row.get(
                    "spotify_track_id"
                )
            )
        )

        source_youtube_url = youtube_search_url(
            source_spotify.get("title"),
            source_spotify.get("artist_names"),
        )

        candidate_youtube_url = youtube_search_url(
            row.get("spotify_title"),
            row.get("spotify_artist_names"),
        )

        return {
            "done": False,
            "position": position,
            "total": total_review_candidates,

            "source_title":
                clean(
                    row.get(
                        "source_title"
                    )
                ),

            "source_artists":
                clean(
                    row.get(
                        "source_artists"
                    )
                ),

            "source_spotify_url":
                source_spotify_url,

            "source_album_image_url":
                source_spotify.get(
                    "album_image_url",
                    "",
                ),

            "source_youtube_url":
                source_youtube_url,

            "relationship_type":
                clean(
                    row.get(
                        "relationship_type"
                    )
                ),

            "related_track":
                clean(
                    row.get(
                        "related_track"
                    )
                ),

            "related_artist":
                clean(
                    row.get(
                        "related_artist"
                    )
                ),

            "whosampled_relationship_url":
                clean(
                    row.get(
                        "whosampled_relationship_url"
                    )
                ),

            "spotify_title":
                clean(
                    row.get(
                        "spotify_title"
                    )
                ),

            "spotify_artist_names":
                clean(
                    row.get(
                        "spotify_artist_names"
                    )
                ),

            "spotify_album_name":
                clean(
                    row.get(
                        "spotify_album_name"
                    )
                ),

            "spotify_album_release_date":
                clean(
                    row.get(
                        "spotify_album_release_date"
                    )
                ),

            "spotify_match_score":
                row.get(
                    "spotify_match_score"
                ),

            "spotify_candidate_isrc":
                clean(
                    row.get(
                        "spotify_candidate_isrc"
                    )
                ),

            "spotify_url":
                candidate_spotify_url,

            "spotify_album_image_url":
                clean(
                    row.get(
                        "spotify_album_image_url"
                    )
                ),

            "spotify_youtube_url":
                candidate_youtube_url,
        }

    page_html = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Spotify Candidate Review</title>
<style>
:root {
    font-family:
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;
}
body {
    margin: 0;
    background: #f3f4f6;
    color: #171717;
}
.wrap {
    max-width: 1120px;
    margin: 28px auto;
    padding: 0 22px;
}
.card {
    background: white;
    border-radius: 16px;
    padding: 28px;
    box-shadow: 0 6px 28px rgba(0,0,0,.08);
}
.progress {
    color: #666;
    font-size: 14px;
    margin-bottom: 12px;
}
.columns {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 22px;
}
.panel {
    border: 1px solid #e3e3e3;
    border-radius: 12px;
    padding: 20px;
}
.artwork-wrap {
    display: flex;
    justify-content: center;
    margin-bottom: 18px;
}
.artwork {
    width: 220px;
    height: 220px;
    object-fit: cover;
    border-radius: 10px;
    box-shadow: 0 4px 16px rgba(0,0,0,.12);
    background: #eee;
}
.panel h2 {
    margin-top: 0;
    font-size: 18px;
}
.big {
    font-size: 23px;
    font-weight: 750;
    line-height: 1.25;
}
.artist {
    font-size: 17px;
    color: #555;
    margin-top: 4px;
}
.meta {
    margin-top: 12px;
    color: #555;
    line-height: 1.55;
}
.score {
    font-size: 34px;
    font-weight: 800;
    margin-top: 8px;
}
.links {
    display: flex;
    flex-wrap: wrap;
    gap: 9px;
    margin-top: 16px;
}
.link {
    display: inline-block;
    padding: 9px 12px;
    border-radius: 8px;
    text-decoration: none;
    background: #eef4ff;
    color: #174ea6;
    font-weight: 650;
}
.relationship {
    margin: 22px 0;
    text-align: center;
    font-size: 16px;
    color: #555;
}
.notes {
    margin-top: 20px;
}
textarea {
    width: 100%;
    min-height: 70px;
    box-sizing: border-box;
    margin-top: 8px;
    padding: 10px;
    border-radius: 8px;
    border: 1px solid #ccc;
    font: inherit;
}
.actions {
    display: flex;
    gap: 14px;
    margin-top: 20px;
}
button {
    flex: 1;
    padding: 17px;
    border: 0;
    border-radius: 10px;
    font-size: 17px;
    font-weight: 800;
    cursor: pointer;
}
.accept {
    background: #d9f5df;
}
.reject {
    background: #ffdede;
}
.unresolved {
    background: #fff0c5;
}
.hint {
    text-align: center;
    color: #666;
    margin-top: 12px;
    font-size: 14px;
}
.complete {
    text-align: center;
    padding: 70px 20px;
}
.complete h1 {
    color: #137333;
}
@media (max-width: 760px) {
    .columns {
        grid-template-columns: 1fr;
    }
    .actions {
        flex-direction: column;
    }
}
</style>
</head>

<body>
<div class="wrap">
<div class="card" id="app">Loading…</div>
</div>

<script>
let busy = false;

async function loadCandidate() {

    const response =
        await fetch("/candidate");

    const data =
        await response.json();

    if (data.done) {

        document.getElementById("app").innerHTML = `
            <div class="complete">
                <h1>Review complete</h1>
                <p>All Spotify candidates have been assigned a decision.</p>
                <p>The pipeline will now continue.</p>
            </div>
        `;

        return;
    }

    document.getElementById("app").innerHTML = `
        <div class="progress">
            Candidate ${data.position} of ${data.total}
        </div>

        <h1>Spotify Candidate Review</h1>

        <div class="columns">

            <div class="panel">
                <h2>WhoSampled relationship</h2>

                ${
                    data.source_album_image_url
                    ? `
                    <div class="artwork-wrap">
                        <img
                            class="artwork"
                            src="${data.source_album_image_url}"
                            alt="Source album artwork">
                    </div>
                    `
                    : ""
                }

                <div class="big">
                    ${data.related_track}
                </div>

                <div class="artist">
                    ${data.related_artist}
                </div>

                <div class="meta">
                    ${data.relationship_type}
                    ←
                    <strong>${data.source_title}</strong>
                    — ${data.source_artists}
                </div>

                <div class="links">

                    ${
                        data.source_spotify_url
                        ? `
                        <a class="link"
                           href="${data.source_spotify_url}"
                           target="_blank"
                           rel="noopener">
                            ▶ Listen to source on Spotify
                        </a>
                        `
                        : ""
                    }

                    ${
                        data.source_youtube_url
                        ? `
                        <a class="link"
                           href="${data.source_youtube_url}"
                           target="_blank"
                           rel="noopener">
                            ▶ Search source on YouTube
                        </a>
                        `
                        : ""
                    }

                    ${
                        data.whosampled_relationship_url
                        ? `
                        <a class="link"
                           href="${data.whosampled_relationship_url}"
                           target="_blank"
                           rel="noopener">
                            Open WhoSampled relationship
                        </a>
                        `
                        : ""
                    }

                </div>
            </div>

            <div class="panel">
                <h2>Spotify candidate</h2>

                ${
                    data.spotify_album_image_url
                    ? `
                    <div class="artwork-wrap">
                        <img
                            class="artwork"
                            src="${data.spotify_album_image_url}"
                            alt="Spotify candidate album artwork">
                    </div>
                    `
                    : ""
                }

                <div class="big">
                    ${data.spotify_title}
                </div>

                <div class="artist">
                    ${data.spotify_artist_names}
                </div>

                <div class="meta">
                    Album: ${data.spotify_album_name}<br>
                    Release: ${data.spotify_album_release_date}<br>
                    Candidate ISRC: ${data.spotify_candidate_isrc}
                </div>

                <div class="score">
                    ${
                        Number(
                            data.spotify_match_score || 0
                        ).toFixed(3)
                    }
                </div>

                <div class="links">

                    ${
                        data.spotify_url
                        ? `
                        <a class="link"
                           href="${data.spotify_url}"
                           target="_blank"
                           rel="noopener">
                            ▶ Listen to Spotify candidate
                        </a>
                        `
                        : ""
                    }

                    ${
                        data.spotify_youtube_url
                        ? `
                        <a class="link"
                           href="${data.spotify_youtube_url}"
                           target="_blank"
                           rel="noopener">
                            ▶ Search candidate on YouTube
                        </a>
                        `
                        : ""
                    }

                </div>
            </div>

        </div>

        <div class="relationship">
            The Spotify candidate should represent the
            <strong>${data.related_track}</strong>
            recording described by WhoSampled.
        </div>

        <div class="notes">
            <strong>Optional note</strong>
            <textarea
                id="note"
                placeholder="Optional reason for your decision"></textarea>
        </div>

        <div class="actions">

            <button
                class="accept"
                onclick="decide('accepted')">
                ACCEPT&nbsp;&nbsp;(Y)
            </button>

            <button
                class="reject"
                onclick="decide('rejected')">
                REJECT&nbsp;&nbsp;(N)
            </button>

            <button
                class="unresolved"
                onclick="decide('unresolved')">
                UNRESOLVED&nbsp;&nbsp;(M)
            </button>

        </div>

        <div class="hint">
            You can click a button or press
            <strong>Y</strong>, <strong>N</strong>, or <strong>M</strong>.
            Links open in a new tab so this review window stays open.
        </div>
    `;
}

async function decide(decision) {

    if (busy) return;

    busy = true;

    const noteElement =
        document.getElementById("note");

    const note =
        noteElement
        ? noteElement.value
        : "";

    await fetch(
        "/decision",
        {
            method: "POST",
            headers: {
                "Content-Type":
                    "application/json"
            },
            body: JSON.stringify({
                decision,
                note
            })
        }
    );

    busy = false;

    await loadCandidate();
}

document.addEventListener(
    "keydown",
    function(event) {

        if (
            event.target &&
            (
                event.target.tagName === "TEXTAREA" ||
                event.target.tagName === "INPUT"
            )
        ) {
            return;
        }

        const key =
            event.key.toLowerCase();

        if (key === "y") {
            decide("accepted");
        } else if (key === "n") {
            decide("rejected");
        } else if (key === "m") {
            decide("unresolved");
        }
    }
);

loadCandidate();
</script>
</body>
</html>
"""

    class Handler(BaseHTTPRequestHandler):

        def log_message(
            self,
            fmt,
            *args
        ):
            return

        def send_json(
            self,
            payload,
            status=200,
        ):

            body = json.dumps(
                payload,
                ensure_ascii=False,
            ).encode(
                "utf-8"
            )

            self.send_response(
                status
            )

            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8"
            )

            self.send_header(
                "Content-Length",
                str(len(body))
            )

            self.end_headers()

            self.wfile.write(
                body
            )

        def do_GET(self):

            path = urlparse(
                self.path
            ).path

            if path == "/":

                body = page_html.encode(
                    "utf-8"
                )

                self.send_response(
                    200
                )

                self.send_header(
                    "Content-Type",
                    "text/html; charset=utf-8"
                )

                self.send_header(
                    "Content-Length",
                    str(len(body))
                )

                self.end_headers()

                self.wfile.write(
                    body
                )

                return

            if path == "/candidate":

                payload = candidate_payload()

                self.send_json(
                    payload
                )

                return

            self.send_response(
                404
            )

            self.end_headers()

        def do_POST(self):

            path = urlparse(
                self.path
            ).path

            if path != "/decision":

                self.send_response(
                    404
                )

                self.end_headers()

                return

            try:

                length = int(
                    self.headers.get(
                        "Content-Length",
                        "0"
                    )
                )

                body = self.rfile.read(
                    length
                )

                payload = json.loads(
                    body.decode(
                        "utf-8"
                    )
                )

                decision = payload.get(
                    "decision"
                )

                note = payload.get(
                    "note",
                    ""
                )

                if decision not in (
                    "accepted",
                    "rejected",
                    "unresolved",
                ):
                    raise ValueError(
                        "Invalid decision."
                    )

                apply_decision(
                    decision,
                    note,
                )

                self.send_json({
                    "ok": True
                })

            except Exception as exc:

                self.send_json(
                    {
                        "ok": False,
                        "error": str(exc),
                    },
                    status=400,
                )

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        Handler,
    )

    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
    )

    thread.start()

    url = (
        f"http://127.0.0.1:"
        f"{server.server_address[1]}/"
    )

    print()
    print("=" * 80)
    print("SPOTIFY CANDIDATE REVIEW")
    print("=" * 80)
    print(
        "Review candidates:",
        len(
            undecided_indices()
        )
    )
    print(
        "Browser:",
        url
    )
    print(
        "Y = accept | N = reject | M = unresolved"
    )
    print(
        "Pipeline is paused until review is complete."
    )

    # Open the review UI in the default browser.
    # Spotify listening links themselves are ordinary Chrome links,
    # so they can reuse the user's existing authenticated Chrome
    # session.
    import subprocess

    subprocess.Popen(
        [
            "open",
            "-a",
            "Google Chrome",
            url,
        ]
    )

    try:

        while undecided_indices():
            time.sleep(
                0.5
            )

    except KeyboardInterrupt:

        print()
        print(
            "Spotify review interrupted."
        )
        print(
            "Decisions already made were saved."
        )
        raise

    finally:

        server.shutdown()
        server.server_close()

    # Persist restored review decisions even when there were no new
    # interactive decisions in this invocation. Without this call, saved
    # accepted/rejected decisions exist only in the in-memory DataFrame and
    # the read below reloads the fresh pre-restoration statuses from disk.
    persist()

    final_df = pd.read_csv(
        enriched_file
    )

    print()
    print(
        "SPOTIFY CANDIDATE REVIEW COMPLETE."
    )

    if "spotify_review_decision" in final_df.columns:
        print(
            final_df[
                "spotify_review_decision"
            ]
            .fillna("")
            .value_counts()
            .to_string()
        )

    return final_df
