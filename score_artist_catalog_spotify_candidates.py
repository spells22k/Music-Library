import argparse
import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd


def clean(value):
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def normalize(value):
    value = clean(value)
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.casefold().replace("&", " and ").replace("’", "'")
    value = re.sub(r"[^a-z0-9\s]", " ", value)
    return " ".join(value.split())


def similarity(a, b):
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def title_without_version(value):
    value = clean(value)
    patterns = [
        r"\s*-\s*\d{4}\s+digital\s+remaster.*$",
        r"\s*-\s*remaster(?:ed|izado|izada)?.*$",
        r"\s*\(\s*remaster(?:ed)?[^)]*\)\s*$",
        r"\s*\[\s*remaster(?:ed)?[^\]]*\]\s*$",
        r"\s*-\s*radio\s+edit\s*$",
        r"\s*-\s*single\s+version\s*$",
        r"\s*-\s*album\s+version\s*$",
    ]
    for pattern in patterns:
        value = re.sub(pattern, "", value, flags=re.I)
    return value.strip()


def parse_year(value):
    match = re.search(r"\b(18|19|20)\d{2}\b", clean(value))
    return int(match.group(0)) if match else None


def parse_int(value):
    try:
        return int(round(float(clean(value))))
    except Exception:
        return None


def parse_spotify_artists(row):
    raw = clean(row.get("artists_json"))
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                names = [
                    clean(x.get("name"))
                    for x in parsed
                    if isinstance(x, dict) and clean(x.get("name"))
                ]
                if names:
                    return names
        except Exception:
            pass
    raw = clean(row.get("artist_names"))
    return [x.strip() for x in raw.split(",") if x.strip()]


def parse_whosampled_artists(value):
    return [x.strip() for x in clean(value).split(",") if x.strip()]


def artist_similarity(ws_artists, spotify_artists):
    ws = [normalize(x) for x in ws_artists if normalize(x)]
    sp = [normalize(x) for x in spotify_artists if normalize(x)]
    if not ws or not sp:
        return 0.0
    if set(ws) == set(sp):
        return 1.0
    forward = [max(SequenceMatcher(None, a, b).ratio() for b in sp) for a in ws]
    reverse = [max(SequenceMatcher(None, b, a).ratio() for a in ws) for b in sp]
    return 0.65 * (sum(forward) / len(forward)) + 0.35 * (sum(reverse) / len(reverse))


def duration_score(ws_ms, sp_ms):
    ws_ms, sp_ms = parse_int(ws_ms), parse_int(sp_ms)
    if ws_ms is None or sp_ms is None:
        return None, None
    diff = abs(ws_ms - sp_ms) / 1000.0
    if diff <= 2:
        score = 1.0
    elif diff <= 5:
        score = 0.95
    elif diff <= 10:
        score = 0.85
    elif diff <= 20:
        score = 0.65
    elif diff <= 30:
        score = 0.45
    elif diff <= 60:
        score = 0.20
    else:
        score = 0.0
    return score, diff


def year_score(ws_year, sp_date):
    a, b = parse_year(ws_year), parse_year(sp_date)
    if a is None or b is None:
        return None
    diff = abs(a - b)
    if diff == 0:
        return 1.0
    if diff <= 2:
        return 0.80
    if diff <= 5:
        return 0.45
    return 0.0


