"""
database.py — Async Database Layer
===================================
Handles all PostgreSQL interactions using asyncpg for high-performance
async I/O. Implements SELECT FOR UPDATE to prevent race conditions on
concurrent booking attempts for the same time slot.
"""

import asyncpg
import asyncio
import logging
import random
import string
from datetime import date, time, datetime, timedelta
from typing import Optional, List, Dict, Any
from uuid import UUID

from config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connection Pool
# ---------------------------------------------------------------------------

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    """Returns the singleton connection pool, creating it if needed."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=settings.DATABASE_URL,
            min_size=5,
            max_size=20,
            command_timeout=30,
            # Enable JSON/JSONB decoding automatically
            init=_init_connection,
        )
        logger.info("Database connection pool created.")
    return _pool


async def _init_connection(conn: asyncpg.Connection):
    """Called for every new connection in the pool."""
    await conn.set_type_codec(
        "jsonb",
        encoder=lambda v: __import__("json").dumps(v),
        decoder=lambda v: __import__("json").loads(v),
        schema="pg_catalog",
    )
    await conn.set_type_codec(
        "json",
        encoder=lambda v: __import__("json").dumps(v),
        decoder=lambda v: __import__("json").loads(v),
        schema="pg_catalog",
    )


async def close_pool():
    """Gracefully close the pool on application shutdown."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def _generate_confirmation_code() -> str:
    """
    Generates a human-friendly confirmation code like 'RES-A3X9K2'.
    Short enough to read over the phone, unique enough to avoid collisions.
    """
    chars = random.choices(string.ascii_uppercase + string.digits, k=6)
    return "RES-" + "".join(chars)


# ---------------------------------------------------------------------------
# Availability Checking (Core Anti-Double-Booking Logic)
# ---------------------------------------------------------------------------

async def check_availability(
    reservation_date: date,
    start_time: time,
    party_size: int,
    preference: str = "no_preference",
    duration_minutes: int = 90,
) -> List[Dict[str, Any]]:
    """
    Returns a list of available tables matching the criteria.

    This query finds tables that do NOT have an overlapping reservation in
    the confirmed/pending states. It does NOT lock rows — locking only
    happens at the moment of booking (see book_table).
    """
    end_time = (
        datetime.combine(date.today(), start_time) + timedelta(minutes=duration_minutes)
    ).time()

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Build preference filter
        preference_filter = (
            "AND t.location = $6::seating_preference"
            if preference != "no_preference"
            else ""
        )

        query = f"""
            SELECT
                t.id,
                t.table_number,
                t.capacity,
                t.location,
                t.description,
                t.is_accessible,
                t.has_high_chair
            FROM tables t
            WHERE
                t.status = 'available'
                AND t.capacity >= $3
                -- Exclude tables with overlapping confirmed/pending reservations
                AND t.id NOT IN (
                    SELECT DISTINCT r.table_id
                    FROM reservations r
                    WHERE
                        r.reservation_date = $1
                        AND r.status NOT IN ('cancelled', 'no_show')
                        AND (
                            -- Check time overlap: existing [rs, re) overlaps new [s, e)?
                            (r.start_time < $5 AND r.end_time > $4)
                        )
                )
                {preference_filter}
            ORDER BY
                ABS(t.capacity - $3),  -- Closest capacity match first
                t.location = 'no_preference' DESC
            LIMIT 10
        """

        params = [reservation_date, start_time, party_size, start_time, end_time]
        if preference != "no_preference":
            params.append(preference)

        rows = await conn.fetch(query, *params)
        return [dict(r) for r in rows]


async def get_available_time_slots(
    reservation_date: date,
    party_size: int,
    preference: str = "no_preference",
) -> List[Dict[str, Any]]:
    """
    Returns all available time slots for a given date, party size, and preference.
    Checks standard service windows: Lunch (11:30–14:30), Dinner (17:30–22:00).
    """
    # Define service windows
    lunch_slots  = ["11:30", "12:00", "12:30", "13:00", "13:30"]
    dinner_slots = ["17:30", "18:00", "18:30", "19:00", "19:30", "20:00", "20:30", "21:00"]

    available_slots = []
    for slot_str in lunch_slots + dinner_slots:
        slot_time = time.fromisoformat(slot_str)
        tables = await check_availability(
            reservation_date, slot_time, party_size, preference
        )
        if tables:
            available_slots.append({
                "time": slot_str,
                "period": "lunch" if slot_str in lunch_slots else "dinner",
                "tables_available": len(tables),
            })

    return available_slots


