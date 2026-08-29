import html
import json
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import (
    BaseHTTPRequestHandler,
    ThreadingHTTPServer,
)
from pathlib import Path
from urllib.parse import parse_qs

from artist_profile_metadata import (
    collect_artist_profile_metadata,
    local_image_data_uri,
)

from artist_review_ui import (
    build_artist_review_candidates,
)

from whosampled_match import (
    normalize,
    save_artist_slug_cache,
)


VALID_DECISIONS = {
    "accepted",
    "rejected",
    "unresolved",
}


def clean(value):
    if value is None:
        return ""
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


def load_reviews(path):
    path = Path(path)

    if not path.exists():
        return {}

    try:
        data = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

        if isinstance(
            data,
            dict,
        ):
            return data

    except Exception:
        pass

    return {}


def save_reviews(
    path,
    reviews,
):
    path = Path(path)

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    path.write_text(
        json.dumps(
            reviews,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def decision_is_complete(
    review,
):
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


def learn_artist_slug(
    artist_slug_cache,
    artist_name,
    slug,
):
    """
    Persist every contributor-accepted WhoSampled slug through the
    same cache used by normal Phase-1 matching.
    """

    artist_name = clean(
        artist_name
    )

    slug = clean(
        slug
    )

    if (
        not artist_name
        or not slug
    ):
        return

    key = normalize(
        artist_name
    )

    existing = (
        artist_slug_cache.get(
            key,
            [],
        )
    )

    if isinstance(
        existing,
        str,
    ):
        existing = [
            existing
        ]

    existing = list(
        existing
    )

    if slug in existing:
        return

    existing.insert(
        0,
        slug,
    )

    artist_slug_cache[
        key
    ] = existing

    save_artist_slug_cache(
        artist_slug_cache
    )

    print(
        "LEARNED ARTIST SLUG FROM REVIEW:",
        artist_name,
        "→",
        slug,
    )


def run_artist_catalog_review(
    match_file,
    spotify_file,
    review_file,
    artist_slug_cache,
    related_artist_candidates_file=None,
):
    """
    Browser-based WhoSampled artist identity review.

    Y = accept selected candidate
    N = reject
    M = unresolved
    1-9 = select candidate

    A candidate supported by an attributable verified track match
    can be accepted automatically, provided there is exactly one
    such profile and no existing contributor decision.
    """

    match_file = Path(
        match_file
    )

    spotify_file = Path(
        spotify_file
    )

    review_file = Path(
        review_file
    )

    rows = build_artist_review_candidates(
        match_file,
        spotify_file,
        artist_slug_cache,
        related_artist_candidates_file=
            related_artist_candidates_file,
    )

    reviews = load_reviews(
        review_file
    )

    def review_storage_key(row):
        """
        Durable persistence key for either identity namespace.

        Backward compatibility:
        existing artist_catalog_reviews.json entries keyed by
        raw Spotify artist ID remain valid.
        """

        spotify_artist_id = clean(
            row.get(
                "spotify_artist_id"
            )
        )

        explicit_key = clean(
            row.get(
                "review_key"
            )
        )

        # Preserve previously stored Spotify decisions.
        if (
            spotify_artist_id
            and spotify_artist_id in reviews
        ):
            return spotify_artist_id

        if explicit_key:
            return explicit_key

        return spotify_artist_id



    # --------------------------------------------------------
    # AUTO-ACCEPT:
    #
    # Only a single candidate carrying verified_track_match can
    # qualify. The candidate builder's cross-artist attribution
    # guard has already removed invalid cases such as
    # Jorge Ben Jor -> Toquinho.
    #
    # Existing human decisions are never overwritten.
    # --------------------------------------------------------

    auto_accepted = 0

    for row in rows:

        artist_id = review_storage_key(row)

        if decision_is_complete(
            reviews.get(
                artist_id
            )
        ):
            continue

        candidates = [
            candidate
            for candidate in row.get(
                "candidates",
                [],
            )
            if (
                "verified_track_match"
                in candidate.get(
                    "sources",
                    [],
                )
            )
        ]

        if len(candidates) != 1:
            continue

        selected = (
            candidates[0]
        )

        reviews[
            artist_id
        ] = {
            "spotify_artist_id":
                clean(
                row.get(
                    "spotify_artist_id"
                )
            ),

            "spotify_artist_name":
                clean(
                    row.get(
                        "spotify_artist_name"
                    )
                ),

            "whosampled_artist_url":
                clean(
                    selected.get(
                        "url"
                    )
                ),

            "whosampled_artist_slug":
                clean(
                    selected.get(
                        "slug"
                    )
                ),

            "candidate_sources":
                selected.get(
                    "sources",
                    [],
                ),

            "decision":
                "accepted",

            "decision_source":
                (
                    "verified_attributable_"
                    "track_match"
                ),

            "auto_accepted":
                True,

            "review_note":
                (
                    "Automatically accepted from "
                    "attributable verified "
                    "WhoSampled track evidence."
                ),

            "reviewed_at":
                now_iso(),
        }

        learn_artist_slug(
            artist_slug_cache,
            row.get(
                "spotify_artist_name"
            ),
            selected.get(
                "slug"
            ),
        )

        auto_accepted += 1

    if auto_accepted:

        save_reviews(
            review_file,
            reviews,
        )

    print()
    print("=" * 80)
    print("ARTIST CATALOG REVIEW")
    print("=" * 80)

    print(
        "Artists requiring catalog identity:",
        len(rows),
    )

    print(
        "Auto-accepted from attributable "
        "verified tracks:",
        auto_accepted,
    )

    # --------------------------------------------------------
    # Determine whether an existing decision should be reopened.
    #
    # Normally completed contributor decisions are final.
    #
    # Exception:
    # a previously rejected/unresolved artist now has a learned
    # slug candidate that did not exist when the review was made.
    # We re-present it, but DO NOT overwrite the old decision
    # until the contributor makes a new one.
    # --------------------------------------------------------

    def needs_review(row):

        artist_id = review_storage_key(row)

        previous = reviews.get(
            artist_id
        )

        if not decision_is_complete(
            previous
        ):
            return True

        previous_decision = clean(
            previous.get(
                "decision"
            )
        )

        if previous_decision == "accepted":
            return False

        learned_candidates = [
            candidate
            for candidate in row.get(
                "candidates",
                [],
            )
            if (
                "learned_slug"
                in candidate.get(
                    "sources",
                    [],
                )
            )
        ]

        if not learned_candidates:
            return False

        previous_url = clean(
            previous.get(
                "whosampled_artist_url"
            )
        )

        # Reopen when there is newly learned identity evidence
        # not represented by the old decision.
        return any(
            clean(
                candidate.get(
                    "url"
                )
            )
            != previous_url
            for candidate
            in learned_candidates
        )

    pending = [
        row
        for row in rows
        if needs_review(
            row
        )
    ]

    print(
        "Previously reviewed and retained:",
        len(rows) - len(pending),
    )

    print(
        "Pending / reopened review:",
        len(pending),
    )

    if not pending:

        print(
            "No artist review candidates remain."
        )

        return reviews

    # --------------------------------------------------------
    # Collect/cache profile metadata before opening the UI.
    #
    # This supplies:
    #   profile image
    #   WhoSampled profile name
    #   real name
    #   aliases
    #   groups
    #   group members
    #   country/origin
    #
    # Cached profiles make zero repeat requests.
    # --------------------------------------------------------

    profile_cache = (
        collect_artist_profile_metadata(
            pending,
            review_file.parent
            / "whosampled_artist_profiles",
        )
    )

    # --------------------------------------------------------
    # Remove candidate profiles that were proven not to exist.
    #
    # collect_artist_profile_metadata() has now visited every
    # uncached candidate and cached its HTTP result. A candidate
    # with an explicit non-200 HTTP status must never be offered
    # to the contributor as a possible WhoSampled identity.
    #
    # Absence from the cache is NOT treated as rejection here:
    # only explicit negative HTTP evidence removes a candidate.
    # --------------------------------------------------------

    removed_non_200_candidates = 0

    for row in pending:

        verified_candidates = []

        for candidate in row.get(
            "candidates",
            [],
        ):

            candidate_url = clean(
                candidate.get(
                    "url"
                )
            )

            cached_profile = (
                profile_cache.get(
                    candidate_url,
                    {}
                )
                if candidate_url
                else {}
            )

            profile_status = clean(
                cached_profile.get(
                    "status"
                )
            ).casefold()

            if (
                profile_status.startswith("http_")
                and profile_status != "http_200"
            ):

                print(
                    "EXCLUDING NON-200 ARTIST PROFILE:",
                    candidate_url,
                    "(" + profile_status + ")",
                )

                removed_non_200_candidates += 1

                continue

            verified_candidates.append(
                candidate
            )

        row[
            "candidates"
        ] = verified_candidates

    print(
        "Non-200 artist profile candidates excluded:",
        removed_non_200_candidates,
    )

    state = {
        "pending":
            pending,

        "reviews":
            reviews,

        "profile_cache":
            profile_cache,
    }

    def current_row():

        if not state[
            "pending"
        ]:
            return None

        return state[
            "pending"
        ][0]

    def persist_decision(
        row,
        decision,
        candidate_index=None,
    ):
        artist_id = review_storage_key(row)

        result = {
            "spotify_artist_id":
                clean(
                row.get(
                    "spotify_artist_id"
                )
            ),

            "spotify_artist_name":
                clean(
                    row.get(
                        "spotify_artist_name"
                    )
                ),

            "spotify_candidate_artist_id":
                clean(
                    row.get(
                        "spotify_candidate_artist_id"
                    )
                ),

            "spotify_candidate_artist_name":
                clean(
                    row.get(
                        "spotify_candidate_artist_name"
                    )
                ),

            "spotify_candidate_url":
                clean(
                    row.get(
                        "spotify_candidate_url"
                    )
                ),

            "spotify_candidate_status":
                clean(
                    row.get(
                        "spotify_candidate_status"
                    )
                ),

            "spotify_candidate_match_basis":
                clean(
                    row.get(
                        "spotify_candidate_match_basis"
                    )
                ),

            "whosampled_artist_url":
                "",

            "whosampled_artist_slug":
                "",

            "candidate_sources":
                [],

            "decision":
                decision,

            "decision_source":
                "contributor_review",

            "auto_accepted":
                False,

            "review_note":
                "",

            "reviewed_at":
                now_iso(),
        }

        if decision == "accepted":

            candidate_spotify_id = clean(
                row.get(
                    "spotify_candidate_artist_id"
                )
            )

            if candidate_spotify_id:

                result[
                    "spotify_artist_id"
                ] = candidate_spotify_id

                result[
                    "spotify_artist_name"
                ] = clean(
                    row.get(
                        "spotify_candidate_artist_name"
                    )
                )

                result[
                    "spotify_artist_url"
                ] = clean(
                    row.get(
                        "spotify_candidate_url"
                    )
                )

                result[
                    "spotify_identity_reconciled"
                ] = True

            else:

                result[
                    "spotify_identity_reconciled"
                ] = False


            candidates = row.get(
                "candidates",
                [],
            )

            if candidate_index is None:
                candidate_index = 0

            if not (
                0
                <= candidate_index
                < len(candidates)
            ):
                return False

            selected = candidates[
                candidate_index
            ]

            result[
                "whosampled_artist_url"
            ] = clean(
                selected.get(
                    "url"
                )
            )

            result[
                "whosampled_artist_slug"
            ] = clean(
                selected.get(
                    "slug"
                )
            )

            result[
                "candidate_sources"
            ] = selected.get(
                "sources",
                [],
            )

            # ----------------------------------------------
            # THIS is the new learning behavior.
            #
            # Any accepted contributor identity becomes a
            # normal learned WhoSampled slug for future runs.
            # ----------------------------------------------

            learn_artist_slug(
                artist_slug_cache,
                row.get(
                    "spotify_artist_name"
                ),
                selected.get(
                    "slug"
                ),
            )

        state[
            "reviews"
        ][artist_id] = result

        save_reviews(
            review_file,
            state[
                "reviews"
            ],
        )

        state[
            "pending"
        ] = [
            item
            for item
            in state[
                "pending"
            ]
            if review_storage_key(
                item
            )
            != artist_id
        ]

        return True

    def metadata_line(
        label,
        value,
    ):
        """
        Render either ordinary strings or structured artist/group
        identities from WhoSampled profile metadata.
        """

        if isinstance(
            value,
            list,
        ):

            formatted = []

            for item in value:

                if isinstance(
                    item,
                    dict,
                ):

                    name = clean(
                        item.get(
                            "name"
                        )
                    )

                    url = clean(
                        item.get(
                            "whosampled_url"
                        )
                    )

                    if not name:
                        continue

                    if url:

                        formatted.append(
                            '<a '
                            'href="'
                            + esc(url)
                            + '" '
                            'target="_blank" '
                            'rel="noopener">'
                            + esc(name)
                            + "</a>"
                        )

                    else:

                        formatted.append(
                            esc(name)
                        )

                else:

                    item = clean(
                        item
                    )

                    if item:

                        formatted.append(
                            esc(item)
                        )

            if not formatted:
                return ""

            rendered_value = (
                ", ".join(
                    formatted
                )
            )

        else:

            value = clean(
                value
            )

            if not value:
                return ""

            rendered_value = esc(
                value
            )

        return (
            '<div class="meta-line">'
            '<strong>'
            + esc(label)
            + ":</strong> "
            + rendered_value
            + "</div>"
        )

    def render_candidate(
        candidate,
        index,
        selected=False,
    ):
        url = clean(
            candidate.get(
                "url"
            )
        )

        profile = (
            state[
                "profile_cache"
            ].get(
                url,
                {},
            )
        )

        image_uri = (
            local_image_data_uri(
                profile.get(
                    "image_path"
                )
            )
        )

        image_html = ""

        if image_uri:

            image_html = f"""
            <div class="profile-image-wrap">
                <img
                    class="profile-image"
                    src="{image_uri}"
                    alt="WhoSampled artist">
            </div>
            """

        meta_html = "".join([
            metadata_line(
                "WhoSampled name",
                profile.get(
                    "profile_name"
                ),
            ),

            metadata_line(
                "Real name",
                profile.get(
                    "real_names"
                ),
            ),

            metadata_line(
                "Aliases",
                profile.get(
                    "aliases"
                ),
            ),

            metadata_line(
                "Current groups / member of",
                profile.get(
                    "current_groups"
                ),
            ),

            metadata_line(
                "Past groups",
                profile.get(
                    "past_groups"
                ),
            ),

            metadata_line(
                "Group members",
                profile.get(
                    "group_members"
                ),
            ),

            metadata_line(
                "Country / origin",
                profile.get(
                    "country"
                ),
            ),
        ])

        sources = "".join(
            (
                '<span class="badge">'
                + esc(source)
                + "</span>"
            )
            for source
            in candidate.get(
                "sources",
                [],
            )
        )

        evidence = "".join(
            (
                "<li>"
                + esc(item)
                + "</li>"
            )
            for item
            in candidate.get(
                "evidence",
                [],
            )
        )

        checked = (
            "checked"
            if selected
            else ""
        )

        return f"""
        <label class="candidate">

            <div class="candidate-top">

                <input
                    type="radio"
                    name="candidate"
                    value="{index}"
                    {checked}>

                <div class="candidate-number">
                    {index + 1}
                </div>

                <div class="candidate-url">
                    {esc(url)}
                </div>

            </div>

            {image_html}

            <div class="profile-meta">
                {meta_html}
            </div>

            <div class="badges">
                {sources}
            </div>

            <ul class="evidence">
                {evidence}
            </ul>

            <a
                class="link"
                href="{esc(url)}"
                target="_blank"
                rel="noopener"
                onclick="event.stopPropagation();">
                Open WhoSampled
            </a>

        </label>
        """

    def render_page():

        row = current_row()

        if row is None:

            return """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Artist Review Complete</title>
<style>
body {
    font-family:
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;
    background: #f3f4f6;
}
.card {
    max-width: 650px;
    margin: 90px auto;
    padding: 55px;
    text-align: center;
    background: white;
    border-radius: 16px;
    box-shadow:
        0 6px 28px rgba(0,0,0,.08);
}
h1 {
    color: #137333;
}
</style>
</head>
<body>
<div class="card">
<h1>Artist review complete</h1>
<p>All decisions have been saved.</p>
</div>
</body>
</html>
"""

        position = (
            len(rows)
            - len(
                state[
                    "pending"
                ]
            )
            + 1
        )

        unresolved = "".join(
            (
                "<li>"
                + esc(
                    track.get(
                        "title"
                    )
                )
                + "</li>"
            )
            for track
            in row.get(
                "unresolved_tracks",
                [],
            )
        )

        verified_rows = (
            row.get(
                "verified_tracks",
                [],
            )
        )

        verified = "".join(
            (
                "<li>"
                + esc(
                    track.get(
                        "title"
                    )
                )
                + "</li>"
            )
            for track
            in verified_rows
        )

        if not verified:
            verified = (
                '<li class="muted">'
                "No attributable verified tracks"
                "</li>"
            )

        candidates = "".join(
            render_candidate(
                candidate,
                index,
                selected=(
                    index == 0
                ),
            )
            for index, candidate
            in enumerate(
                row.get(
                    "candidates",
                    [],
                )
            )
        )

        return f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>WhoSampled Artist Review</title>

<style>
:root {{
    font-family:
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;
}}

body {{
    margin: 0;
    background: #f3f4f6;
    color: #171717;
}}

.wrap {{
    max-width: 1100px;
    margin: 28px auto;
    padding: 0 22px;
}}

.progress {{
    color: #666;
    font-size: 14px;
    margin-bottom: 12px;
}}

.card {{
    background: white;
    border-radius: 16px;
    padding: 28px;
    box-shadow:
        0 6px 28px rgba(0,0,0,.08);
}}

h1 {{
    margin-top: 0;
}}

.subtitle {{
    color: #666;
    margin-bottom: 24px;
}}

.identity {{
    border: 1px solid #e3e3e3;
    border-radius: 12px;
    padding: 22px;
}}

.name {{
    font-size: 28px;
    font-weight: 800;
}}

.spotify-id {{
    color: #666;
    margin-top: 5px;
}}

.columns {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 18px;
    margin-top: 20px;
}}

.panel {{
    background: #fafafa;
    border-radius: 10px;
    padding: 16px 18px;
}}

.panel h3 {{
    margin-top: 0;
}}

.panel ul {{
    padding-left: 20px;
    line-height: 1.6;
}}

.section-title {{
    font-size: 19px;
    font-weight: 800;
    margin: 28px 0 12px;
}}

.candidate {{
    display: block;
    border: 2px solid #e5e5e5;
    border-radius: 12px;
    padding: 18px;
    margin-bottom: 14px;
    cursor: pointer;
}}

.candidate:has(
    input[type="radio"]:checked
) {{
    border-color: #174ea6;
    background: #f5f8ff;
}}

.candidate-top {{
    display: flex;
    align-items: center;
    gap: 10px;
}}

.candidate-number {{
    width: 28px;
    height: 28px;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: 50%;
    background: #eee;
    font-weight: 800;
}}

.candidate-url {{
    font-weight: 700;
    overflow-wrap: anywhere;
}}

.profile-image-wrap {{
    margin: 16px 0 12px 40px;
}}

.profile-image {{
    width: 190px;
    height: 190px;
    object-fit: cover;
    border-radius: 10px;
    background: #eee;
    box-shadow:
        0 4px 16px rgba(0,0,0,.12);
}}

.profile-meta {{
    margin: 10px 0 8px 40px;
    line-height: 1.6;
}}

.meta-line {{
    margin: 3px 0;
}}

.badges {{
    margin: 12px 0 6px 40px;
}}

.badge {{
    display: inline-block;
    padding: 5px 8px;
    margin: 0 6px 5px 0;
    background: #eee;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 700;
}}

.evidence {{
    margin-left: 23px;
    color: #555;
    line-height: 1.5;
}}

.link {{
    display: inline-block;
    margin-left: 40px;
    padding: 9px 12px;
    background: #eef4ff;
    color: #174ea6;
    text-decoration: none;
    border-radius: 8px;
    font-weight: 650;
}}

.actions {{
    display: flex;
    gap: 14px;
    margin-top: 26px;
}}

button {{
    flex: 1;
    padding: 17px;
    border: 0;
    border-radius: 10px;
    font-size: 17px;
    font-weight: 800;
    cursor: pointer;
}}

.accept {{
    background: #d9f7df;
}}

.reject {{
    background: #ffdede;
}}

.unresolved {{
    background: #fff0c9;
}}

.hint {{
    text-align: center;
    color: #666;
    margin-top: 14px;
    line-height: 1.5;
}}

.muted {{
    color: #888;
}}

@media (max-width: 760px) {{
    .columns {{
        grid-template-columns: 1fr;
    }}

    .actions {{
        flex-direction: column;
    }}
}}
</style>
</head>

<body>

<div class="wrap">

<div class="progress">
Artist {position} of {len(rows)}
· {len(state["pending"])} remaining
</div>

<div class="card">

<h1>WhoSampled Artist Identity Review</h1>

<div class="subtitle">
Accepting an identity permits future artist-catalog
collection. Accepted slugs are learned automatically.
</div>

<div class="identity">

<div class="name">
{esc(row.get("spotify_artist_name"))}
</div>

<div class="spotify-id">
Review key:
{esc(row.get("review_key") or row.get("spotify_artist_id"))}
</div>

<div class="columns">

<div class="panel">
<h3>Spotify comparison</h3>

{
    (
        '<div><strong>'
        + esc(
            row.get(
                "spotify_candidate_artist_name"
            )
            or row.get(
                "spotify_artist_name"
            )
        )
        + '</strong></div>'
        + (
            '<div>Spotify ID: '
            + esc(
                row.get(
                    "spotify_candidate_artist_id"
                )
                or row.get(
                    "spotify_artist_id"
                )
            )
            + '</div>'
            if (
                row.get(
                    "spotify_candidate_artist_id"
                )
                or row.get(
                    "spotify_artist_id"
                )
            )
            else ''
        )
        + (
            '<div><a href="'
            + esc(
                row.get(
                    "spotify_candidate_url"
                )
                or row.get(
                    "spotify_url"
                )
            )
            + '" target="_blank">'
            + 'Open Spotify artist'
            + '</a></div>'
            if (
                row.get(
                    "spotify_candidate_url"
                )
                or row.get(
                    "spotify_url"
                )
            )
            else (
                '<div><em>'
                'No local Spotify identity. '
                'Spotify reconciliation remains unresolved.'
                '</em></div>'
            )
        )
        + (
            '<div>Status: '
            + esc(
                row.get(
                    "spotify_candidate_status"
                )
            )
            + '</div>'
            if row.get(
                "spotify_candidate_status"
            )
            else ''
        )
    )
}

</div>

<div class="panel">
<h3>WhoSampled identity</h3>
<div>
<strong>
{esc(row.get("spotify_artist_name"))}
</strong>
</div>
<div>
Review the WhoSampled candidate profile below.
</div>
</div>

</div>

<div class="columns">

<div class="panel">
<h3>Unresolved tracks</h3>
<ul>
{unresolved}
</ul>
</div>

<div class="panel">
<h3>Verified identity evidence</h3>
<ul>
{verified}
</ul>
</div>

</div>
</div>

<div class="section-title">
WhoSampled artist candidates
</div>

<form
    id="review-form"
    method="post"
    action="/decide">

{candidates}

<input
    type="hidden"
    id="decision"
    name="decision"
    value="">

<div class="actions">

<button
    class="accept"
    type="button"
    onclick="submitDecision('accepted')">
ACCEPT (Y)
</button>

<button
    class="reject"
    type="button"
    onclick="submitDecision('rejected')">
REJECT (N)
</button>

<button
    class="unresolved"
    type="button"
    onclick="submitDecision('unresolved')">
UNRESOLVED (M)
</button>

</div>

</form>

<div class="hint">
Y = accept selected candidate
· N = reject
· M = unresolved
<br>
1–9 selects a candidate.
Candidate 1 is selected initially.
</div>

</div>
</div>

<script>

function selectCandidate(number) {{

    const radios =
        document.querySelectorAll(
            'input[name="candidate"]'
        );

    const index =
        number - 1;

    if (
        index >= 0
        && index < radios.length
    ) {{
        radios[
            index
        ].checked = true;
    }}
}}

function submitDecision(decision) {{

    document.getElementById(
        "decision"
    ).value = decision;

    document.getElementById(
        "review-form"
    ).submit();
}}

document.addEventListener(
    "keydown",
    function(event) {{

        const key =
            event.key.toLowerCase();

        if (
            /^[1-9]$/.test(
                key
            )
        ) {{
            selectCandidate(
                parseInt(key)
            );

            return;
        }}

        if (key === "y") {{
            submitDecision(
                "accepted"
            );
        }}

        if (key === "n") {{
            submitDecision(
                "rejected"
            );
        }}

        if (key === "m") {{
            submitDecision(
                "unresolved"
            );
        }}
    }}
);

</script>

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
                    len(encoded)
                ),
            )

            self.end_headers()

            self.wfile.write(
                encoded
            )

        def redirect(
            self,
            path="/",
        ):
            self.send_response(
                303
            )

            self.send_header(
                "Location",
                path,
            )

            self.end_headers()

        def do_GET(self):

            if self.path == "/":

                self.send_html(
                    render_page()
                )

                return

            if self.path == "/complete":

                self.send_html(
                    render_page()
                )

                threading.Timer(
                    0.4,
                    server.shutdown,
                ).start()

                return

            self.send_response(
                404
            )

            self.end_headers()

        def do_POST(self):

            if (
                self.path
                != "/decide"
            ):
                self.send_response(
                    404
                )

                self.end_headers()

                return

            length = int(
                self.headers.get(
                    "Content-Length",
                    "0",
                )
            )

            raw = (
                self.rfile.read(
                    length
                )
                .decode(
                    "utf-8"
                )
            )

            form = parse_qs(
                raw
            )

            decision = clean(
                form.get(
                    "decision",
                    [""],
                )[0]
            )

            row = current_row()

            if row is None:
                self.redirect(
                    "/complete"
                )
                return

            candidate_index = None

            if decision == "accepted":

                try:
                    candidate_index = int(
                        clean(
                            form.get(
                                "candidate",
                                ["0"],
                            )[0]
                        )
                    )

                except Exception:
                    candidate_index = 0

            if decision not in (
                "accepted",
                "rejected",
                "unresolved",
            ):
                self.redirect()
                return

            if not persist_decision(
                row,
                decision,
                candidate_index,
            ):
                self.redirect()
                return

            if not state[
                "pending"
            ]:
                self.redirect(
                    "/complete"
                )

            else:
                self.redirect()

    server = (
        ThreadingHTTPServer(
            (
                "127.0.0.1",
                0,
            ),
            Handler,
        )
    )

    port = (
        server
        .server_address[
            1
        ]
    )

    url = (
        f"http://127.0.0.1:"
        f"{port}/"
    )

    print()
    print(
        "Browser:",
        url,
    )

    print(
        "Y = accept | "
        "N = reject | "
        "M = unresolved"
    )

    print(
        "1–9 = select "
        "WhoSampled candidate"
    )

    print(
        "Pipeline is paused until "
        "artist review is complete."
    )

    threading.Timer(
        0.4,
        lambda: webbrowser.open(
            url
        ),
    ).start()

    try:
        server.serve_forever()

    finally:
        server.server_close()

    time.sleep(
        0.2
    )

    print()
    print(
        "ARTIST CATALOG REVIEW COMPLETE."
    )

    counts = {
        "accepted": 0,
        "rejected": 0,
        "unresolved": 0,
    }

    for review in (
        state[
            "reviews"
        ].values()
    ):
        decision = clean(
            review.get(
                "decision"
            )
        )

        if decision in counts:
            counts[
                decision
            ] += 1

    print(
        "accepted:",
        counts[
            "accepted"
        ],
    )

    print(
        "rejected:",
        counts[
            "rejected"
        ],
    )

    print(
        "unresolved:",
        counts[
            "unresolved"
        ],
    )

    return state[
        "reviews"
    ]
