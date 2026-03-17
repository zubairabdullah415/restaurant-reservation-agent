"""
notifications.py — Email & SMS Notification Service
======================================================
Sends booking confirmations and reminders via:
  - Email:  SendGrid (transactional email API)
  - SMS:    Twilio (programmable messaging)

All sends are logged to the `notification_log` table for retry and audit.
"""

import logging
from typing import Optional, Dict, Any
import httpx

from config import settings
from database import get_pool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Email Templates
# ---------------------------------------------------------------------------

def _build_email_html(data: Dict[str, Any]) -> str:
    """Generates a clean, branded HTML confirmation email."""
    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Reservation Confirmed — The Grand Olive</title>
  <style>
    body {{ font-family: 'Georgia', serif; background: #f9f5f0; margin: 0; padding: 20px; }}
    .container {{ max-width: 600px; margin: 0 auto; background: white;
                  border-radius: 12px; overflow: hidden; box-shadow: 0 4px 20px rgba(0,0,0,0.08); }}
    .header {{ background: #2C5F2E; color: white; padding: 32px 40px; text-align: center; }}
    .header h1 {{ margin: 0; font-size: 28px; letter-spacing: 1px; }}
    .header p  {{ margin: 8px 0 0; opacity: 0.85; font-size: 14px; }}
    .body  {{ padding: 36px 40px; }}
    .greeting {{ font-size: 18px; color: #333; margin-bottom: 24px; }}
    .detail-card {{ background: #f5f0e8; border-radius: 8px; padding: 24px; margin: 20px 0; }}
    .detail-row  {{ display: flex; justify-content: space-between; padding: 8px 0;
                    border-bottom: 1px solid #e8e0d0; font-size: 15px; }}
    .detail-row:last-child {{ border-bottom: none; }}
    .label {{ color: #777; font-weight: normal; }}
    .value {{ color: #222; font-weight: bold; }}
    .code-box {{ background: #2C5F2E; color: white; text-align: center;
                 border-radius: 8px; padding: 16px; margin: 24px 0; }}
    .code-box .code {{ font-size: 28px; font-weight: bold; letter-spacing: 4px; }}
    .code-box p {{ margin: 4px 0 0; font-size: 12px; opacity: 0.8; }}
    .footer {{ background: #f0ebe1; padding: 20px 40px; text-align: center;
               font-size: 13px; color: #888; }}
    .footer a {{ color: #2C5F2E; text-decoration: none; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>🫒 The Grand Olive</h1>
      <p>42 Harrington Square, London · reservations@thegrandolive.com</p>
    </div>
    <div class="body">
      <p class="greeting">Dear {data.get('customer_name', 'Valued Guest')},</p>
      <p style="color:#555; line-height:1.6;">
        Your reservation has been confirmed. We're delighted to welcome you to
        <strong>The Grand Olive</strong> and look forward to a wonderful dining experience together.
      </p>

      <div class="detail-card">
        <div class="detail-row">
          <span class="label">📅 Date</span>
          <span class="value">{data.get('reservation_date', '')}</span>
        </div>
        <div class="detail-row">
          <span class="label">🕐 Time</span>
          <span class="value">{data.get('reservation_time', '')}</span>
        </div>
        <div class="detail-row">
          <span class="label">👥 Party Size</span>
          <span class="value">{data.get('party_size', '')} guests</span>
        </div>
        <div class="detail-row">
          <span class="label">🪑 Table</span>
          <span class="value">{data.get('table_number', '')}</span>
        </div>
        {"" if not data.get('special_requests') else f'''
        <div class="detail-row">
          <span class="label">📋 Special Requests</span>
          <span class="value">{data.get('special_requests')}</span>
        </div>
        '''}
      </div>

      <div class="code-box">
        <div class="code">{data.get('confirmation_code', '')}</div>
        <p>Your Confirmation Code — keep this safe</p>
      </div>

      <p style="color:#555; line-height:1.6; font-size:14px;">
        Need to modify or cancel? Simply reply to this email or contact Aria, our AI reservation
        assistant, with your confirmation code and the email address used to book.
      </p>
      <p style="color:#555; line-height:1.6; font-size:14px;">
        We kindly ask that you notify us at least 2 hours in advance for any changes.
        A courtesy reminder will be sent to you 24 hours before your visit.
      </p>
    </div>
    <div class="footer">
      <p>📞 +44 20 7946 0921 &nbsp;|&nbsp;
         🌐 <a href="https://www.thegrandolive.com">www.thegrandolive.com</a> &nbsp;|&nbsp;
         📍 42 Harrington Square, London</p>
      <p style="margin-top:8px;">© The Grand Olive. All rights reserved.</p>
    </div>
  </div>
</body>
</html>
"""


def _build_sms_text(data: Dict[str, Any]) -> str:
    """Short, information-dense SMS confirmation."""
    return (
        f"The Grand Olive ✓\n"
        f"Booking confirmed for {data.get('customer_name')}!\n"
        f"📅 {data.get('reservation_date')} at {data.get('reservation_time')}\n"
        f"👥 {data.get('party_size')} guests | Table {data.get('table_number')}\n"
        f"Code: {data.get('confirmation_code')}\n"
        f"Modify/Cancel: +44 20 7946 0921"
    )


# ---------------------------------------------------------------------------
# SendGrid Email
# ---------------------------------------------------------------------------

async def send_confirmation_email(
    reservation_id: str,
    customer_email: str,
    customer_name: str,
    confirmation_code: str,
    reservation_date: str,
    reservation_time: str,
    party_size: int,
    table_number: str = "TBD",
    special_requests: Optional[str] = None,
    **kwargs,  # Accept extra kwargs gracefully
) -> bool:
    """
    Sends a transactional confirmation email via SendGrid API.
    Logs the attempt to notification_log regardless of outcome.
    """
    data = {
        "customer_name": customer_name,
        "customer_email": customer_email,
        "confirmation_code": confirmation_code,
        "reservation_date": reservation_date,
        "reservation_time": reservation_time,
        "party_size": party_size,
        "table_number": table_number,
        "special_requests": special_requests,
    }

    payload = {
        "personalizations": [{"to": [{"email": customer_email, "name": customer_name}]}],
        "from": {
            "email": settings.EMAIL_FROM_ADDRESS,
            "name":  settings.EMAIL_FROM_NAME,
        },
        "subject": f"✅ Reservation Confirmed — {confirmation_code} | The Grand Olive",
        "content": [
            {
                "type": "text/plain",
                "value": (
                    f"Your reservation at The Grand Olive is confirmed!\n"
                    f"Date: {reservation_date} at {reservation_time}\n"
                    f"Party: {party_size} guests | Table: {table_number}\n"
                    f"Code: {confirmation_code}"
                ),
            },
            {"type": "text/html", "value": _build_email_html(data)},
        ],
    }

    provider_id = None
    error_msg   = None
    success     = False

    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            resp = await http.post(
                "https://api.sendgrid.com/v3/mail/send",
                json=payload,
                headers={
                    "Authorization": f"Bearer {settings.SENDGRID_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code == 202:
                provider_id = resp.headers.get("X-Message-Id")
                success     = True
                logger.info(f"Email sent to {customer_email} | MsgID: {provider_id}")
            else:
                error_msg = f"SendGrid error {resp.status_code}: {resp.text[:200]}"
                logger.error(error_msg)

    except httpx.RequestError as e:
        error_msg = f"Network error sending email: {e}"
        logger.exception(error_msg)

    # Log to database
    await _log_notification(
        reservation_id=reservation_id,
        channel="email",
        recipient=customer_email,
        template_name="booking_confirmation",
        success=success,
        provider_id=provider_id,
        error_message=error_msg,
    )
    return success


# ---------------------------------------------------------------------------
# Twilio SMS
# ---------------------------------------------------------------------------

async def send_confirmation_sms(
    reservation_id: str,
    customer_phone: str,
    customer_name: str,
    confirmation_code: str,
    reservation_date: str,
    reservation_time: str,
    party_size: int,
    table_number: str = "TBD",
    **kwargs,
) -> bool:
    """
    Sends an SMS via Twilio Messages API.
    Phone number must be in E.164 format (e.g., +447911123456).
    """
    data = {
        "customer_name":     customer_name,
        "confirmation_code": confirmation_code,
        "reservation_date":  reservation_date,
        "reservation_time":  reservation_time,
        "party_size":        party_size,
        "table_number":      table_number,
    }
    body = _build_sms_text(data)

    provider_id = None
    error_msg   = None
    success     = False

    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            resp = await http.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{settings.TWILIO_ACCOUNT_SID}/Messages.json",
                auth=(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN),
                data={"From": settings.TWILIO_FROM_NUMBER, "To": customer_phone, "Body": body},
            )
            result = resp.json()
            if resp.status_code == 201:
                provider_id = result.get("sid")
                success     = True
                logger.info(f"SMS sent to {customer_phone} | SID: {provider_id}")
            else:
                error_msg = f"Twilio error {resp.status_code}: {result.get('message', 'Unknown')}"
                logger.error(error_msg)

    except httpx.RequestError as e:
        error_msg = f"Network error sending SMS: {e}"
        logger.exception(error_msg)

    await _log_notification(
        reservation_id=reservation_id,
        channel="sms",
        recipient=customer_phone,
        template_name="booking_confirmation",
        success=success,
        provider_id=provider_id,
        error_message=error_msg,
    )
    return success


# ---------------------------------------------------------------------------
# Notification Logger
# ---------------------------------------------------------------------------

async def _log_notification(
    reservation_id: str,
    channel: str,
    recipient: str,
    template_name: str,
    success: bool,
    provider_id: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    """Writes a notification attempt record to notification_log."""
    from datetime import datetime, timezone
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO notification_log
                (reservation_id, channel, recipient, template_name,
                 status, provider_id, error_message, sent_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            reservation_id,
            channel,
            recipient,
            template_name,
            "sent" if success else "failed",
            provider_id,
            error_message,
            datetime.now(timezone.utc) if success else None,
        )
