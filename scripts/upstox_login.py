#!/usr/bin/env python3
# ============================================================
# scripts/upstox_login.py
#
# Run this ONCE every morning before 9:15 AM IST.
# Generates a fresh Upstox access token and saves it
# to Supabase so the Railway scheduler can use it all day.
#
# Usage:
#   python scripts/upstox_login.py
#
# What it does:
#   1. Generates the Upstox login URL
#   2. Opens it in your browser automatically
#   3. You log in with your Upstox credentials
#   4. Upstox redirects to http://127.0.0.1:8000/callback?code=XXX
#   5. This script catches the code automatically
#   6. Exchanges it for an access token
#   7. Saves token to Supabase
#   8. Done — scheduler will use it for the whole day
#
# Time required: ~30 seconds
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
# STEP 1 — Generate login URL and open browser
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
                <h2 style='color:#1a9e75;'>Login successful!</h2>
                <p>Access token saved to Supabase.</p>
                <p>You can close this tab.</p>
                <p style='color:#888;font-size:13px;'>Scheduler is now ready for market hours.</p>
                </body></html>
            """)
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Error: no code received")

    def log_message(self, format, *args):
        pass  # Suppress server logs


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

        data = response.json()
        return data.get("access_token")

    except Exception as e:
        print(f"Token exchange error: {e}")
        return None


# ============================================================
# MAIN
# ============================================================

def main():
    if not API_KEY or not API_SECRET:
        print("ERROR: UPSTOX_API_KEY and UPSTOX_API_SECRET must be set in .env")
        sys.exit(1)

    print("=" * 55)
    print("  Upstox Daily Login — Algo Trading Platform")
    print("=" * 55)
    print()

    login_url = get_login_url()

    print("Opening Upstox login in your browser...")
    print("If browser doesn't open, visit this URL manually:")
    print(f"\n  {login_url}\n")

    webbrowser.open(login_url)

    print("Waiting for login callback on http://127.0.0.1:8000...")
    print("(Log in with your Upstox credentials in the browser)\n")

    # Start local server to catch callback
    server = HTTPServer(("127.0.0.1", 8000), CallbackHandler)
    server.timeout = 120  # 2 minute timeout
    server.handle_request()  # Wait for exactly one request

    if not auth_code:
        print("ERROR: No authorization code received. Try again.")
        sys.exit(1)

    print("Authorization code received. Exchanging for access token...")

    token = exchange_code(auth_code)

    if not token:
        print("ERROR: Failed to get access token. Check API credentials.")
        sys.exit(1)

    print("Access token received. Saving to Supabase...")

    from data.providers.upstox_provider import save_token
    success = save_token(token)

    if success:
        print()
        print("=" * 55)
        print("  SUCCESS — Token saved to Supabase")
        print("  Scheduler will use Upstox data all day.")
        print("  Market opens at 9:15 AM IST.")
        print("=" * 55)
    else:
        print("ERROR: Failed to save token to Supabase.")
        print("Check SUPABASE_URL and SUPABASE_KEY in .env")
        sys.exit(1)


if __name__ == "__main__":
    main()