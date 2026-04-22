# agent-podcast-recommendations

A Python agent that monitors podcast episodes from a given podcast series, using Spotify, extracts music recommendations from show notes, searches for each track on Spotify using fuzzy matching, and automatically adds matched tracks to a dedicated Spotify playlist. Runs on a scheduled chronilogical job (cron job) and sends an email notification whenever new tracks are added.

## What it does
- Monitors podcasts listed in `podcasts_sources.txt`
- Parses show notes for track recommendations
- Searches Spotify and adds matched tracks to a playlist
- Sends an email notification when new tracks are added

## Requirements
- Python 3.11+ (tested with fastai conda environment)
- A Spotify Developer account and app
- A Gmail account with an App Password enabled

## Setup

### 1. Clone the repo
```bash
git clone https://github.com/singingdata/agent-podcast-recommendations.git
cd agent-podcast-recommendations
```

### 2. Configure environment variables
Add the following to your workspace `.env` file (one level above this folder):

| Variable | Description |
|---|---|
| `SPOTIFY_CLIENT_ID` | From your Spotify Developer app |
| `SPOTIFY_CLIENT_SECRET` | From your Spotify Developer app |
| `SPOTIFY_REDIRECT_URI` | Set to `http://localhost:8888/callback` |
| `GMAIL_ADDRESS` | Gmail address used to send notifications |
| `GMAIL_APP_PASSWORD` | Gmail App Password (not your login password) |
| `NOTIFICATION_EMAIL` | Where to send new-track notifications |

### 3. Create a Spotify Developer app
1. Go to developer.spotify.com → Dashboard → Create App
2. Set Redirect URI to `http://localhost:8888/callback`
3. Copy Client ID and Client Secret into `.env`

### 4. Add podcasts to monitor
Edit `podcasts_sources.txt`. One podcast per line:
    show_id | Playlist Name | Description for the playlist

To find a podcast's Spotify show ID, open it in Spotify and copy the ID from the URL:
    https://open.spotify.com/show/<SHOW_ID>

### 5. First run (browser authorisation required)
