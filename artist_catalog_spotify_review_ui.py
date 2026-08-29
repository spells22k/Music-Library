import argparse
import html
import json
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import pandas as pd


VALID_DECISIONS = {
    "accepted",
    "rejected",
    "unresolved",
}


def clean(value):
    if value is None:
        return ""

    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass

    return str(value).strip()


def esc(value):
    return html.escape(
        clean(value),
        quote=True,
    )


def now_iso():
    return datetime.now(
        timezone.utc
    ).isoformat()


def load_json(path, default):
    path = Path(path)

    if not path.exists():
        return default

    try:
        data = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )
        return data
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


def normalize_review_key(recording_id, whosampled_url):
    recording_id = clean(
        recording_id
    )

    if recording_id:
        return recording_id

    return (
        "whosampled:"
        + clean(
            whosampled_url
        )
    )


def decision_is_complete(review):
    return (
        isinstance(
            review,
            dict,
        )
        and clean(
            review.get(
                "decision"
            )
        )
        in VALID_DECISIONS
    )


def number(value, digits=4):
    raw = clean(
        value
    )

    if not raw:
        return ""

    try:
        return str(
            round(
                float(raw),
                digits,
            )
        )
    except Exception:
        return raw


def duration_seconds_from_ms(value):
    raw = clean(
        value
    )

    if not raw:
        return ""

    try:
        seconds = (
            float(raw)
            / 1000.0
        )

        minutes = int(
            seconds // 60
        )

        remainder = int(
            round(
                seconds
                - minutes * 60
            )
        )

        return (
            f"{minutes}:"
            f"{remainder:02d}"
        )

    except Exception:
        return raw


def load_candidate_groups(candidate_file):
    candidate_file = Path(
        candidate_file
    )

    df = pd.read_csv(
        candidate_file
    ).fillna("")

    groups = []

    if df.empty:
        return groups

    for recording_id, group in df.groupby(
        "recording_id",
        sort=False,
        dropna=False,
    ):

        rows = group.to_dict(
            orient="records"
        )

        if not rows:
            continue

        first = rows[0]

        candidates = [
            row
            for row in rows
            if (
                clean(
                    row.get(
                        "candidate_status"
                    )
                )
                == "review"
                and clean(
                    row.get(
                        "spotify_track_id"
                    )
                )
            )
        ]

        if not candidates:
            continue

        candidates.sort(
            key=lambda row: (
                int(
                    float(
                        clean(
                            row.get(
                                "candidate_rank"
                            )
                        )
                        or 9999
                    )
                )
            )
        )

        groups.append({
            "review_key":
                normalize_review_key(
                    recording_id,
                    first.get(
                        "whosampled_url"
                    ),
                ),

            "recording_id":
                clean(
                    recording_id
                ),

            "whosampled_url":
                clean(
                    first.get(
                        "whosampled_url"
                    )
                ),

            "whosampled_title":
                clean(
                    first.get(
                        "whosampled_title"
                    )
                ),

            "whosampled_artist_names":
                clean(
                    first.get(
                        "whosampled_artist_names"
                    )
                ),

            "whosampled_album":
                clean(
                    first.get(
                        "whosampled_album"
                    )
                ),

            "whosampled_release_year":
                clean(
                    first.get(
                        "whosampled_release_year"
                    )
                ),

            "whosampled_duration_ms":
                clean(
                    first.get(
                        "whosampled_duration_ms"
                    )
                ),

            "candidates":
                candidates,
        })

    return groups


