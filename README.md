# Central Bank Policy Dashboard — Python/Flask Backend

A lightweight data API that fetches live monetary policy data from free public
sources and serves it to the dashboard frontend.

---

## Architecture

```
Browser (cb-policy-dashboard.html)
    │
    │  GET /api/data/pol
    ▼
Flask API  (app.py)
    │
    ├── assembler.py          merges 4 series into monthly rows
    │       │
    │       ├── fetchers/fred.py        FRED API  (rates + most yields)
    │       ├── fetchers/ecb.py         ECB Data Portal  (ECB rate + HICP)
    │       └── fetchers/eurostat.py    Eurostat  (CEE country CPI)
    │
    └── cache.py              file-based JSON cache (12h TTL)
                              stored in  .cache/
```

### Data sources by series

| Series       | Fed | ECB | BoE | BoJ | Poland | Hungary | Romania | Czech |
|---|---|---|---|---|---|---|---|---|
| Policy rate  | FRED | ECB Portal | FRED | FRED | FRED | FRED | FRED | FRED |
| CPI          | FRED | ECB Portal | FRED | FRED | Eurostat | Eurostat | Eurostat | Eurostat |
| 2Y yield     | FRED | FRED | FRED | FRED | FRED | FRED | FRED | FRED |
| 10Y yield    | FRED | FRED | FRED | FRED | FRED | FRED | FRED | FRED |

---

## Setup

### 1. Prerequisites

- Python 3.11+
- A free FRED API key → https://fred.stlouisfed.org/docs/api/api_key.html

### 2. Install

```bash
cd cb-api
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env and paste your FRED_API_KEY
```

### 4. Run (development)

```bash
python app.py
# → http://localhost:5000
```

### 5. Run (production)

```bash
gunicorn app:app --bind 0.0.0.0:5000 --workers 2
```

---

## API reference

### `GET /api/banks`
List all available central bank keys.

```json
{
  "banks": [
    { "key": "fed", "name": "Federal Reserve (Fed)", "currency": "USD", "cpi_target": 2.0 },
    { "key": "pol", "name": "National Bank of Poland (NBP)", "currency": "PLN", "cpi_target": 2.5 },
    ...
  ]
}
```

---

### `GET /api/data/<cb_key>`
Returns assembled monthly chart data for one central bank.

**Path params**
- `cb_key` — one of: `fed`, `ecb`, `boe`, `boj`, `pol`, `hun`, `rom`, `cze`

**Query params**
| Param | Type | Default | Description |
|---|---|---|---|
| `start` | YYYY-MM-DD | 2021-01-01 | Earliest data point |
| `refresh` | 0\|1 | 0 | Force cache refresh |

**Example**
```
GET /api/data/pol?start=2022-01-01
```

**Response**
```json
{
  "cb": "pol",
  "name": "National Bank of Poland (NBP)",
  "currency": "PLN",
  "cpi_target": 2.5,
  "start_date": "2022-01-01",
  "count": 36,
  "data": [
    { "date": "2022-01", "rate": 2.25, "cpi": 9.4, "y2": 4.10, "y10": 4.05 },
    { "date": "2022-02", "rate": 2.75, "cpi": 8.5, "y2": 4.50, "y10": 4.40 },
    ...
  ]
}
```

- `data[].rate` — policy rate (%)
- `data[].cpi`  — CPI / HICP YoY (%)
- `data[].y2`   — 2Y government bond yield (%)
- `data[].y10`  — 10Y government bond yield (%)
- Any value may be `null` if the upstream source has not yet published data for that month.

---

### `GET /api/meta/<cb_key>`
Static metadata: series labels, source APIs, series IDs.

---

### `GET /api/status`
Health check + cache inventory.

```json
{
  "status": "ok",
  "available_banks": ["fed", "ecb", "boe", "boj", "pol", "hun", "rom", "cze"],
  "cache": {
    "entries": 4,
    "ttl_seconds": 43200,
    "files": [
      { "key": "fred_FEDFUNDS_2021-01-01", "age_seconds": 3600, "expires_in_seconds": 39600 }
    ]
  }
}
```

---

### `DELETE /api/cache`
Clears all cached files. Useful after correcting upstream data issues.

---

## Connecting the frontend

In `cb-policy-dashboard.html`, replace the hardcoded `CB_DB` object
with a live fetch. Find the `switchCB` function and change:

```js
// BEFORE (hardcoded)
function switchCB(key) {
  const cb = CB_DB[key];
  buildChart(cb.data);
  ...
}

// AFTER (live API)
const API_BASE = "http://localhost:5000";

async function switchCB(key) {
  showLoadingState();
  const res  = await fetch(`${API_BASE}/api/data/${key}`);
  const json = await res.json();
  currentData = json.data;
  buildChart(currentData);
  buildKPI(json);
  ...
}
```

See `cb-policy-dashboard-live.html` for the fully wired version.

---

## Adding a new central bank

1. Add an entry to `CB_CONFIG` in `config.py` with the four series codes.
2. No other code changes needed — the assembler and app pick it up automatically.

## Caveats

- FRED's OECD-sourced CEE bond yield series (`IRLTST01XXM156N`, `IRLTLT01XXM156N`)
  may have a 1–2 month lag vs. market data. For real-time bond yields, a paid
  data provider (Refinitiv, Bloomberg, Nasdaq Data Link) is needed.
- Romania 2Y yields from FRED may have gaps; the assembler forward-fills these.
- The ECB deposit facility rate series starts in 1999; only post-2021 data is
  used by default.
