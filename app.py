import json
import os
from flask import Flask, jsonify, request, render_template

app = Flask(__name__)

DATA_FILE = os.path.join(os.path.dirname(__file__), "data", "fundraiser.json")
DEFAULT_DATA = {"goal": 10000, "raised": 0}


def read_data():
    """Read fundraiser data from JSON file, returning defaults if missing."""
    if not os.path.exists(DATA_FILE):
        return DEFAULT_DATA.copy()
    with open(DATA_FILE, "r") as f:
        return json.load(f)


def write_data(data):
    """Write fundraiser data to JSON file."""
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/admin")
def admin():
    return render_template("admin.html")


@app.route("/embed")
def embed():
    """Minimal embeddable gauge — no fire background, transparent bg, iframe-friendly."""
    return render_template("embed.html")


@app.route("/api/fundraiser", methods=["GET"])
def get_fundraiser():
    """Return current goal and raised amount."""
    return jsonify(read_data())


@app.route("/api/fundraiser", methods=["POST"])
def update_fundraiser():
    """Update goal and/or raised amount. Accepts JSON body."""
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
    return jsonify(data)


@app.route("/api/fundraiser/reset", methods=["POST"])
def reset_fundraiser():
    """Reset to defaults."""
    write_data(DEFAULT_DATA.copy())
    return jsonify(DEFAULT_DATA)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
