import os
from dotenv import load_dotenv
import spotipy
from spotipy.oauth2 import SpotifyOAuth

load_dotenv()

sp = spotipy.Spotify(
    auth_manager=SpotifyOAuth(
        client_id=os.getenv("SPOTIFY_CLIENT_ID"),
        client_secret=os.getenv("SPOTIFY_CLIENT_SECRET"),
        redirect_uri="http://127.0.0.1:8888/callback",
        scope="playlist-read-private playlist-read-collaborative"
    )
)

playlist_url = input("Paste Spotify playlist URL: ").strip()

results = sp.playlist_tracks(playlist_url)

print()
print("PLAYLIST ACCESS SUCCESS")
print("Items returned:", len(results["items"]))
print()

for i, entry in enumerate(results["items"], 1):
    item = entry.get("item")
    if not item or item.get("type") != "track":
        print(f"{i}. [not a track]")
        continue

    print(
        f"{i}. {item["name"]} | "
        f"{item["id"]} | "
        f"{item.get("external_ids", {}).get("isrc", "NO ISRC")}"
    )
