"""
Red Hot Beverly — Fundraising Gauge
Flask backend with Google Sheets integration for live "raised" amount.

Google Sheets setup (two modes):
─────────────────────────────────
MODE A – Public sheet (simplest):
  1. In Google Sheets: File → Share → "Anyone with the link" = Viewer
  2. Set SHEETS_MODE=public in .env (or environment)
  3. Set SPREADSHEET_ID and RAISED_CELL (e.g. "Sheet1!B2")

MODE B – Private sheet (service account):
  1. Create a Google Cloud project, enable Sheets API
  2. Create a Service Account, download JSON key → save as credentials.json
  3. Share the sheet with the service account email
  4. Set SHEETS_MODE=service_account in .env
  5. Set SPREADSHEET_ID, RAISED_CELL, GOOGLE_CREDS_FILE path

Environment variables (put in .env or export before running):
  SHEETS_MODE          = public | service_account   (default: public)
  SPREADSHEET_ID       = 1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms  (example)
  RAISED_CELL          = Sheet1!B2                  (default: Sheet1!B2)
  SHEETS_CACHE_SECONDS = 60                         (how often to re-fetch, default: 60)
  GOOGLE_CREDS_FILE    = credentials.json           (service account only)
"""

import csv
import io
import json
import os
import time
import threading

import requests
from flask import Flask, jsonify, request, render_template

# ── Load .env if present ───────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv optional

app = Flask(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
DATA_FILE    = os.path.join(os.path.dirname(__file__), "data", "fundraiser.json")
DEFAULT_DATA = {"goal": 10000, "raised": 0}

SHEETS_MODE     = os.environ.get("SHEETS_MODE", "public")
SPREADSHEET_ID  = os.environ.get("SPREADSHEET_ID", "")
RAISED_CELL     = os.environ.get("RAISED_CELL", "Sheet1!B2")
CACHE_SECONDS   = int(os.environ.get("SHEETS_CACHE_SECONDS", "60"))
CREDS_FILE      = os.environ.get("GOOGLE_CREDS_FILE", "credentials.json")

# ── In-memory sheets cache ──────────────────────────────────────────────────────
_sheets_cache = {"value": None, "fetched_at": 0, "error": None}
_cache_lock   = threading.Lock()


# ── JSON file helpers ───────────────────────────────────────────────────────────
def read_data():
    if not os.path.exists(DATA_FILE):
        return DEFAULT_DATA.copy()
    with open(DATA_FILE) as f:
        return json.load(f)


def write_data(data):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ── Google Sheets helpers ───────────────────────────────────────────────────────
def _parse_cell_ref(cell_ref):
    """Parse 'Sheet1!B2' into (sheet_name, col_index, row_index) zero-based."""
    if "!" in cell_ref:
        sheet, addr = cell_ref.split("!", 1)
    else:
        sheet, addr = "Sheet1", cell_ref
    col_str = "".join(c for c in addr if c.isalpha()).upper()
    row_str = "".join(c for c in addr if c.isdigit())
    col_idx = 0
    for ch in col_str:
        col_idx = col_idx * 26 + (ord(ch) - ord("A") + 1)
    col_idx -= 1
    row_idx = int(row_str) - 1
    return sheet, col_idx, row_idx


def _fetch_public(spreadsheet_id, sheet_name, col_idx, row_idx):
    """Fetch a cell from a publicly shared Google Sheet via CSV export."""
    url = (
        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={requests.utils.quote(sheet_name)}"
    )
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    rows = list(csv.reader(io.StringIO(resp.text)))
    if row_idx >= len(rows):
        raise ValueError(f"Row {row_idx+1} out of range (sheet has {len(rows)} rows)")
    row = rows[row_idx]
    if col_idx >= len(row):
        raise ValueError(f"Col {col_idx+1} out of range (row has {len(row)} cols)")
    return row[col_idx]


def _fetch_service_account(spreadsheet_id, cell_ref, creds_file):
    """Fetch a cell using a service account + Sheets API v4."""
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError:
        raise ImportError(
            "Run: pip install google-auth google-api-python-client"
        )
    scopes  = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds   = service_account.Credentials.from_service_account_file(creds_file, scopes=scopes)
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    result  = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=cell_ref
    ).execute()
    values = result.get("values", [])
    if not values or not values[0]:
        raise ValueError(f"Cell {cell_ref} is empty")
    return values[0][0]


