"""
agent.py — AI Reservation Agent Core
======================================
Implements the conversational AI agent using Anthropic's Claude API with
tool use (function calling). The agent orchestrates the full booking flow:
availability checking → customer info collection → booking → confirmation.

Architecture:
  • System prompt defines the agent's persona and restaurant rules
  • Tools are Python-backed functions that query the live database
  • The agent loop handles multi-step tool use transparently
"""

import logging
import json
from datetime import date, time, datetime
from typing import List, Dict, Any, Optional
from uuid import UUID

import anthropic

from config import settings
from database import (
    check_availability,
    get_available_time_slots,
    find_or_create_customer,
    book_table,
    get_reservation_by_code,
    cancel_reservation,
    modify_reservation,
)
from notifications import send_confirmation_email, send_confirmation_sms

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Anthropic Client
# ---------------------------------------------------------------------------

client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

# ---------------------------------------------------------------------------
# System Prompt — The Agent's Persona & Rules
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are "Aria", the friendly and professional AI reservation assistant for
**The Grand Olive** — a high-end Mediterranean restaurant celebrated for its warm ambiance,
award-winning cuisine, and impeccable hospitality.

## YOUR ROLE
Guide guests through the entire reservation process: checking availability, gathering their
details, confirming the booking, and handling modifications or cancellations — all in natural,
warm, conversational language.

## RESTAURANT DETAILS
- Name:    The Grand Olive
- Hours:   Lunch: 11:30 AM – 3:00 PM | Dinner: 5:30 PM – 10:30 PM
- Cuisine: Modern Mediterranean
- Address: 42 Harrington Square, London

## SEATING OPTIONS
Describe these warmly when relevant:
- 🪟 Window Seats — Stunning street or garden views, intimate for two
- 🌿 Outdoor Terrace — Fresh air, garden setting, pet-friendly
- 🔥 Indoor / Near Fireplace — Cozy, romantic atmosphere
- 🤫 Quiet Corner — Perfect for private conversations or business lunches
- 🍸 Bar Seating — Vibrant, social atmosphere
- 🚪 Private Room — Exclusive dining, available for 8–10+ guests, ideal for events

## BOOKING RULES YOU MUST FOLLOW
1. ALWAYS use your tools to check live availability before quoting availability.
2. NEVER confirm a booking without first calling the `book_table` tool.
3. ALWAYS collect: guest name, date, time, party size. Email is required for confirmation.
4. Ask about special requests (allergies, dietary needs, celebrations) naturally.
5. Standard dining duration is 90 minutes. Private rooms allow up to 3 hours.
6. Minimum party for private room: 8 guests.
7. If a requested slot has NO availability, immediately suggest 2–3 alternative times.
8. For cancellations, verify with the confirmation code AND the email on file.

## CONVERSATION STYLE
- Warm, elegant, and efficient — like a top-tier maître d'.
- Never robotic. Use natural language, empathy, and gentle suggestions.
- If a guest seems uncertain, offer to help them explore options.
- Keep responses concise unless the guest asks for more detail.
- Always end a completed booking by reading back the full summary to the guest.

## BOOKING SUMMARY FORMAT (use when confirming)
"Perfect! Here's your reservation summary:
📍 The Grand Olive, 42 Harrington Square
📅 [Date], [Time]
👥 [Party Size] guests | [Location/Preference]
🪑 Table [Number] — [Description]
📋 Special requests: [or 'None noted']
🔑 Confirmation code: **[CODE]**
A confirmation email has been sent to [email]. We look forward to welcoming you! 🫒"

## WHAT YOU CAN DO
- Check available time slots for a date/party size
- Check available tables for a specific slot
- Create or look up customer profiles
- Book a table (this is a confirmed, locked reservation)
- Retrieve an existing reservation by code
- Modify a reservation (date, time, party size)
- Cancel a reservation
- Trigger email and SMS confirmations