# ---------------------------------------------------------------------------
# Customer Management
# ---------------------------------------------------------------------------

async def find_or_create_customer(
    full_name: str,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    dietary_notes: Optional[str] = None,
    allergy_notes: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Looks up a customer by email or phone. If not found, creates a new record.
    Returns the customer record as a dict.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Try to find existing customer
        if email:
            row = await conn.fetchrow(
                "SELECT * FROM customers WHERE email = $1", email
            )
            if row:
                # Update visit info if found
                await conn.execute(
                    "UPDATE customers SET full_name=$1, phone=COALESCE($2,phone), "
                    "dietary_notes=COALESCE($3,dietary_notes), "
                    "allergy_notes=COALESCE($4,allergy_notes) WHERE id=$5",
                    full_name, phone, dietary_notes, allergy_notes, row["id"],
                )
                return dict(row)

        if phone:
            row = await conn.fetchrow(
                "SELECT * FROM customers WHERE phone = $1", phone
            )
            if row:
                return dict(row)

        # Create new customer
        row = await conn.fetchrow(
            """
            INSERT INTO customers (full_name, email, phone, dietary_notes, allergy_notes)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING *
            """,
            full_name, email, phone, dietary_notes, allergy_notes,
        )
        logger.info(f"New customer created: {row['id']}")
        return dict(row)


# ---------------------------------------------------------------------------
# Reservation Booking (with Optimistic Locking)
# ---------------------------------------------------------------------------

async def book_table(
    customer_id: UUID,
    table_id: UUID,
    reservation_date: date,
    start_time: time,
    party_size: int,
    special_requests: Optional[str] = None,
    duration_minutes: int = 90,
) -> Dict[str, Any]:
    """
    Books a table using a database transaction with SELECT FOR UPDATE.

    RACE CONDITION PROTECTION:
    ─────────────────────────
    Two users may simultaneously pass the availability check. This function
    uses SELECT FOR UPDATE NOWAIT inside a transaction to atomically claim
    the table. The second user will receive a 'table already taken' error
    immediately rather than waiting.

    Returns the completed reservation record, or raises an exception.
    """
    end_time = (
        datetime.combine(date.today(), start_time) + timedelta(minutes=duration_minutes)
    ).time()

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():  # All-or-nothing transaction

            # ── Step 1: Lock the target table row exclusively ──────────────
            # NOWAIT raises immediately if another transaction holds the lock,
            # instead of waiting (which could cause a deadlock queue).
            table_row = await conn.fetchrow(
                "SELECT * FROM tables WHERE id = $1 FOR UPDATE NOWAIT",
                table_id,
            )
            if not table_row or table_row["status"] != "available":
                raise ValueError(f"Table {table_id} is not available.")

            # ── Step 2: Re-verify no overlap within the transaction ─────────
            # This is the critical check — done AFTER acquiring the lock.
            conflict = await conn.fetchval(
                """
                SELECT COUNT(*) FROM reservations
                WHERE
                    table_id = $1
                    AND reservation_date = $2
                    AND status NOT IN ('cancelled', 'no_show')
                    AND (start_time < $4 AND end_time > $3)
                """,
                table_id, reservation_date, start_time, end_time,
            )
            if conflict > 0:
                raise ValueError(
                    f"Table {table_row['table_number']} was just booked for that time. "
                    "Please choose another slot."
                )

            # ── Step 3: Verify party size fits the table ───────────────────
            if party_size > table_row["capacity"]:
                raise ValueError(
                    f"Party size {party_size} exceeds table capacity of {table_row['capacity']}."
                )

            # ── Step 4: Generate unique confirmation code ──────────────────
            # Retry on (extremely unlikely) collision
            for attempt in range(5):
                code = _generate_confirmation_code()
                exists = await conn.fetchval(
                    "SELECT 1 FROM reservations WHERE confirmation_code = $1", code
                )
                if not exists:
                    break
            else:
                raise RuntimeError("Failed to generate unique confirmation code.")

            # ── Step 5: Insert the reservation ────────────────────────────
            reservation = await conn.fetchrow(
                """
                INSERT INTO reservations (
                    customer_id, table_id, reservation_date, start_time, end_time,
                    party_size, special_requests, confirmation_code, status
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'confirmed')
                RETURNING *
                """,
                customer_id, table_id, reservation_date, start_time, end_time,
                party_size, special_requests, code,
            )

            # ── Step 6: Increment customer visit count ─────────────────────
            await conn.execute(
                "UPDATE customers SET visit_count = visit_count + 1 WHERE id = $1",
                customer_id,
            )

            logger.info(
                f"Reservation {reservation['confirmation_code']} created for "
                f"customer {customer_id} at table {table_row['table_number']}."
            )
            return dict(reservation)


# ---------------------------------------------------------------------------
# Reservation Retrieval & Modification
# ---------------------------------------------------------------------------

async def get_reservation_by_code(code: str) -> Optional[Dict[str, Any]]:
    """Fetches a full reservation record by its human-readable confirmation code."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                r.*,
                c.full_name, c.email, c.phone,
                t.table_number, t.location, t.capacity
            FROM reservations r
            JOIN customers c ON c.id = r.customer_id
            JOIN tables    t ON t.id = r.table_id
            WHERE r.confirmation_code = $1
            """,
            code.upper(),
        )
        return dict(row) if row else None


