# 🫒 The Grand Olive — AI Reservation Agent
### Complete Backend System | Python / FastAPI / Claude AI

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                        FRONTEND                             │
│   Website / React App / Mobile                              │
│   reservation-widget.js  ←→  REST API (JSON)               │
└─────────────────────┬───────────────────────────────────────┘
                      │ HTTPS
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                    FASTAPI BACKEND                          │
│                                                             │
│  POST /api/chat ──────────→ agent.py (AI Agent Loop)       │
│  GET  /api/availability ──→ database.py (read-only)        │
│  GET  /api/reservations   → database.py                    │
│  PUT  /api/reservations   → database.py + notify           │
│  DEL  /api/reservations   → database.py + notify           │
└──────────┬──────────────────────────┬───────────────────────┘
           │                          │
           ▼                          ▼
┌──────────────────┐       ┌──────────────────────────────────┐
│  Anthropic API   │       │         PostgreSQL                │
│  (Claude claude-opus-4-5)   │       │                                  │
│  Tool Use / FC   │       │  customers | tables              │
└──────────────────┘       │  reservations | sessions         │
                           │  notification_log                │
                           └──────────────────────────────────┘
                                       │
                           ┌───────────┴──────────┐
                           ▼                      ▼
                    ┌────────────┐        ┌────────────┐
                    │  SendGrid  │        │   Twilio   │
                    │  (Email)   │        │   (SMS)    │
                    └────────────┘        └────────────┘
```

---

## File Structure

```
reservation_agent/
├── main.py              ← FastAPI routes & application entry point
├── agent.py             ← Claude AI agent + tool definitions + agent loop
├── database.py          ← Async PostgreSQL layer (asyncpg) + race condition handling
├── notifications.py     ← SendGrid email + Twilio SMS services
├── config.py            ← Pydantic settings (loaded from .env)
├── schema.sql           ← Full PostgreSQL schema with constraints
├── requirements.txt     ← Python dependencies
├── .env.example         ← Environment variable template
└── frontend/
    └── reservation-widget.js  ← Drop-in frontend chat widget
```

---

## Quick Start

### 1. Prerequisites

- Python 3.11+
- PostgreSQL 15+
- Anthropic API key
- SendGrid account (free tier works)
- Twilio account (free trial works)

### 2. Clone & Install

```bash
git clone https://github.com/your-org/grand-olive-reservation
cd grand-olive-reservation

python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure Environment

```bash
cp .env.example .env
# Edit .env with your actual credentials
nano .env
```

### 4. Initialize Database

```bash
# Create database
psql -U postgres -c "CREATE DATABASE grand_olive;"

# Run schema (creates all tables, types, indexes, seed data)
psql -U postgres -d grand_olive -f schema.sql
```

> **Note:** The schema includes `btree_gist` and `pg_trgm` extensions.
> These are included in standard PostgreSQL and require no additional installation.

### 5. Run the Server

```bash
# Development
python main.py

# Production (with Gunicorn + Uvicorn workers)
gunicorn main:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000
```

API documentation: http://localhost:8000/docs (DEBUG mode only)

---

## Frontend Integration

### Option A — Drop-in Script Tag (Fastest)

Add one line to any HTML page:

```html
<!-- Place before </body> -->
<script
  src="https://api.thegrandolive.com/static/reservation-widget.js"
  data-api-url="https://api.thegrandolive.com"
  data-restaurant-name="The Grand Olive">
</script>
```

This renders a floating chat button (bottom-right). Clicking it opens the AI agent.
No other code needed. Session is persisted in `sessionStorage` automatically.

### Option B — React / Next.js Integration

```jsx
// hooks/useReservationAgent.js
import { useState, useCallback } from "react";

export function useReservationAgent() {
  const [messages, setMessages]           = useState([]);
  const [sessionToken, setSessionToken]   = useState(null);
  const [isLoading, setIsLoading]         = useState(false);
  const [bookingComplete, setBookingComplete] = useState(false);

  const sendMessage = useCallback(async (text) => {
    setIsLoading(true);
    setMessages(prev => [...prev, { role: "user", content: text }]);

    try {
      const res = await fetch(`${process.env.NEXT_PUBLIC_API_URL}/api/chat`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(sessionToken ? { "X-Session-Token": sessionToken } : {}),
        },
        body: JSON.stringify({ message: text }),
      });
      const data = await res.json();

      if (data.session_token) setSessionToken(data.session_token);
      if (data.booking_complete) setBookingComplete(true);

      setMessages(prev => [...prev, { role: "agent", content: data.reply }]);
      return data;
    } finally {
      setIsLoading(false);
    }
  }, [sessionToken]);

  return { messages, sendMessage, isLoading, bookingComplete };
}
```