If a request falls outside reservations (e.g., menu questions), politely answer briefly
and redirect: "For the most current menu, I'd suggest visiting our website or I can have
a team member reach out — but let's get your table sorted first!"
"""

# ---------------------------------------------------------------------------
# Tool Definitions — Exposed to the Claude API
# ---------------------------------------------------------------------------

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "get_available_time_slots",
        "description": (
            "Fetches all available time slots for a given date, party size, and optional "
            "seating preference. Use this FIRST when a customer asks what times are available, "
            "or when their preferred slot is unavailable and you need to suggest alternatives."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reservation_date": {
                    "type": "string",
                    "description": "Date in YYYY-MM-DD format, e.g. '2025-12-24'",
                },
                "party_size": {
                    "type": "integer",
                    "description": "Number of guests (1–20)",
                },
                "preference": {
                    "type": "string",
                    "enum": [
                        "indoor", "outdoor", "bar", "quiet_corner",
                        "window", "private_room", "no_preference"
                    ],
                    "description": "Seating preference. Default: 'no_preference'",
                },
            },
            "required": ["reservation_date", "party_size"],
        },
    },
    {
        "name": "check_table_availability",
        "description": (
            "Checks which specific tables are available for a given date, start time, "
            "party size, and optional preference. Use when the customer has chosen a specific "
            "time and you need to present or select a table."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reservation_date": {"type": "string", "description": "YYYY-MM-DD"},
                "start_time":       {"type": "string", "description": "HH:MM (24h), e.g. '19:30'"},
                "party_size":       {"type": "integer"},
                "preference": {
                    "type": "string",
                    "enum": [
                        "indoor", "outdoor", "bar", "quiet_corner",
                        "window", "private_room", "no_preference"
                    ],
                    "description": "Optional seating preference",
                },
            },
            "required": ["reservation_date", "start_time", "party_size"],
        },
    },
    {
        "name": "find_or_create_customer",
        "description": (
            "Looks up a customer by email or phone number. If not found, creates a new profile. "
            "Call this after collecting the guest's name, email, and/or phone number."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "full_name":      {"type": "string"},
                "email":          {"type": "string", "description": "Guest's email address"},
                "phone":          {"type": "string", "description": "Guest's phone number"},
                "dietary_notes":  {"type": "string", "description": "e.g. 'vegetarian, no pork'"},
                "allergy_notes":  {"type": "string", "description": "e.g. 'severe nut allergy'"},
            },
            "required": ["full_name"],
        },
    },
    {
        "name": "book_table",
        "description": (
            "CONFIRMS a reservation. Books the specified table for the customer. "
            "Only call this when you have: customer_id, table_id, date, time, and party_size. "
            "This is the step that actually creates the reservation in the database."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id":      {"type": "string", "description": "UUID of the customer"},
                "table_id":         {"type": "string", "description": "UUID of the selected table"},
                "reservation_date": {"type": "string", "description": "YYYY-MM-DD"},
                "start_time":       {"type": "string", "description": "HH:MM (24h)"},
                "party_size":       {"type": "integer"},
                "special_requests": {"type": "string", "description": "Allergies, celebrations, etc."},
            },
            "required": ["customer_id", "table_id", "reservation_date", "start_time", "party_size"],
        },
    },
    {
        "name": "get_reservation",
        "description": (
            "Retrieves an existing reservation by confirmation code. "
            "Use when a guest asks to view, modify, or cancel their booking."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "confirmation_code": {
                    "type": "string",
                    "description": "The code given at booking, e.g. 'RES-A3X9K2'",
                },
            },
            "required": ["confirmation_code"],
        },
    },
    {
        "name": "modify_reservation",
        "description": (
            "Modifies an existing confirmed reservation. Can change date, time, or party size. "
            "Requires confirmation code and the guest's email for verification."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "confirmation_code": {"type": "string"},
                "customer_email":    {"type": "string"},
                "new_date":          {"type": "string", "description": "YYYY-MM-DD (optional)"},
                "new_time":          {"type": "string", "description": "HH:MM (optional)"},
                "new_party_size":    {"type": "integer", "description": "Optional new party size"},
            },
            "required": ["confirmation_code", "customer_email"],
        },
    },
    {
        "name": "cancel_reservation",
        "description": (
            "Cancels a confirmed reservation. Requires confirmation code and the email "
            "used at booking to verify identity."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "confirmation_code": {"type": "string"},
                "customer_email":    {"type": "string"},
            },
            "required": ["confirmation_code", "customer_email"],
        },
    },
    {
        "name": "send_confirmation",
        "description": (
            "Triggers email and/or SMS confirmation messages after a successful booking "
            "or modification. Always call this after book_table or modify_reservation succeed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reservation_id":    {"type": "string", "description": "UUID of the reservation"},
                "confirmation_code": {"type": "string"},
                "customer_name":     {"type": "string"},
                "customer_email":    {"type": "string"},
                "customer_phone":    {"type": "string"},
                "reservation_date":  {"type": "string"},
                "reservation_time":  {"type": "string"},
                "party_size":        {"type": "integer"},
                "table_number":      {"type": "string"},
                "special_requests":  {"type": "string"},
            },
            "required": [
                "reservation_id", "confirmation_code", "customer_name",
                "reservation_date", "reservation_time", "party_size",
            ],
        },
    },
]

# ---------------------------------------------------------------------------
# Tool Executor — Maps tool names to actual async functions
# ---------------------------------------------------------------------------

async def execute_tool(tool_name: str, tool_input: Dict[str, Any]) -> str:
    """
    Dispatches a tool call from Claude to the appropriate Python function.
    Returns a JSON string result to feed back to the model.
    """
    logger.info(f"Executing tool: {tool_name} | Input: {tool_input}")

    try:
        # ── get_available_time_slots ────────────────────────────────────────
        if tool_name == "get_available_time_slots":
            slots = await get_available_time_slots(
                reservation_date=date.fromisoformat(tool_input["reservation_date"]),
                party_size=tool_input["party_size"],
                preference=tool_input.get("preference", "no_preference"),
            )
            if not slots:
                return json.dumps({"available": False, "message": "No availability found for that date."})
            return json.dumps({"available": True, "slots": slots})

        # ── check_table_availability ────────────────────────────────────────
        elif tool_name == "check_table_availability":
            tables = await check_availability(
                reservation_date=date.fromisoformat(tool_input["reservation_date"]),
                start_time=time.fromisoformat(tool_input["start_time"]),
                party_size=tool_input["party_size"],
                preference=tool_input.get("preference", "no_preference"),
            )
            if not tables:
                return json.dumps({"available": False, "tables": []})
            # Serialize UUID fields to strings for JSON
            safe_tables = [
                {k: str(v) if isinstance(v, UUID) else v for k, v in t.items()}
                for t in tables
            ]
            return json.dumps({"available": True, "tables": safe_tables})

        # ── find_or_create_customer ─────────────────────────────────────────
        elif tool_name == "find_or_create_customer":
            customer = await find_or_create_customer(**tool_input)
            safe_customer = {k: str(v) if isinstance(v, UUID) else v for k, v in customer.items()}
            # Remove sensitive fields returned to the model
            safe_customer.pop("created_at", None)
            safe_customer.pop("updated_at", None)
            return json.dumps({"success": True, "customer": safe_customer})

        # ── book_table ──────────────────────────────────────────────────────
        elif tool_name == "book_table":
            reservation = await book_table(
                customer_id=UUID(tool_input["customer_id"]),
                table_id=UUID(tool_input["table_id"]),
                reservation_date=date.fromisoformat(tool_input["reservation_date"]),
                start_time=time.fromisoformat(tool_input["start_time"]),
                party_size=tool_input["party_size"],
                special_requests=tool_input.get("special_requests"),
            )
            safe_res = {
                k: str(v) if isinstance(v, (UUID, date, time, datetime)) else v
                for k, v in reservation.items()
            }
            return json.dumps({"success": True, "reservation": safe_res})

        # ── get_reservation ─────────────────────────────────────────────────
        elif tool_name == "get_reservation":
            reservation = await get_reservation_by_code(tool_input["confirmation_code"])
            if not reservation:
                return json.dumps({"found": False, "message": "Reservation not found."})
            safe_res = {
                k: str(v) if isinstance(v, (UUID, date, time, datetime)) else v
                for k, v in reservation.items()
            }
            return json.dumps({"found": True, "reservation": safe_res})

        # ── modify_reservation ──────────────────────────────────────────────
        elif tool_name == "modify_reservation":
            kwargs: Dict[str, Any] = {
                "code": tool_input["confirmation_code"],
                "customer_email": tool_input["customer_email"],
            }
            if "new_date" in tool_input:
                kwargs["new_date"] = date.fromisoformat(tool_input["new_date"])
            if "new_time" in tool_input:
                kwargs["new_time"] = time.fromisoformat(tool_input["new_time"])
            if "new_party_size" in tool_input:
                kwargs["new_party_size"] = tool_input["new_party_size"]

            new_res = await modify_reservation(**kwargs)
            safe_res = {
                k: str(v) if isinstance(v, (UUID, date, time, datetime)) else v
                for k, v in new_res.items()
            }
            return json.dumps({"success": True, "new_reservation": safe_res})

        # ── cancel_reservation ──────────────────────────────────────────────
        elif tool_name == "cancel_reservation":
            result = await cancel_reservation(
                code=tool_input["confirmation_code"],
                customer_email=tool_input["customer_email"],
            )
            return json.dumps({"success": True, "cancelled_code": result["confirmation_code"]})

        # ── send_confirmation ───────────────────────────────────────────────
        elif tool_name == "send_confirmation":
            email_sent = False
            sms_sent   = False

            if tool_input.get("customer_email"):
                email_sent = await send_confirmation_email(
                    reservation_id=tool_input["reservation_id"],
                    **tool_input,
                )

            if tool_input.get("customer_phone"):
                sms_sent = await send_confirmation_sms(
                    reservation_id=tool_input["reservation_id"],
                    **tool_input,
                )

            return json.dumps({"email_sent": email_sent, "sms_sent": sms_sent})

        else:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})

    except ValueError as e:
        # Business logic errors (e.g., double booking, capacity exceeded)
        logger.warning(f"Tool {tool_name} raised ValueError: {e}")
        return json.dumps({"error": str(e), "type": "business_error"})
    except Exception as e:
        logger.exception(f"Tool {tool_name} raised unexpected error: {e}")
        return json.dumps({"error": "An unexpected error occurred. Please try again.", "type": "system_error"})


# ---------------------------------------------------------------------------
# Agent Loop — Handles multi-turn tool use
# ---------------------------------------------------------------------------

async def run_agent(
    user_message: str,
    conversation_history: List[Dict[str, Any]],
    context: Dict[str, Any],
) -> tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
    """
    Runs one full agent turn:
    1. Appends user message to history
    2. Calls Claude with tools
    3. Executes any tool calls and feeds results back
    4. Repeats until Claude gives a final text response
    5. Returns (final_response, updated_history, updated_context)

    Args:
        user_message:          The latest message from the user.
        conversation_history:  Full prior conversation (list of message dicts).
        context:               Extracted booking state (date, party size, etc.).

    Returns:
        Tuple of (assistant_text, new_history, updated_context)
    """
    # Add user message to history
    conversation_history.append({"role": "user", "content": user_message})

    # Inject context as a reminder if meaningful data has been collected
    system = SYSTEM_PROMPT
    if context:
        context_summary = json.dumps(context, indent=2, default=str)
        system += f"\n\n## CURRENT BOOKING CONTEXT (do not ask for info already collected)\n```json\n{context_summary}\n```"

    final_response = ""

    # Agentic loop — keep going until Claude stops calling tools
    while True:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=2048,
            system=system,
            tools=TOOLS,
            messages=conversation_history,
        )

        # Build the assistant's content block list (may include tool_use blocks)
        assistant_content = response.content
        conversation_history.append({
            "role": "assistant",
            "content": assistant_content,
        })

        # Check stop reason
        if response.stop_reason == "end_turn":
            # Extract the final text block
            for block in assistant_content:
                if hasattr(block, "text"):
                    final_response = block.text
            break

        elif response.stop_reason == "tool_use":
            # Execute all tool calls in parallel (or sequentially for safety)
            tool_results = []
            for block in assistant_content:
                if block.type == "tool_use":
                    result_str = await execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_str,
                    })
                    # Update context with any booking progress
                    _update_context(context, block.name, block.input, result_str)

            # Feed all tool results back in a single user turn
            conversation_history.append({
                "role": "user",
                "content": tool_results,
            })

        else:
            # Unexpected stop reason — extract whatever text is available
            for block in assistant_content:
                if hasattr(block, "text"):
                    final_response = block.text
            break

    return final_response, conversation_history, context


def _update_context(
    context: Dict[str, Any],
    tool_name: str,
    tool_input: Dict,
    result_str: str,
) -> None:
    """
    Updates the booking context dictionary based on tool calls.
    This lets the agent avoid re-asking for info it already collected.
    """
    try:
        result = json.loads(result_str)
    except json.JSONDecodeError:
        return

    if tool_name == "find_or_create_customer" and result.get("success"):
        customer = result.get("customer", {})
        context.update({
            "customer_id":   customer.get("id"),
            "customer_name": customer.get("full_name"),
            "customer_email": customer.get("email"),
            "customer_phone": customer.get("phone"),
        })

    elif tool_name == "book_table" and result.get("success"):
        res = result.get("reservation", {})
        context.update({
            "reservation_id":   res.get("id"),
            "confirmation_code": res.get("confirmation_code"),
            "booking_complete":  True,
        })

    elif tool_name in ("get_available_time_slots", "check_table_availability"):
        context.update({
            "reservation_date": tool_input.get("reservation_date"),
            "party_size":       tool_input.get("party_size"),
        })