async def cancel_reservation(code: str, customer_email: str) -> Dict[str, Any]:
    """
    Cancels a reservation after verifying customer ownership.
    Returns the updated reservation record.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE reservations r
            SET status = 'cancelled'
            FROM customers c
            WHERE
                r.customer_id = c.id
                AND r.confirmation_code = $1
                AND c.email = $2
                AND r.status = 'confirmed'
            RETURNING r.*
            """,
            code.upper(), customer_email,
        )
        if not row:
            raise ValueError(
                "Could not cancel reservation. Please verify the confirmation code "
                "and the email address used at booking."
            )
        logger.info(f"Reservation {code} cancelled.")
        return dict(row)


async def modify_reservation(
    code: str,
    customer_email: str,
    new_date: Optional[date] = None,
    new_time: Optional[time] = None,
    new_party_size: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Modifies an existing reservation. Re-runs availability check before committing.
    This is implemented as cancel + rebook to leverage existing locking logic.
    """
    existing = await get_reservation_by_code(code)
    if not existing:
        raise ValueError(f"Reservation '{code}' not found.")
    if existing["email"] != customer_email:
        raise ValueError("Email does not match the reservation holder.")
    if existing["status"] != "confirmed":
        raise ValueError(f"Only confirmed reservations can be modified (current: {existing['status']}).")

    # Use new values or fall back to existing
    target_date       = new_date       or existing["reservation_date"]
    target_time       = new_time       or existing["start_time"]
    target_party_size = new_party_size or existing["party_size"]

    # Find available tables for new slot
    tables = await check_availability(target_date, target_time, target_party_size)
    if not tables:
        raise ValueError(
            f"No tables available on {target_date} at {target_time} for {target_party_size} guests."
        )

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Cancel old reservation
            await conn.execute(
                "UPDATE reservations SET status='cancelled' WHERE confirmation_code=$1",
                code.upper(),
            )
            # Rebook with first available table
            new_reservation = await book_table(
                customer_id=existing["customer_id"],
                table_id=tables[0]["id"],
                reservation_date=target_date,
                start_time=target_time,
                party_size=target_party_size,
                special_requests=existing.get("special_requests"),
            )
            logger.info(f"Reservation {code} modified → new code {new_reservation['confirmation_code']}.")
            return new_reservation


# ---------------------------------------------------------------------------
# Session Management
# ---------------------------------------------------------------------------

async def get_or_create_session(session_token: str) -> Dict[str, Any]:
    """Fetches an active session or creates a new one."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM conversation_sessions
            WHERE session_token = $1 AND is_active = TRUE AND expires_at > NOW()
            """,
            session_token,
        )
        if row:
            # Refresh expiry on activity
            await conn.execute(
                "UPDATE conversation_sessions SET last_active_at=NOW(), "
                "expires_at=NOW()+INTERVAL '2 hours' WHERE id=$1",
                row["id"],
            )
            return dict(row)

        # Create new session
        row = await conn.fetchrow(
            """
            INSERT INTO conversation_sessions (session_token, messages, context)
            VALUES ($1, '[]'::jsonb, '{}'::jsonb)
            RETURNING *
            """,
            session_token,
        )
        return dict(row)


async def update_session(
    session_token: str,
    messages: List[Dict],
    context: Dict,
    customer_id: Optional[UUID] = None,
    reservation_id: Optional[UUID] = None,
) -> None:
    """Persists updated conversation history and context to the database."""
    import json
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE conversation_sessions
            SET
                messages      = $2::jsonb,
                context       = $3::jsonb,
                customer_id   = COALESCE($4, customer_id),
                reservation_id = COALESCE($5, reservation_id),
                last_active_at = NOW()
            WHERE session_token = $1
            """,
            session_token,
            json.dumps(messages),
            json.dumps(context),
            customer_id,
            reservation_id,
        )