def update_candidate_csv(
    candidate_file,
    reviews,
):
    candidate_file = Path(
        candidate_file
    )

    df = pd.read_csv(
        candidate_file
    ).fillna("")

    if (
        "spotify_review_decision"
        not in df.columns
    ):
        df[
            "spotify_review_decision"
        ] = ""

    if (
        "spotify_reviewed_at"
        not in df.columns
    ):
        df[
            "spotify_reviewed_at"
        ] = ""

    if (
        "spotify_selected_candidate"
        not in df.columns
    ):
        df[
            "spotify_selected_candidate"
        ] = ""

    for idx, row in df.iterrows():

        key = normalize_review_key(
            row.get(
                "recording_id"
            ),
            row.get(
                "whosampled_url"
            ),
        )

        review = reviews.get(
            key
        )

        if not isinstance(
            review,
            dict,
        ):
            continue

        decision = clean(
            review.get(
                "decision"
            )
        )

        if decision not in VALID_DECISIONS:
            continue

        df.at[
            idx,
            "spotify_review_decision"
        ] = decision

        df.at[
            idx,
            "spotify_reviewed_at"
        ] = clean(
            review.get(
                "reviewed_at"
            )
        )

        selected_id = clean(
            review.get(
                "spotify_track_id"
            )
        )

        row_id = clean(
            row.get(
                "spotify_track_id"
            )
        )

        df.at[
            idx,
            "spotify_selected_candidate"
        ] = (
            "true"
            if (
                selected_id
                and row_id
                == selected_id
            )
            else ""
        )

    df.to_csv(
        candidate_file,
        index=False,
        encoding="utf-8",
    )


