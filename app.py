"""
Red Hot Beverly — Fundraising Gauge
Flask backend with Google Sheets integration for live "raised" amount.

SOURCE MODES  (set RAISED_SOURCE in .env):
  cell    — read a single cell, e.g. Sheet1!B2
  column  — sum all numeric values in a column range, e.g. Sheet1!B2:B

AUTH MODES  (set SHEETS_MODE in .env):
  public          — sheet shared as "Anyone with link can view" (no API key needed)
  service_account — private sheet via a Google service account credentials.json

Environment variables (.env or export):
  SHEETS_MODE          = public | service_account   (default: public)
  SPREADSHEET_ID       = <your spreadsheet id>
  RAISED_SOURCE        = cell | column              (default: cell)
  RAISED_CELL          = Sheet1!B2                  (cell mode: which cell to read)
  RAISED_COLUMN        = Sheet1!B2:B                (column mode: range to sum)
  SHEETS_CACHE_SECONDS = 60
  GOOGLE_CREDS_FILE    = credentials.json           (service_account only)
"""

import csv
import io
import json
import os
import re
import time
import threading

import requests
from flask import Flask, jsonify, request, render_template

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
DATA_FILE    = os.path.join(os.path.dirname(__file__), "data", "fundraiser.json")
DEFAULT_DATA = {"goal": 10000, "raised": 0}

SHEETS_MODE    = os.environ.get("SHEETS_MODE",    "public")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "")
RAISED_SOURCE  = os.environ.get("RAISED_SOURCE",  "cell")    # "cell" or "column"
RAISED_CELL    = os.environ.get("RAISED_CELL",    "Sheet1!B2")
RAISED_COLUMN  = os.environ.get("RAISED_COLUMN",  "Sheet1!B2:B")
CACHE_SECONDS  = int(os.environ.get("SHEETS_CACHE_SECONDS", "60"))
CREDS_FILE     = os.environ.get("GOOGLE_CREDS_FILE", "credentials.json")

_sheets_cache = {"value": None, "fetched_at": 0, "error": None, "row_count": None}
_cache_lock   = threading.Lock()


# ── JSON helpers ───────────────────────────────────────────────────────────────
def read_data():
    if not os.path.exists(DATA_FILE):
        return DEFAULT_DATA.copy()
    with open(DATA_FILE) as f:
        return json.load(f)

def write_data(data):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ── Sheets helpers ─────────────────────────────────────────────────────────────
def _parse_cell_ref(ref):
    """'Sheet1!B2' → (sheet_name, col_idx, row_idx)  zero-based."""
    sheet, addr = ref.split("!", 1) if "!" in ref else ("Sheet1", ref)
    col_str = "".join(c for c in addr if c.isalpha()).upper()
    row_str = "".join(c for c in addr if c.isdigit())
    col_idx = sum((ord(ch) - ord("A") + 1) * (26 ** i)
                  for i, ch in enumerate(reversed(col_str))) - 1
    row_idx = int(row_str) - 1
    return sheet, col_idx, row_idx


def _parse_column_range(range_ref):
    """
    Parse a column range like 'Sheet1!B2:B' or 'Sheet1!B:B' or 'B2:B'.
    Returns (sheet_name, col_idx, start_row_idx).
    start_row_idx is 0 if no row number is given.
    """
    sheet, addr = range_ref.split("!", 1) if "!" in range_ref else ("Sheet1", range_ref)
    # addr looks like B2:B  or  B:B  or  B2:B100
    parts = addr.split(":")
    start = parts[0]
    col_str = "".join(c for c in start if c.isalpha()).upper()
    row_str = "".join(c for c in start if c.isdigit())
    col_idx = sum((ord(ch) - ord("A") + 1) * (26 ** i)
                  for i, ch in enumerate(reversed(col_str))) - 1
    start_row = int(row_str) - 1 if row_str else 0
    return sheet, col_idx, start_row


def _clean_number(raw):
    """Strip $, commas, spaces then parse as float. Returns None if not numeric."""
    cleaned = raw.replace("$", "").replace(",", "").replace(" ", "").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _fetch_csv(spreadsheet_id, sheet_name):
    """Download the full sheet as CSV rows."""
    url = (
        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={requests.utils.quote(sheet_name)}"
    )
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return list(csv.reader(io.StringIO(resp.text)))