def fetch_raised_from_sheets():
    """
    Returns (float|None, error_str|None).
    Uses an in-memory cache; re-fetches only after CACHE_SECONDS.
    """
    if not SPREADSHEET_ID:
        return None, "SPREADSHEET_ID not set — add it to your .env file"

    with _cache_lock:
        age = time.time() - _sheets_cache["fetched_at"]
        if age < CACHE_SECONDS and _sheets_cache["fetched_at"] > 0:
            return _sheets_cache["value"], _sheets_cache["error"]

    try:
        sheet_name, col_idx, row_idx = _parse_cell_ref(RAISED_CELL)
        if SHEETS_MODE == "service_account":
            raw = _fetch_service_account(SPREADSHEET_ID, RAISED_CELL, CREDS_FILE)
        else:
            raw = _fetch_public(SPREADSHEET_ID, sheet_name, col_idx, row_idx)

        cleaned = raw.replace("$", "").replace(",", "").replace(" ", "").strip()
        value   = float(cleaned)
        with _cache_lock:
            _sheets_cache.update({"value": value, "fetched_at": time.time(), "error": None})
        return value, None

    except Exception as exc:
        err = str(exc)
        with _cache_lock:
            _sheets_cache.update({"value": None, "fetched_at": time.time(), "error": err})
        return None, err


def get_live_data():
    """Merge JSON goal with live Sheets raised value (falls back to JSON if Sheets fails)."""
    base = read_data()
    sheets_raised, err = fetch_raised_from_sheets()
    if sheets_raised is not None:
        base["raised"]        = sheets_raised
        base["sheets_source"] = True
        base["sheets_error"]  = None
    else:
        base["sheets_source"] = False
        base["sheets_error"]  = err
    return base


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/admin")
def admin():
    return render_template("admin.html")


@app.route("/embed")
def embed():
    return render_template("embed.html")


@app.route("/api/fundraiser", methods=["GET"])
def get_fundraiser():
    return jsonify(get_live_data())


@app.route("/api/fundraiser", methods=["POST"])
def update_fundraiser():
    body = request.get_json(silent=True) or {}
    data = read_data()

    if "goal" in body:
        goal = float(body["goal"])
        if goal <= 0:
            return jsonify({"error": "goal must be greater than 0"}), 400
        data["goal"] = goal

    if "raised" in body:
        raised = float(body["raised"])
        if raised < 0:
            return jsonify({"error": "raised cannot be negative"}), 400
        data["raised"] = raised

    write_data(data)
    return jsonify(get_live_data())


@app.route("/api/fundraiser/reset", methods=["POST"])
def reset_fundraiser():
    write_data(DEFAULT_DATA.copy())
    return jsonify(get_live_data())


@app.route("/api/sheets/status", methods=["GET"])
def sheets_status():
    """Return Sheets config + last fetch result — used by admin UI."""
    _, err = fetch_raised_from_sheets()
    return jsonify({
        "mode":           SHEETS_MODE,
        "spreadsheet_id": SPREADSHEET_ID,
        "raised_cell":    RAISED_CELL,
        "cache_seconds":  CACHE_SECONDS,
        "configured":     bool(SPREADSHEET_ID),
        "last_error":     err,
        "cached_value":   _sheets_cache.get("value"),
        "cache_age_s":    round(time.time() - _sheets_cache["fetched_at"], 1)
                          if _sheets_cache["fetched_at"] else None,
    })


@app.route("/api/sheets/refresh", methods=["POST"])
def sheets_refresh():
    """Bust cache and force an immediate re-fetch."""
    with _cache_lock:
        _sheets_cache["fetched_at"] = 0
    value, err = fetch_raised_from_sheets()
    return jsonify({"ok": err is None, "error": err, "value": value})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
