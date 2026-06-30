#!/usr/bin/env python3
# ============================================================
# scripts/upstox_login.py   (CORRECTED — truthful + verify-on-write)
#
# Run ONCE every morning before 9:15 AM IST.
# Generates a fresh Upstox access token, saves it to Azure
# PostgreSQL, and VERIFIES the write actually landed.
#
# WHAT CHANGED vs the old version (and why it matters):
#   - The OLD browser page said "Login successful! Token saved"
#     the instant Upstox redirected back — BEFORE the token was
#     exchanged or saved. So a silent DB-write failure (e.g. your
#     IP not allow-listed on the Azure firewall) still showed
#     "success". That is exactly why the scheduler ran on stale
#     yfinance data for a week without anyone noticing.
#   - NOW: the browser page only says "code received — finishing
#     in the terminal". The TERMINAL is the source of truth, and
#     we READ THE TOKEN BACK from the DB to confirm it is really
#     stored and valid before declaring success.
#
# Usage:
#   python scripts/upstox_login.py
# ============================================================

import os
import sys
import webbrowser
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
from dotenv import load_dotenv

# Ensure project root is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv()

API_KEY      = os.getenv("UPSTOX_API_KEY", "")
API_SECRET   = os.getenv("UPSTOX_API_SECRET", "")
REDIRECT_URI = "http://127.0.0.1:8000/callback"
AUTH_URL     = "https://api.upstox.com/v2/login/authorization/dialog"
TOKEN_URL    = "https://api.upstox.com/v2/login/authorization/token"


# ============================================================
# STEP 1 — Generate login URL
# ============================================================

def get_login_url() -> str:
    params = {
        "client_id":     API_KEY,
        "redirect_uri":  REDIRECT_URI,
        "response_type": "code",
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


# ============================================================
# STEP 2 — Local server to catch the OAuth callback
# NOTE: the callback page is now NEUTRAL. It does NOT claim the
# token was saved, because at this point it has not been.
# ============================================================

auth_code = None

class CallbackHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        global auth_code
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"""
                <html><body style='font-family:sans-serif;text-align:center;padding:60px;'>
                <h2 style='color:#1a73e8;'>Authorization received.</h2>
                <p>Finishing token exchange and saving&hellip;</p>
                <p><b>Check the terminal window</b> for the real result
                   (success or failure).</p>
                <p>You can close this tab.</p>
                </body></html>
            """)
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Error: no code received")

    def log_message(self, format, *args):
        pass


# ============================================================
# STEP 3 — Exchange code for access token
# ============================================================

def exchange_code(code: str) -> str | None:
    try:
        response = requests.post(TOKEN_URL, data={
            "code":          code,
            "client_id":     API_KEY,
            "client_secret": API_SECRET,
            "redirect_uri":  REDIRECT_URI,
            "grant_type":    "authorization_code",
        }, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept":       "application/json",
        }, timeout=10)

        if response.status_code != 200:
            print(f"Token exchange failed: {response.status_code} — {response.text}")
            return None

        return response.json().get("access_token")

    except Exception as e:
        print(f"Token exchange error: {e}")
        return None


# ============================================================
# Helpers for truthful banners
# ============================================================

def banner(lines, char="="):
    width = 58
    print(char * width)
    for ln in lines:
        print("  " + ln)
    print(char * width)


def fail(lines):
    print()
    banner(["LOGIN FAILED — TOKEN NOT SAVED"] + lines, char="!")
    print()
    sys.exit(1)


# ============================================================
# MAIN
# ============================================================

def main():
    if not API_KEY or not API_SECRET:
        fail(["UPSTOX_API_KEY and UPSTOX_API_SECRET must be set in .env"])

    banner(["Upstox Daily Login — Algo Trading Platform"])
    print()

    login_url = get_login_url()
    print("Opening Upstox login in your browser...")
    print("If it doesn't open, visit this URL manually:")
    print(f"\n  {login_url}\n")
    webbrowser.open(login_url)

    print("Waiting for login callback on http://127.0.0.1:8000 ...")
    print("(Log in with your Upstox credentials in the browser)\n")

    server = HTTPServer(("127.0.0.1", 8000), CallbackHandler)
    server.timeout = 120
    server.handle_request()

    if not auth_code:
        fail(["No authorization code received from Upstox.",
              "Try again — make sure you completed the login in the browser."])

    print("Authorization code received. Exchanging for access token...")
    token = exchange_code(auth_code)

    if not token:
        fail(["Could not exchange code for an access token.",
              "Check UPSTOX_API_KEY / UPSTOX_API_SECRET in .env."])

    print("Access token received. Saving to Azure PostgreSQL...")

    # ---- WRITE ----
    try:
        from core.database.db import save_upstox_token, get_upstox_token
    except Exception as e:
        fail([f"Could not import the database layer: {e}"])

    try:
        saved = save_upstox_token(token)
    except Exception as e:
        # Most common real-world cause: DB unreachable from this network.
        fail([f"Database write raised an error: {e}",
              "",
              "MOST LIKELY CAUSE: your current IP is not allow-listed on the",
              "Azure PostgreSQL firewall (your home/office IP changes daily).",
              "Fix: Azure Portal -> ariqt-algo-trading-db-001 -> Networking",
              "     -> '+ Add current client IP address' -> Save -> re-run."])

    if not saved:
        fail(["save_upstox_token() returned False — the row was not written.",
              "Check DATABASE_URL and the Azure firewall allow-list."])

    # ---- VERIFY (read it back) ----
    # This is the crucial new step: prove the token is actually in the DB
    # and reads back as VALID, instead of trusting the write blindly.
    print("Verifying the token is stored and valid...")
    try:
        readback = get_upstox_token()
    except Exception as e:
        fail([f"Wrote the token but could not read it back: {e}"])

    if not readback:
        fail(["Token was written but reads back as INVALID/EXPIRED.",
              "This points to a token-validity (timezone) issue in",
              "get_upstox_token(), not a write failure. Tell the developer."])

    # ---- TRUE SUCCESS ----
    print()
    banner(["SUCCESS — token saved AND verified in Azure PostgreSQL",
            "The scheduler will use live Upstox data today.",
            "Market opens at 9:15 AM IST."])
    print()


if __name__ == "__main__":
    main()