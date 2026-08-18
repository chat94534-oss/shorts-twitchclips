#!/usr/bin/env python3
"""One-time OAuth authorization for the Shorts uploader.

Opens a browser, has you approve the app for the channel's own Google
account, and writes the resulting refresh token to token.json. Run this
once; youtube_upload.py reuses token.json after that.

While the OAuth app is in "Testing" mode, the refresh token expires after
7 days, so you'd need to re-run this weekly. Once the consent screen is
published ("In production"), the refresh token does not expire.
"""
import os
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    # force-ssl, not readonly: the pipeline edits titles and descriptions on
    # videos it already posted. It is a superset of readonly.
    "https://www.googleapis.com/auth/youtube.force-ssl",
]
CLIENT_SECRETS_FILE = "client_secret.json"
TOKEN_FILE = "token.json"


def main():
    if not os.path.exists(CLIENT_SECRETS_FILE):
        raise SystemExit(
            f"Missing {CLIENT_SECRETS_FILE}. Download it from the Google Cloud "
            "Console (OAuth client, Desktop app) and place it next to this script."
        )

    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
    # port=0 lets the OS pick a free port for the local redirect listener.
    # open_browser=True auto-opens the system default browser for consent.
    creds = flow.run_local_server(port=0, prompt="consent", open_browser=True)

    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(creds.to_json())

    print(f"\nAuthorization complete. Token saved to {TOKEN_FILE}.")
    print("You can now run youtube_upload.py to upload videos.")


if __name__ == "__main__":
    main()
