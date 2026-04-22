# agent-podcast-recommendations

A Python agent that monitors podcast episodes on Spotify, extracts music recommendations from show notes, searches for each track on Spotify using fuzzy matching, and automatically adds matched tracks to a dedicated Spotify playlist. Runs on a scheduled cron job and sends an email notification whenever new tracks are added.

## What it does
- Monitors podcasts listed in `podcasts_sources.txt`
- Parses show notes for track recommendations
- Searches Spotify and adds matched tracks to a playlist
- Sends an email notification when new tracks are added

## Requirements
- Python 3.11+  (Refer to how to set up here: https://docs.conda.io/en/latest/miniconda.html)
- A Spotify Developer account and app
- A Gmail account with an App Password enabled

## Setup

### 1. Clone the repo
```bash
git clone https://github.com/singingdata/agent-podcast-recommendations.git
cd agent-podcast-recommendations
```

### 2. Create a Spotify Developer app
1. Go to [developer.spotify.com](https://developer.spotify.com) → Dashboard → Create App
2. Set Redirect URI to `http://localhost:8888/callback`
3. Note your Client ID and Client Secret — you'll need them in the next step

### 3. Configure environment variables
Create a `.env` file one level above this folder. 
Reference this .env file how-to if you need to:  https://www.geeksforgeeks.org/python/how-to-create-and-use-env-files-in-python/
The expected directory structure is:

```
parent-folder/
├── .env                              ← credentials go here
└── agent-podcast-recommendations/
    ├── agent-script-fetch-recommendations-to-my-libraries.py
    └── ...
```

Add the following variables to `.env`:

| Variable | Description |
|---|---|
| `SPOTIFY_CLIENT_ID` | From your Spotify Developer app |
| `SPOTIFY_CLIENT_SECRET` | From your Spotify Developer app |
| `SPOTIFY_REDIRECT_URI` | Set to `http://localhost:8888/callback` |
| `GMAIL_ADDRESS` | Gmail address used to send notifications |
| `GMAIL_APP_PASSWORD` | Gmail App Password — see note below |
| `NOTIFICATION_EMAIL` | Where to send new-track notifications |

> **Gmail App Password:** This is not your regular Gmail password. Generate one at
> [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
> Requires 2-Step Verification to be enabled on your Google account.

### 4. Add podcasts to monitor
Edit `podcasts_sources.txt`. One podcast per line:
```
show_id | Playlist Name | Description for the playlist
```

To find a podcast's Spotify show ID, open it in Spotify and copy the ID from the URL:
```
https://open.spotify.com/show/<SHOW_ID>
```

### 5. Install dependencies
Navigate to your python environment in the terminal.  
Install the dependencies from the requirements.txt file.
```bash
pip install -r requirements.txt
```
> Dependencies are also installed automatically on first run if not already present.

### 6. First run (browser authorisation required)
```bash
python agent-script-fetch-recommendations-to-my-libraries.py
```
A browser window will open asking you to authorise the app with your Spotify account.
After approving, the token is cached in `.spotify_token_cache` and all subsequent runs
are fully automated with no browser interaction required.

### 7. Schedule (optional)
See `cron-jobs-schedule.txt` for the cron job configuration used with OpenClaw.

## File structure

| File | Purpose |
|---|---|
| `agent-script-fetch-recommendations-to-my-libraries.py` | Main script |
| `podcasts_sources.txt` | Podcast list |
| `requirements.txt` | Python dependencies |
| `cron-jobs-schedule.txt` | Cron schedule (documentation only) |
| `.gitignore` | Excludes token cache and admin files from git |
| `administrative-files/state_*.json` | Per-podcast run state (auto-created) |
| `administrative-files/agent.log` | Run history (auto-created) |
| `administrative-files/unmatched_*.log` | Tracks that couldn't be matched (auto-created) |

## Notes
- `.spotify_token_cache` is excluded from git — do not commit it
- `administrative-files/` is excluded from git — it is created automatically on first run
- All API keys live in the `.env` file, not in this repo
