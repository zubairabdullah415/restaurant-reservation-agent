"""
main.py — FastAPI Application & API Routes
==========================================
Exposes the reservation agent and management endpoints.
The frontend website communicates exclusively through these routes.

Endpoints:
  POST /api/chat              — Main AI conversation endpoint
  POST /api/reservations      — Direct booking (bypass AI)
  GET  /api/reservations/{id} — Fetch a reservation by code
  PUT  /api/reservations/{id} — Modify a reservation
  DELETE /api/reservations/{id} — Cancel a reservation
  GET  /api/availability      — Check available slots
  GET  /health                — Health check
"""

import logging
import secrets
from datetime import date, time
from typing import Optional, List
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Depends, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field, validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from config import settings
from database import (
    get_pool,
    close_pool,
    get_or_create_session,
    update_session,
    get_available_time_slots,
    get_reservation_by_code,
    cancel_reservation as db_cancel,
)
from agent import run_agent

# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate Limiter (prevent abuse)
# ---------------------------------------------------------------------------

limiter = Limiter(key_func=get_remote_address)

# ---------------------------------------------------------------------------
# App Lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize resources on startup, clean up on shutdown."""
    logger.info("Starting up — initializing DB pool...")
    await get_pool()
    yield
    logger.info("Shutting down — closing DB pool...")
    await close_pool()


# ---------------------------------------------------------------------------
# FastAPI Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="The Grand Olive — AI Reservation API",
    description="AI-powered restaurant reservation system with real-time availability.",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.DEBUG else None,  # Disable Swagger in production
    redoc_url=None,
)

# Rate limiting error handler
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS — allow only your frontend domain(s)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "X-Session-Token", "Authorization"],
)

# Trusted host middleware (production security)
if not settings.DEBUG:
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=settings.ALLOWED_HOSTS,
    )


# ---------------------------------------------------------------------------
# Pydantic Request / Response Models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000, description="User message to the AI agent")
    session_token: Optional[str] = Field(None, description="Existing session token. Omit to start new session.")

    @validator("message")
    def sanitize_message(cls, v):
        # Basic sanitization — strip excessive whitespace
        return " ".join(v.split())


class ChatResponse(BaseModel):
    reply: str
    session_token: str
    booking_complete: bool = False
    confirmation_code: Optional[str] = None


class AvailabilityRequest(BaseModel):
    date: date
    party_size: int = Field(..., ge=1, le=20)
    preference: Optional[str] = "no_preference"


class ModifyRequest(BaseModel):
    email: EmailStr
    new_date: Optional[date] = None
    new_time: Optional[str] = None  # HH:MM
    new_party_size: Optional[int] = None


class CancelRequest(BaseModel):
    email: EmailStr


class ReservationResponse(BaseModel):
    confirmation_code: str
    customer_name: str
    reservation_date: str
    start_time: str
    party_size: int
    table_number: str
    status: str
    special_requests: Optional[str]


# ---------------------------------------------------------------------------
# Session Token Helper
# ---------------------------------------------------------------------------

