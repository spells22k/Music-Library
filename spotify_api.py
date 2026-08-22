#!/usr/bin/env python3

"""
Spotify Playlist Exporter

Usage:
    python spotify_playlist_export.py "https://open.spotify.com/playlist/PLAYLIST_ID"

Optional:
    python spotify_playlist_export.py "PLAYLIST_URL" --output my_playlist.csv

Required environment variables:
    SPOTIFY_CLIENT_ID
    SPOTIFY_CLIENT_SECRET
"""

import argparse
import os

import pandas as pd
import spotipy
from spotipy.oauth2 import SpotifyOAuth


def get_arguments():
    parser = argparse.ArgumentParser(
        description="Export Spotify playlist metadata to CSV"
    )

    parser.add_argument(
        "playlist_url",
        nargs="?",
        help="Spotify playlist URL"
    )

    parser.add_argument(
        "--output",
        default="spotify_playlist_export.csv",
        help="Output CSV filename"
    )

    args = parser.parse_args()

    if args.playlist_url:
        playlist_url = args.playlist_url
    else:
        playlist_url = input(
            "Paste Spotify playlist URL: "
        ).strip()

    return playlist_url, args.output


def main():

    playlist_url, output_file = get_arguments()

    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise EnvironmentError(
            """
Spotify credentials missing.

Set these environment variables:

SPOTIFY_CLIENT_ID
SPOTIFY_CLIENT_SECRET
"""
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

    results = sp.playlist_tracks(playlist_url)

    tracks = []

    while results:

        for item in results["items"]:

            track = item.get("track")

            if track is None:
                continue

            tracks.append(
                {
                    "spotify_track_id": track["id"],
                    "spotify_uri": track["uri"],
                    "title": track["name"],
                    "album": track["album"]["name"],
                    "album_id": track["album"]["id"],
                    "release_date": track["album"]["release_date"],
                    "isrc": track.get(
                        "external_ids",
                        {}
                    ).get("isrc"),

                    "artist_names": ", ".join(
                        artist["name"]
                        for artist in track["artists"]
                    ),

                    "artist_ids": ", ".join(
                        artist["id"]
                        for artist in track["artists"]
                    ),

                    "duration_ms": track["duration_ms"]
                }
            )

        if results["next"]:
            results = sp.next(results)
        else:
            results = None


    df = pd.DataFrame(tracks)

    df.to_csv(
        output_file,
        index=False
    )

    print()
    print("Export complete!")
    print(f"Tracks exported: {len(df)}")
    print(f"File created: {output_file}")


if __name__ == "__main__":
    main()