def run_catalog_spotify_review(
    candidate_file,
    review_file,
):
    candidate_file = Path(
        candidate_file
    )

    review_file = Path(
        review_file
    )

    groups = load_candidate_groups(
        candidate_file
    )

    reviews = load_json(
        review_file,
        {},
    )

    if not isinstance(
        reviews,
        dict,
    ):
        reviews = {}

    pending = [
        group
        for group in groups
        if not decision_is_complete(
            reviews.get(
                group[
                    "review_key"
                ]
            )
        )
    ]

    print()
    print("=" * 100)
    print(
        "ARTIST CATALOG ↔ SPOTIFY REVIEW"
    )
    print("=" * 100)
    print(
        "Recordings with candidates:",
        len(groups),
    )
    print(
        "Previously reviewed:",
        len(groups)
        - len(pending),
    )
    print(
        "Pending review:",
        len(pending),
    )

    if not pending:
        print()
        print(
            "No catalog Spotify review "
            "candidates remain."
        )
        update_candidate_csv(
            candidate_file,
            reviews,
        )
        return reviews

    state = {
        "pending":
            pending,

        "reviews":
            reviews,

        "selected_index":
            0,

        "done":
            threading.Event(),
    }

    def current():
        if not state[
            "pending"
        ]:
            return None

        return state[
            "pending"
        ][0]

    def persist(
        decision,
        candidate_index=None,
    ):
        row = current()

        if row is None:
            return

        selected = None

        if candidate_index is None:
            candidate_index = state[
                "selected_index"
            ]

        if (
            decision
            in {
                "accepted",
                "rejected",
            }
            and row[
                "candidates"
            ]
        ):
            if (
                candidate_index < 0
                or candidate_index
                >= len(
                    row[
                        "candidates"
                    ]
                )
            ):
                candidate_index = 0

            selected = row[
                "candidates"
            ][
                candidate_index
            ]

        review = {
            "review_key":
                row[
                    "review_key"
                ],

            "recording_id":
                row[
                    "recording_id"
                ],

            "whosampled_url":
                row[
                    "whosampled_url"
                ],

            "whosampled_title":
                row[
                    "whosampled_title"
                ],

            "whosampled_artist_names":
                row[
                    "whosampled_artist_names"
                ],

            "decision":
                decision,

            "decision_source":
                "contributor_review",

            "reviewed_at":
                now_iso(),

            "spotify_track_id":
                "",

            "spotify_title":
                "",

            "spotify_artist_names":
                "",

            "spotify_url":
                "",

            "candidate_rank":
                "",

            "match_score":
                "",
        }

        if selected is not None:
            review.update({
                "spotify_track_id":
                    clean(
                        selected.get(
                            "spotify_track_id"
                        )
                    ),

                "spotify_title":
                    clean(
                        selected.get(
                            "spotify_title"
                        )
                    ),

                "spotify_artist_names":
                    clean(
                        selected.get(
                            "spotify_artist_names"
                        )
                    ),

                "spotify_url":
                    clean(
                        selected.get(
                            "spotify_url"
                        )
                    ),

                "candidate_rank":
                    clean(
                        selected.get(
                            "candidate_rank"
                        )
                    ),

                "match_score":
                    clean(
                        selected.get(
                            "match_score"
                        )
                    ),
            })

        state[
            "reviews"
        ][
            row[
                "review_key"
            ]
        ] = review

        save_json(
            review_file,
            state[
                "reviews"
            ],
        )

        update_candidate_csv(
            candidate_file,
            state[
                "reviews"
            ],
        )

        state[
            "pending"
        ].pop(
            0
        )

        state[
            "selected_index"
        ] = 0

        if not state[
            "pending"
        ]:
            state[
                "done"
            ].set()

    def render():
        row = current()

        if row is None:
            return """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Catalog Spotify Review Complete</title>
<style>
body {
    font-family: -apple-system, BlinkMacSystemFont, sans-serif;
    margin: 40px;
    max-width: 1100px;
}
</style>
</head>
<body>
<h1>Catalog Spotify review complete.</h1>
<p>You can return to the terminal.</p>
</body>
</html>
"""

        selected_index = state[
            "selected_index"
        ]

        cards = []

        for index, candidate in enumerate(
            row[
                "candidates"
            ]
        ):

            selected = (
                index
                == selected_index
            )

            classes = (
                "candidate selected"
                if selected
                else "candidate"
            )

            spotify_url = clean(
                candidate.get(
                    "spotify_url"
                )
            )

            link = (
                f'<a href="{esc(spotify_url)}" '
                f'target="_blank">Open Spotify</a>'
                if spotify_url
                else ""
            )

            cards.append(
                f"""
<div class="{classes}"
     onclick="selectCandidate({index})">

  <div class="rank">
    Candidate {index + 1}
  </div>

  <h2>
    {esc(candidate.get("spotify_title"))}
  </h2>

  <div class="artist">
    {esc(candidate.get("spotify_artist_names"))}
  </div>

  <div class="meta">
    Album:
    {esc(candidate.get("spotify_album_name"))}
    <br>
    Release:
    {esc(candidate.get("spotify_album_release_date"))}
    <br>
    Duration:
    {esc(duration_seconds_from_ms(
        candidate.get("spotify_duration_ms")
    ))}
  </div>

  <div class="score">
    Overall:
    <strong>{esc(number(candidate.get("match_score")))}</strong>
  </div>

  <table>
    <tr>
      <td>Title</td>
      <td>{esc(number(candidate.get("title_score")))}</td>
    </tr>
    <tr>
      <td>Artist</td>
      <td>{esc(number(candidate.get("artist_score")))}</td>
    </tr>
    <tr>
      <td>Duration</td>
      <td>{esc(number(candidate.get("duration_score")))}</td>
    </tr>
    <tr>
      <td>Duration difference</td>
      <td>{esc(number(candidate.get("duration_difference_seconds"), 2))} s</td>
    </tr>
    <tr>
      <td>Year</td>
      <td>{esc(number(candidate.get("year_score")))}</td>
    </tr>
    <tr>
      <td>Album</td>
      <td>{esc(number(candidate.get("album_score")))}</td>
    </tr>
  </table>

  <div class="links">
    {link}
  </div>
</div>
"""
            )

        whosampled_link = (
            f'<a href="{esc(row["whosampled_url"])}" '
            f'target="_blank">Open WhoSampled track</a>'
            if row[
                "whosampled_url"
            ]
            else ""
        )

        return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Catalog Spotify Review</title>

<style>
body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    margin: 0;
    background: #f5f5f7;
    color: #1d1d1f;
}}

.wrap {{
    max-width: 1250px;
    margin: 0 auto;
    padding: 30px;
}}

.header {{
    margin-bottom: 24px;
}}

.source {{
    background: white;
    padding: 24px;
    border-radius: 16px;
    margin-bottom: 24px;
}}

.source h1 {{
    margin: 0 0 6px 0;
}}

.source .artist {{
    font-size: 20px;
    margin-bottom: 14px;
}}

.meta {{
    line-height: 1.6;
}}

