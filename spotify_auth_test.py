import os
from dotenv import load_dotenv
import spotipy
from spotipy.oauth2 import SpotifyOAuth
load_dotenv()

client_id = os.getenv("SPOTIFY_CLIENT_ID")
client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")

if not client_id or not client_secret:
    raise EnvironmentError("Spotify credentials are missing")

sp = spotipy.Spotify(
    auth_manager=SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri="http://127.0.0.1:8888/callback",
        scope="playlist-read-private playlist-read-collaborative"
    )
)

user = sp.current_user()

print()
print("AUTHENTICATION SUCCESS")
print("Spotify user:", user["display_name"])
print("Spotify ID:", user["id"])
print()