### Option C — Availability Calendar (Direct API)

```javascript
// Fetch available slots for a date picker
async function fetchAvailability(date, partySize, preference = "no_preference") {
  const params = new URLSearchParams({ date, party_size: partySize, preference });
  const res = await fetch(`/api/availability?${params}`);
  const { available_slots } = await res.json();

  // available_slots = [{ time: "19:00", period: "dinner", tables_available: 3 }, ...]
  return available_slots;
}
```

---

## API Reference

### `POST /api/chat`

Main conversation endpoint.

**Request:**
```json
{
  "message": "I'd like a table for 4 this Saturday evening",
  "session_token": "abc123..."  // Optional; omit to start new session
}
```

**Response:**
```json
{
  "reply": "Wonderful! I have availability on Saturday the 21st for 4 guests...",
  "session_token": "abc123...",
  "booking_complete": false,
  "confirmation_code": null
}
```

**Headers to send:**
| Header | Value |
|--------|-------|
| `Content-Type` | `application/json` |
| `X-Session-Token` | Your session token (after first message) |

---

### `GET /api/availability`

Returns available time slots for calendar display.

```
GET /api/availability?date=2025-12-24&party_size=4&preference=outdoor
```

**Response:**
```json
{
  "date": "2025-12-24",
  "party_size": 4,
  "available_slots": [
    { "time": "18:00", "period": "dinner", "tables_available": 2 },
    { "time": "19:30", "period": "dinner", "tables_available": 3 }
  ]
}
```

---

### `GET /api/reservations/{code}`

Fetch a reservation by confirmation code (e.g., `RES-A3X9K2`).

```
GET /api/reservations/RES-A3X9K2
```

---

### `DELETE /api/reservations/{code}`

Cancel a reservation (requires email verification).

```json
{ "email": "guest@example.com" }
```

---

### `PUT /api/reservations/{code}`

Modify a reservation.

```json
{
  "email": "guest@example.com",
  "new_date": "2025-12-28",
  "new_time": "20:00"
}
```

---

## Race Condition Protection

Two users booking the same slot simultaneously is handled at **three levels**:

| Layer | Mechanism |
|-------|-----------|
| **1. Database Constraint** | `EXCLUDE USING gist` prevents overlapping time ranges at the schema level |
| **2. Row-Level Lock** | `SELECT FOR UPDATE NOWAIT` in `book_table()` atomically claims the table |
| **3. Re-verification** | After acquiring the lock, availability is re-checked inside the transaction |

If two requests arrive simultaneously:
- **First request** → acquires lock → books successfully
- **Second request** → lock fails immediately → `ValueError` → agent suggests alternatives

---

## Deployment (Production)

### PostgreSQL (Supabase / Railway / RDS)

```bash
# Supabase: paste schema.sql in the SQL editor
# Railway:  use DATABASE_URL from the provisioned service
# AWS RDS:  psql -h your-rds-endpoint -U postgres -d grand_olive -f schema.sql
```

### Fly.io (Recommended for FastAPI)

```toml
# fly.toml
app = "grand-olive-api"
[build]
  [build.args]
    PYTHON_VERSION = "3.11"

[http_service]
  internal_port = 8000
  auto_stop_machines = true
  auto_start_machines = true
```

```bash
fly launch
fly secrets set ANTHROPIC_API_KEY=sk-ant-... DATABASE_URL=postgresql://...
fly deploy
```

### Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["gunicorn", "main:app", "-w", "4", "-k", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8000"]
```

---

## Security Checklist

- [ ] Set `DEBUG=false` in production
- [ ] Use strong `SECRET_KEY` (32+ random bytes)
- [ ] Configure `ALLOWED_ORIGINS` to your frontend domain only
- [ ] Enable HTTPS (TLS) via your load balancer or Fly.io
- [ ] Rotate API keys regularly
- [ ] Enable PostgreSQL SSL (`?sslmode=require` in DATABASE_URL)
- [ ] Set up connection pooling (PgBouncer) for high traffic

---

## Extending the System

**Add a new AI tool:** Define it in `TOOLS` in `agent.py` and add a handler in `execute_tool()`.

**Add staff management UI:** The database schema supports internal notes and status flags. Build a simple CRUD admin panel on top of the existing FastAPI app.

**Add reminder scheduling:** Use APScheduler or Celery Beat to query upcoming reservations (`reservation_date = NOW() + INTERVAL '24 hours'`) and call `send_confirmation_email()` with a reminder template.

**Multi-language support:** Pass a `language` field in the chat request and append a language instruction to the system prompt (`"Respond in French."`).

---

*Built with ❤️ using Claude AI, FastAPI, asyncpg, and PostgreSQL.*
