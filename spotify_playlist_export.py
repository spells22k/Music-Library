import os
import json
import argparse
import pandas as pd
import spotipy
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyOAuth

load_dotenv()

parser = argparse.ArgumentParser(
    description="Export Spotify playlist metadata to CSV"
)

parser.add_argument(
    "playlist_url",
    help="Spotify playlist URL"
)

parser.add_argument(
    "--output",
    default="spotify_playlist_input.csv",
    help="Output CSV filename"
)

args = parser.parse_args()

client_id = os.getenv("SPOTIFY_CLIENT_ID")
client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")

if not client_id or not client_secret:
    raise EnvironmentError(
        "Spotify credentials are missing"
    )

sp = spotipy.Spotify(
    auth_manager=SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri="http://127.0.0.1:8888/callback",
        scope="playlist-read-private playlist-read-collaborative"
    )
)

print("Retrieving playlist...")

playlist = sp.playlist(args.playlist_url)
playlist_id = playlist["id"]

results = sp.playlist_items(
    args.playlist_url,
    limit=100,
    additional_types=["track"]
)

rows = []

while results:

    for entry in results["items"]:

        item = entry.get("item")

        if not item or item.get("type") != "track":
            continue

        album = item.get("album", {})
        artists = item.get("artists", [])

        spotify_id = item.get("id")

        rows.append({
            "playlist_id": playlist_id,
            "playlist_url": args.playlist_url,

            "spotify_track_id": spotify_id,
            "spotify_uri": item.get("uri"),
            "spotify_url": (
                f"https://open.spotify.com/track/{spotify_id}"
                if spotify_id
                else None
            ),

            "isrc": (
                item.get("external_ids", {})
                .get("isrc")
            ),

            "title": item.get("name"),

            "artist_names": ", ".join(
                artist.get("name", "")
                for artist in artists
            ),

            "artist_ids": ", ".join(
                artist.get("id", "")
                for artist in artists
            ),

            # Preserve Spotify's structured artist identities.
            # The legacy display columns above remain for readability,
            # but must not be used to reconstruct artist boundaries
            # because artist names themselves may contain commas.
            "artists_json": json.dumps(
                [
                    {
                        "id": artist.get("id"),
                        "name": artist.get("name"),
                        "uri": artist.get("uri"),
                        "spotify_url": (
                            artist
                            .get("external_urls", {})
                            .get("spotify")
                        ),
                    }
                    for artist in artists
                ],
                ensure_ascii=False,
            ),

            "album_name": album.get("name"),
            "album_id": album.get("id"),

            "album_release_date": (
                album.get("release_date")
            ),

            "album_release_precision": (
                album.get("release_date_precision")
            ),

            "album_image_url": (
                album.get("images", [{}])[0].get("url")
                if album.get("images")
                else None
            ),

            "album_label": album.get("label"),

            "duration_ms": item.get("duration_ms"),
        })

    if results.get("next"):
        results = sp.next(results)
    else:
        results = None


df = pd.DataFrame(rows)

df.to_csv(
    args.output,
    index=False,
    encoding="utf-8"
)

print()
print("SPOTIFY INGESTION COMPLETE")
print("==========================")
print("Playlist:", playlist_id)
print("Tracks:", len(df))
print("Output:", args.output)
print()

print(
    df[
        [
            "title",
            "spotify_track_id",
            "isrc",
            "album_label"
        ]
    ].to_string(index=False)
)
