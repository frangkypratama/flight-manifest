#!/usr/bin/env python3
"""Script one-time untuk OAuth Gmail. Jalankan sebelum main.py."""

import pickle
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_PATH = BASE_DIR / "credentials.json"
TOKEN_PATH = BASE_DIR / "token.pickle"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def main() -> None:
    if not CREDENTIALS_PATH.exists():
        print(f"✗ credentials.json tidak ditemukan di {CREDENTIALS_PATH}")
        print("  Unduh OAuth client credentials (Desktop App) dari Google Cloud Console")
        print(f"  dan simpan sebagai {CREDENTIALS_PATH}, lalu jalankan ulang script ini.")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
    creds = flow.run_local_server(port=0)

    with TOKEN_PATH.open("wb") as f:
        pickle.dump(creds, f)

    print(f"✅ Login Gmail berhasil. Token disimpan di {TOKEN_PATH}")


if __name__ == "__main__":
    main()
