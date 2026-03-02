# Red Hot Beverly — Fundraising Thermometer

A Flask web app with a fire-themed fundraising thermometer and a simple REST API backed by a local JSON file.

## Setup

```bash
pip install -r requirements.txt
python app.py
```

Then open http://localhost:5000 in your browser.

## File Structure

```
beverly-fundraiser/
├── app.py               # Flask app + API routes
├── requirements.txt
├── data/
│   └── fundraiser.json  # Persisted goal & raised values (auto-created)
└── templates/
    └── index.html       # Fire-themed frontend
```

## API

### GET /api/fundraiser
Returns the current goal and amount raised.

```json
{ "goal": 10000, "raised": 3750 }
```

### POST /api/fundraiser
Update goal and/or raised. Send any combination of fields.

```bash
curl -X POST http://localhost:5000/api/fundraiser \
  -H "Content-Type: application/json" \
  -d '{"goal": 50000, "raised": 12500}'
```

Returns the updated state:
```json
{ "goal": 50000, "raised": 12500 }
```

### POST /api/fundraiser/reset
Resets goal to 10000 and raised to 0.

```bash
curl -X POST http://localhost:5000/api/fundraiser/reset
```