def _fetch_range_service_account(spreadsheet_id, range_ref, creds_file):
    """Fetch a range of cells using a service account. Returns list of rows (each a list)."""
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError:
        raise ImportError("Run: pip install google-auth google-api-python-client")
    scopes  = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds   = service_account.Credentials.from_service_account_file(creds_file, scopes=scopes)
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    result  = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=range_ref
    ).execute()
    return result.get("values", [])


def _sum_column_public(spreadsheet_id, sheet_name, col_idx, start_row):
    """Sum numeric cells in a column (public sheet via CSV)."""
    rows = _fetch_csv(spreadsheet_id, sheet_name)
    total     = 0.0
    row_count = 0
    for row in rows[start_row:]:
        if col_idx < len(row):
            val = _clean_number(row[col_idx])
            if val is not None:
                total     += val
                row_count += 1
    return total, row_count


def _sum_column_service_account(spreadsheet_id, range_ref, creds_file):
    """Sum numeric cells in a column range (service account)."""
    rows      = _fetch_range_service_account(spreadsheet_id, range_ref, creds_file)
    total     = 0.0
    row_count = 0
    for row in rows:
        if row:
            val = _clean_number(row[0])
            if val is not None:
                total     += val
                row_count += 1
    return total, row_count


def fetch_raised_from_sheets():
    """
    Returns (float|None, error_str|None).
    Chooses cell vs column mode based on RAISED_SOURCE.
    Caches result for CACHE_SECONDS.
    """
    if not SPREADSHEET_ID:
        return None, "SPREADSHEET_ID not set — add it to your .env file"

    with _cache_lock:
        age = time.time() - _sheets_cache["fetched_at"]
        if age < CACHE_SECONDS and _sheets_cache["fetched_at"] > 0:
            return _sheets_cache["value"], _sheets_cache["error"]

    try:
        row_count = None

        if RAISED_SOURCE == "column":
            # ── Column-sum mode ──────────────────────────────────────────────
            if SHEETS_MODE == "service_account":
                value, row_count = _sum_column_service_account(
                    SPREADSHEET_ID, RAISED_COLUMN, CREDS_FILE
                )
            else:
                sheet_name, col_idx, start_row = _parse_column_range(RAISED_COLUMN)
                value, row_count = _sum_column_public(
                    SPREADSHEET_ID, sheet_name, col_idx, start_row
                )
        else:
            # ── Single cell mode ─────────────────────────────────────────────
            if SHEETS_MODE == "service_account":
                rows = _fetch_range_service_account(
                    SPREADSHEET_ID, RAISED_CELL, CREDS_FILE
                )
                raw = rows[0][0] if rows and rows[0] else ""
            else:
                sheet_name, col_idx, row_idx = _parse_cell_ref(RAISED_CELL)
                rows = _fetch_csv(SPREADSHEET_ID, sheet_name)
                if row_idx >= len(rows):
                    raise ValueError(f"Row {row_idx+1} out of range")
                row = rows[row_idx]
                raw = row[col_idx] if col_idx < len(row) else ""

            val = _clean_number(raw)
            if val is None:
                raise ValueError(f"Cell value {raw!r} is not a number")
            value = val

        with _cache_lock:
            _sheets_cache.update({
                "value": value, "fetched_at": time.time(),
                "error": None, "row_count": row_count
            })
        return value, None

    except Exception as exc:
        err = str(exc)
        with _cache_lock:
            _sheets_cache.update({
                "value": None, "fetched_at": time.time(),
                "error": err, "row_count": None
            })
        return None, err


def get_live_data():
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
    _, err = fetch_raised_from_sheets()
    return jsonify({
        "mode":           SHEETS_MODE,
        "spreadsheet_id": SPREADSHEET_ID,
        "raised_source":  RAISED_SOURCE,
        "raised_cell":    RAISED_CELL,
        "raised_column":  RAISED_COLUMN,
        "cache_seconds":  CACHE_SECONDS,
        "configured":     bool(SPREADSHEET_ID),
        "last_error":     err,
        "cached_value":   _sheets_cache.get("value"),
        "row_count":      _sheets_cache.get("row_count"),
        "cache_age_s":    round(time.time() - _sheets_cache["fetched_at"], 1)
                          if _sheets_cache["fetched_at"] else None,
    })

@app.route("/api/sheets/refresh", methods=["POST"])
def sheets_refresh():
    with _cache_lock:
        _sheets_cache["fetched_at"] = 0
    value, err = fetch_raised_from_sheets()
    return jsonify({
        "ok":        err is None,
        "error":     err,
        "value":     value,
        "row_count": _sheets_cache.get("row_count"),
    })

if __name__ == "__main__":
    app.run(debug=True, port=5000)