def weighted_score(title, artist, duration, year, album):
    parts = [(title, 0.40), (artist, 0.35), (duration, 0.15), (year, 0.05), (album, 0.05)]
    available = [(score, weight) for score, weight in parts if score is not None]
    if not available:
        return 0.0
    return sum(score * weight for score, weight in available) / sum(weight for _, weight in available)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", default="runs/playlist_3XtRerTr3ndS88v51AAixb")
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--minimum-score", type=float, default=0.45)
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    catalog_file = run_dir / "artist_catalog_recordings_parsed.csv"
    spotify_file = run_dir / "spotify_tracks.csv"
    match_file = run_dir / "matched_tracks.csv"
    output_file = run_dir / "artist_catalog_spotify_candidates.csv"

    for path in (catalog_file, spotify_file, match_file):
        if not path.exists():
            raise SystemExit(f"Missing: {path}")

    catalog_df = pd.read_csv(catalog_file).fillna("")
    spotify_df = pd.read_csv(spotify_file).fillna("")
    match_df = pd.read_csv(match_file).fillna("")

    status_column = next((c for c in ("match_status", "whosampled_match_status") if c in match_df.columns), None)
    if status_column is None:
        raise SystemExit("Could not find match_status or whosampled_match_status in matched_tracks.csv")

    unresolved_statuses = {"unresolved", "not_found", "artist_profile_only", "unmatched", "review"}
    unresolved_ids = set()
    for _, row in match_df.iterrows():
        if clean(row.get(status_column)).casefold() in unresolved_statuses:
            track_id = clean(row.get("spotify_track_id"))
            if track_id:
                unresolved_ids.add(track_id)

    spotify_pool = []
    for _, row in spotify_df.iterrows():
        track_id = clean(row.get("spotify_track_id") or row.get("track_id") or row.get("id"))
        if not track_id or track_id not in unresolved_ids:
            continue
        spotify_pool.append({
            "spotify_track_id": track_id,
            "spotify_title": clean(row.get("title") or row.get("track_name")),
            "spotify_artist_names": parse_spotify_artists(row),
            "spotify_album_name": clean(row.get("album_name")),
            "spotify_album_release_date": clean(row.get("album_release_date")),
            "spotify_duration_ms": clean(row.get("duration_ms")),
            "spotify_url": clean(row.get("spotify_url")),
        })

    print("=" * 100)
    print("ARTIST CATALOG -> UNRESOLVED SPOTIFY OFFLINE CANDIDATE SCORING")
    print("=" * 100)
    print("Catalog recordings:", len(catalog_df))
    print("Unresolved Spotify track IDs:", len(unresolved_ids))
    print("Spotify comparison pool:", len(spotify_pool))

    output_rows = []
    skipped_reconciled = 0

    for _, ws in catalog_df.iterrows():
        recording_id = clean(ws.get("recording_id"))
        ws_url = clean(ws.get("whosampled_url"))
        ws_title = clean(ws.get("whosampled_title"))
        ws_artists = parse_whosampled_artists(ws.get("whosampled_artist_names"))
        ws_album = clean(ws.get("whosampled_album"))
        ws_year = clean(ws.get("whosampled_release_year"))
        ws_duration = clean(ws.get("whosampled_duration_ms"))

        # Canonical reconciliation is terminal for candidate scoring.
        # Once a catalog recording has been accepted/reconciled, later
        # scorer reruns must not reopen it against the remaining unresolved
        # Spotify pool.
        reconciliation_status = clean(
            ws.get("catalog_reconciliation_status")
            or ws.get("canonical_merge_status")
        ).casefold()
        canonical_recording_id = clean(
            ws.get("canonical_recording_id")
        )

        is_reconciled = (
            reconciliation_status in {
                "accepted_reconciled",
                "reconciled",
                "merged",
                "accepted",
            }
            or bool(canonical_recording_id)
        )

        if is_reconciled:
            print("\n" + "-" * 100)
            print("WHOSAMPLED:", ws_title, "—", ", ".join(ws_artists))
            print(
                "SKIPPED — already canonically reconciled"
                + (
                    f" -> {canonical_recording_id}"
                    if canonical_recording_id
                    else ""
                )
            )
            skipped_reconciled += 1
            continue

        scored = []
        for sp in spotify_pool:
            raw_title = similarity(ws_title, sp["spotify_title"])
            normalized_title = similarity(title_without_version(ws_title), title_without_version(sp["spotify_title"]))
            title = max(raw_title, normalized_title)
            artist = artist_similarity(ws_artists, sp["spotify_artist_names"])
            duration, duration_diff = duration_score(ws_duration, sp["spotify_duration_ms"])
            year = year_score(ws_year, sp["spotify_album_release_date"])
            album = similarity(ws_album, sp["spotify_album_name"]) if ws_album and sp["spotify_album_name"] else None
            total = weighted_score(title, artist, duration, year, album)
            scored.append({
                **sp,
                "title_score": title,
                "artist_score": artist,
                "duration_score": duration,
                "duration_difference_seconds": duration_diff,
                "year_score": year,
                "album_score": album,
                "match_score": total,
            })

        scored.sort(key=lambda x: x["match_score"], reverse=True)
        plausible = [x for x in scored if x["match_score"] >= args.minimum_score][:args.top]

        print("\n" + "-" * 100)
        print("WHOSAMPLED:", ws_title, "—", ", ".join(ws_artists))

        base = {
            "recording_id": recording_id,
            "whosampled_url": ws_url,
            "whosampled_title": ws_title,
            "whosampled_artist_names": ", ".join(ws_artists),
            "whosampled_album": ws_album,
            "whosampled_release_year": ws_year,
            "whosampled_duration_ms": ws_duration,
        }

        if not plausible:
            print("No plausible unresolved Spotify candidate.")
            output_rows.append({
                **base, "candidate_rank": "", "spotify_track_id": "", "spotify_title": "",
                "spotify_artist_names": "", "spotify_album_name": "", "spotify_album_release_date": "",
                "spotify_duration_ms": "", "spotify_url": "", "title_score": "", "artist_score": "",
                "duration_score": "", "duration_difference_seconds": "", "year_score": "", "album_score": "",
                "match_score": "", "candidate_status": "no_plausible_candidate", "spotify_review_decision": "",
            })
            continue

        for rank, candidate in enumerate(plausible, start=1):
            print(f"{rank}. {candidate['spotify_title']} — {', '.join(candidate['spotify_artist_names'])} | score: {candidate['match_score']:.4f}")
            output_rows.append({
                **base,
                "candidate_rank": rank,
                "spotify_track_id": candidate["spotify_track_id"],
                "spotify_title": candidate["spotify_title"],
                "spotify_artist_names": ", ".join(candidate["spotify_artist_names"]),
                "spotify_album_name": candidate["spotify_album_name"],
                "spotify_album_release_date": candidate["spotify_album_release_date"],
                "spotify_duration_ms": candidate["spotify_duration_ms"],
                "spotify_url": candidate["spotify_url"],
                "title_score": candidate["title_score"],
                "artist_score": candidate["artist_score"],
                "duration_score": "" if candidate["duration_score"] is None else candidate["duration_score"],
                "duration_difference_seconds": "" if candidate["duration_difference_seconds"] is None else candidate["duration_difference_seconds"],
                "year_score": "" if candidate["year_score"] is None else candidate["year_score"],
                "album_score": "" if candidate["album_score"] is None else candidate["album_score"],
                "match_score": candidate["match_score"],
                "candidate_status": "review",
                "spotify_review_decision": "",
            })

    pd.DataFrame(output_rows).to_csv(output_file, index=False, encoding="utf-8")

    review_ids = {clean(r.get("recording_id")) for r in output_rows if clean(r.get("candidate_status")) == "review"}
    no_candidate_ids = {clean(r.get("recording_id")) for r in output_rows if clean(r.get("candidate_status")) == "no_plausible_candidate"}

    print("\n" + "=" * 100)
    print("CATALOG -> SPOTIFY SCORING COMPLETE")
    print("=" * 100)
    print("Catalog recordings:", len(catalog_df))
    print("Already reconciled recordings skipped:", skipped_reconciled)
    print("Recordings with review candidates:", len(review_ids))
    print("Recordings with no plausible candidate:", len(no_candidate_ids))
    print("Candidate rows:", sum(clean(r.get("candidate_status")) == "review" for r in output_rows))
    print("Output:", output_file)
    print("No Spotify requests were made.")
    print("No identities were merged.")
    print("Every plausible match remains pending contributor review.")


if __name__ == "__main__":
    main()