.candidates {{
    display: grid;
    grid-template-columns:
        repeat(auto-fit, minmax(330px, 1fr));
    gap: 18px;
}}

.candidate {{
    background: white;
    padding: 20px;
    border: 3px solid transparent;
    border-radius: 16px;
    cursor: pointer;
}}

.candidate.selected {{
    border-color: #0071e3;
}}

.rank {{
    font-size: 13px;
    font-weight: 700;
    text-transform: uppercase;
    opacity: .6;
}}

.candidate h2 {{
    margin-bottom: 4px;
}}

.candidate .artist {{
    font-size: 18px;
    margin-bottom: 12px;
}}

.score {{
    font-size: 20px;
    margin: 16px 0;
}}

table {{
    width: 100%;
    border-collapse: collapse;
}}

td {{
    padding: 4px 0;
}}

td:last-child {{
    text-align: right;
    font-family: ui-monospace, monospace;
}}

.links {{
    margin-top: 16px;
}}

.controls {{
    position: sticky;
    bottom: 0;
    margin-top: 28px;
    background: rgba(255,255,255,.96);
    padding: 18px;
    border-radius: 16px;
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
}}

button {{
    padding: 12px 20px;
    font-size: 16px;
    border: 0;
    border-radius: 10px;
    cursor: pointer;
}}

.accept {{
    background: #1f9d55;
    color: white;
}}

.reject {{
    background: #d64545;
    color: white;
}}

.unresolved {{
    background: #777;
    color: white;
}}

.progress {{
    margin-bottom: 12px;
    opacity: .7;
}}

.help {{
    margin-top: 12px;
    font-size: 14px;
    opacity: .7;
}}
</style>

<script>
function post(values) {{
    fetch(
        "/decision",
        {{
            method: "POST",
            headers: {{
                "Content-Type":
                    "application/x-www-form-urlencoded"
            }},
            body: new URLSearchParams(values)
        }}
    ).then(() => {{
        window.location.reload();
    }});
}}

function selectCandidate(index) {{
    post({{
        action: "select",
        index: index
    }});
}}

function decide(value) {{
    post({{
        action: "decision",
        decision: value
    }});
}}

document.addEventListener(
    "keydown",
    function(event) {{

        const key =
            event.key.toLowerCase();

        if (
            key >= "1"
            && key <= "9"
        ) {{
            selectCandidate(
                parseInt(key, 10) - 1
            );
            return;
        }}

        if (key === "y") {{
            decide("accepted");
        }}

        if (key === "n") {{
            decide("rejected");
        }}

        if (key === "m") {{
            decide("unresolved");
        }}
    }}
);
</script>
</head>

<body>
<div class="wrap">

<div class="header">
  <div class="progress">
    Pending:
    {len(state["pending"])}
    |
    Reviewed:
    {len(groups) - len(state["pending"])}
  </div>
</div>

<div class="source">

  <h1>
    {esc(row["whosampled_title"])}
  </h1>

  <div class="artist">
    {esc(row["whosampled_artist_names"])}
  </div>

  <div class="meta">
    Album:
    {esc(row["whosampled_album"])}
    <br>
    Year:
    {esc(row["whosampled_release_year"])}
    <br>
    Duration:
    {esc(duration_seconds_from_ms(
        row["whosampled_duration_ms"]
    ))}
  </div>

  <div style="margin-top:14px">
    {whosampled_link}
  </div>

</div>

<div class="candidates">
{''.join(cards)}
</div>

<div class="controls">

  <button
    class="accept"
    onclick="decide('accepted')">
    Y — Accept selected
  </button>

  <button
    class="reject"
    onclick="decide('rejected')">
    N — Reject selected candidate
  </button>

  <button
    class="unresolved"
    onclick="decide('unresolved')">
    M — Unresolved
  </button>

</div>

<div class="help">
Keyboard:
1–9 select candidate |
Y accept |
N reject selected candidate |
M unresolved
<br>
No decision in this UI merges identities.
It only persists contributor review.
</div>