async def resolve_session(request: Request) -> dict:
    """
    Dependency: extracts or creates a session from the X-Session-Token header.
    Generates a new secure token if none is provided.
    """
    token = request.headers.get("X-Session-Token")
    if not token or len(token) < 16:
        token = secrets.token_urlsafe(32)

    session = await get_or_create_session(token)
    session["_token"] = token  # Attach token for downstream use
    return session


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health", tags=["System"])
async def health_check():
    """Liveness probe — returns 200 if the service is running."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"status": "healthy", "service": "Grand Olive Reservation API"}


@app.post(
    "/api/chat",
    response_model=ChatResponse,
    tags=["AI Agent"],
    summary="Send a message to the AI reservation agent",
)
@limiter.limit("30/minute")
async def chat(
    request: Request,
    body: ChatRequest,
    session: dict = Depends(resolve_session),
):
    """
    Main AI conversation endpoint. The frontend sends each user message here
    and receives the agent's reply. Session state is persisted server-side.

    Flow:
      1. Load conversation history from session
      2. Run AI agent with tools
      3. Save updated history back to DB
      4. Return agent's reply + session token
    """
    token = session["_token"]

    # Deserialize stored conversation history
    messages: list = session.get("messages", [])
    context: dict  = session.get("context", {})

    try:
        reply, updated_messages, updated_context = await run_agent(
            user_message=body.message,
            conversation_history=messages,
            context=context,
        )
    except Exception as e:
        logger.exception(f"Agent error for session {token}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The reservation assistant encountered an error. Please try again.",
        )

    # Persist updated session
    await update_session(
        session_token=token,
        messages=updated_messages,
        context=updated_context,
        customer_id=updated_context.get("customer_id"),
        reservation_id=updated_context.get("reservation_id"),
    )

    return ChatResponse(
        reply=reply,
        session_token=token,
        booking_complete=updated_context.get("booking_complete", False),
        confirmation_code=updated_context.get("confirmation_code"),
    )


@app.get(
    "/api/availability",
    tags=["Reservations"],
    summary="Get available time slots for a date and party size",
)
@limiter.limit("60/minute")
async def get_availability(
    request: Request,
    date: date,
    party_size: int,
    preference: Optional[str] = "no_preference",
):
    """
    Returns available time slots — useful for calendar pickers on the frontend.
    This does NOT book anything; it's a read-only availability check.
    """
    if party_size < 1 or party_size > 20:
        raise HTTPException(400, "Party size must be between 1 and 20.")

    slots = await get_available_time_slots(
        reservation_date=date,
        party_size=party_size,
        preference=preference,
    )
    return {
        "date": str(date),
        "party_size": party_size,
        "preference": preference,
        "available_slots": slots,
        "total": len(slots),
    }


@app.get(
    "/api/reservations/{confirmation_code}",
    response_model=ReservationResponse,
    tags=["Reservations"],
    summary="Retrieve a reservation by confirmation code",
)
@limiter.limit("20/minute")
async def get_reservation(request: Request, confirmation_code: str):
    """Fetch reservation details. Used for 'manage my booking' pages."""
    reservation = await get_reservation_by_code(confirmation_code.upper())
    if not reservation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Reservation '{confirmation_code}' not found.",
        )
    return ReservationResponse(
        confirmation_code=reservation["confirmation_code"],
        customer_name=reservation["full_name"],
        reservation_date=str(reservation["reservation_date"]),
        start_time=str(reservation["start_time"])[:5],
        party_size=reservation["party_size"],
        table_number=reservation["table_number"],
        status=reservation["status"],
        special_requests=reservation.get("special_requests"),
    )


@app.delete(
    "/api/reservations/{confirmation_code}",
    tags=["Reservations"],
    summary="Cancel a reservation",
)
@limiter.limit("10/minute")
async def cancel_reservation_endpoint(
    request: Request,
    confirmation_code: str,
    body: CancelRequest,
):
    """
    Cancels a reservation. Requires email verification to prevent unauthorized cancellations.
    """
    try:
        result = await db_cancel(
            code=confirmation_code.upper(),
            customer_email=body.email,
        )
        return {
            "success": True,
            "message": f"Reservation {result['confirmation_code']} has been cancelled.",
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put(
    "/api/reservations/{confirmation_code}",
    tags=["Reservations"],
    summary="Modify an existing reservation",
)
@limiter.limit("10/minute")
async def modify_reservation_endpoint(
    request: Request,
    confirmation_code: str,
    body: ModifyRequest,
):
    """
    Modifies a reservation's date, time, or party size.
    Returns the new confirmation code (old one is cancelled and a new one issued).
    """
    from database import modify_reservation
    from datetime import time as time_type

    try:
        new_time = time_type.fromisoformat(body.new_time) if body.new_time else None
        result = await modify_reservation(
            code=confirmation_code.upper(),
            customer_email=body.email,
            new_date=body.new_date,
            new_time=new_time,
            new_party_size=body.new_party_size,
        )
        return {
            "success": True,
            "old_confirmation_code": confirmation_code.upper(),
            "new_confirmation_code": result["confirmation_code"],
            "message": (
                f"Your reservation has been updated. "
                f"New confirmation code: {result['confirmation_code']}"
            ),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# Global Exception Handler
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception(f"Unhandled exception on {request.url}: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": "An internal error occurred. Our team has been notified."},
    )


# ---------------------------------------------------------------------------
# Entry Point (local dev)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.DEBUG,
        log_level="info",
    )