</div>
</body>
</html>
"""

    class Handler(
        BaseHTTPRequestHandler
    ):

        def log_message(
            self,
            format,
            *args,
        ):
            return

        def send_html(
            self,
            body,
        ):
            encoded = body.encode(
                "utf-8"
            )

            self.send_response(
                200
            )

            self.send_header(
                "Content-Type",
                "text/html; charset=utf-8",
            )

            self.send_header(
                "Content-Length",
                str(
                    len(
                        encoded
                    )
                ),
            )

            self.end_headers()

            self.wfile.write(
                encoded
            )

        def do_GET(
            self
        ):
            self.send_html(
                render()
            )

        def do_POST(
            self
        ):
            length = int(
                self.headers.get(
                    "Content-Length",
                    "0",
                )
            )

            raw = self.rfile.read(
                length
            ).decode(
                "utf-8"
            )

            form = parse_qs(
                raw
            )

            action = clean(
                (
                    form.get(
                        "action",
                        [""],
                    )
                    or [""]
                )[0]
            )

            if action == "select":

                try:
                    index = int(
                        (
                            form.get(
                                "index",
                                ["0"],
                            )
                            or ["0"]
                        )[0]
                    )
                except Exception:
                    index = 0

                row = current()

                if row is not None:

                    if (
                        0 <= index
                        < len(
                            row[
                                "candidates"
                            ]
                        )
                    ):
                        state[
                            "selected_index"
                        ] = index

            elif action == "decision":

                decision = clean(
                    (
                        form.get(
                            "decision",
                            [""],
                        )
                        or [""]
                    )[0]
                )

                if decision in VALID_DECISIONS:
                    persist(
                        decision
                    )

            self.send_response(
                204
            )
            self.end_headers()

    server = ThreadingHTTPServer(
        (
            "127.0.0.1",
            0,
        ),
        Handler,
    )

    port = server.server_address[
        1
    ]

    url = (
        f"http://127.0.0.1:"
        f"{port}/"
    )

    thread = threading.Thread(
        target=
            server.serve_forever,
        daemon=True,
    )

    thread.start()

    print()
    print(
        "Browser:",
        url
    )

    print(
        "Y = accept | "
        "N = reject | "
        "M = unresolved"
    )

    print(
        "1–9 = select candidate"
    )

    print(
        "Pipeline is paused until "
        "catalog Spotify review "
        "is complete."
    )

    webbrowser.open(
        url
    )

    state[
        "done"
    ].wait()

    server.shutdown()
    server.server_close()

    update_candidate_csv(
        candidate_file,
        state[
            "reviews"
        ],
    )

    print()
    print(
        "CATALOG SPOTIFY REVIEW COMPLETE."
    )

    counts = {
        "accepted": 0,
        "rejected": 0,
        "unresolved": 0,
    }

    for review in state[
        "reviews"
    ].values():

        decision = clean(
            review.get(
                "decision"
            )
        )

        if decision in counts:
            counts[
                decision
            ] += 1

    print()
    print(
        "accepted:",
        counts[
            "accepted"
        ]
    )

    print(
        "rejected:",
        counts[
            "rejected"
        ]
    )

    print(
        "unresolved:",
        counts[
            "unresolved"
        ]
    )

    return state[
        "reviews"
    ]


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--run-dir",
        default=(
            "runs/"
            "playlist_3XtRerTr3ndS88v51AAixb"
        ),
    )

    args = parser.parse_args()

    run_dir = Path(
        args.run_dir
    )

    candidate_file = (
        run_dir
        / "artist_catalog_spotify_candidates.csv"
    )

    review_file = (
        run_dir
        / "artist_catalog_spotify_reviews.json"
    )

    if not candidate_file.exists():
        raise SystemExit(
            f"Missing: {candidate_file}"
        )

    run_catalog_spotify_review(
        candidate_file,
        review_file,
    )

    print()
    print(
        "Reviews:",
        review_file
    )

    print()
    print(
        "No Spotify requests were made."
    )

    print(
        "No WhoSampled requests were made."
    )

    print(
        "No identities were merged."
    )


if __name__ == "__main__":
    main()
