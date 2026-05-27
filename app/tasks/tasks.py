import asyncio
import re
import uuid
from datetime import date, datetime, timezone
from email.header import decode_header
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from random import randint
import email as email_lib

import httpx

from app.tasks.celery_app import celery_app
from app.core.config import settings
from app.tasks.oauth2 import get_graph_token

POINTER_KEY = "bts:allocation:pointer"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _run(coro):
    return asyncio.run(coro)


def _extract_body(msg) -> tuple[str | None, str | None]:
    body_text = None
    body_html = None
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = part.get("Content-Disposition", "")
            if "attachment" in cd:
                continue
            charset = part.get_content_charset() or "utf-8"
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            if ct == "text/plain" and body_text is None:
                body_text = payload.decode(charset, errors="replace")
            elif ct == "text/html" and body_html is None:
                body_html = payload.decode(charset, errors="replace")
    else:
        ct = msg.get_content_type()
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        if payload:
            text = payload.decode(charset, errors="replace")
            if ct == "text/html":
                body_html = text
            else:
                body_text = text
    return body_text, body_html


def _extract_attachments(msg) -> list[dict]:
    attachments = []
    for part in msg.walk():
        cd = part.get("Content-Disposition", "")
        if "attachment" not in cd:
            continue
        raw_filename = part.get_filename()
        if not raw_filename:
            continue
        decoded = decode_header(raw_filename)
        filename = "".join(
            p.decode(enc or "utf-8") if isinstance(p, bytes) else p
            for p, enc in decoded
        )
        data = part.get_payload(decode=True)
        if data:
            attachments.append({
                "filename": filename,
                "content_type": part.get_content_type() or "application/octet-stream",
                "data": data,
            })
    return attachments


def _parse_addresses(header_val: str | None) -> str:
    """Return a comma-separated string of all email addresses from a To/CC header."""
    if not header_val:
        return ""
    return ", ".join(addr for _, addr in getaddresses([header_val]) if addr)


def _strip_re_prefix(subject: str) -> str:
    """Strip all leading 'Re:' / 'RE:' / 'Fwd:' prefixes to get the base subject."""
    s = subject.strip()
    while True:
        lower = s.lower()
        if lower.startswith("re:"):
            s = s[3:].lstrip()
        elif lower.startswith("fwd:"):
            s = s[4:].lstrip()
        else:
            break
    return s


async def _find_existing_booking_id(db, in_reply_to: str, references: str, sender_email: str, subject: str) -> str | None:
    """
    Detect whether this email is a reply to an existing booking.

    Strategy (in priority order):
    1. In-Reply-To / References headers → match against email_messages.message_id
    2. Subject starts with Re: and base subject + sender_email matches an existing booking
    """
    from sqlalchemy import select
    from app.models.email_message import EmailMessage
    from app.models.booking import Booking

    # 1. Header-based threading (most reliable)
    for header_val in [in_reply_to, references]:
        if not header_val:
            continue
        for mid in header_val.split():
            mid = mid.strip()
            if not mid:
                continue
            result = await db.execute(
                select(EmailMessage.booking_id)
                .where(EmailMessage.message_id == mid)
                .limit(1)
            )
            booking_id = result.scalar_one_or_none()
            if booking_id:
                return booking_id

    # 2. Subject-based fallback
    base_subject = _strip_re_prefix(subject)
    if base_subject != subject.strip():
        result = await db.execute(
            select(Booking.id)
            .where(
                Booking.subject == base_subject,
                Booking.sender_email == sender_email,
            )
            .order_by(Booking.received_at.desc())
            .limit(1)
        )
        booking_id = result.scalar_one_or_none()
        if booking_id:
            return booking_id

    return None


async def _save_attachments(db, raw_attachments: list[dict], booking_id: str, email_msg_id: uuid.UUID):
    from app.models.email_message import EmailAttachment
    from app.storage import s3_key, upload_bytes
    if not raw_attachments:
        return
    for att in raw_attachments:
        safe_name = Path(att["filename"]).name
        key = s3_key(booking_id, str(email_msg_id), safe_name)
        try:
            await upload_bytes(att["data"], key, att.get("content_type", "application/octet-stream"))
        except Exception as e:
            print(f"[BTS] S3 upload failed for {safe_name}: {e} — attachment skipped")
            continue
        db.add(EmailAttachment(
            message_id=email_msg_id,
            filename=safe_name,
            content_type=att["content_type"],
            size_bytes=len(att["data"]),
            storage_path=key,
        ))


# ------------------------------------------------------------------ #
#  Graph API helpers                                                   #
# ------------------------------------------------------------------ #

def _graph_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _graph_get_messages(client: httpx.Client, token: str, mailbox: str, allowed_sender: str, lookback_minutes: int = 60) -> list[dict]:
    """Fetch inbox messages received in the last `lookback_minutes`, handling OData pagination.

    Uses time-based fetch instead of isRead filter so emails opened in Outlook
    before the poll runs are not missed. ProcessedEmail dedup prevents reprocessing.
    """
    from datetime import timedelta
    select_fields = (
        "id,subject,from,toRecipients,ccRecipients,body,"
        "receivedDateTime,internetMessageId,conversationId,"
        "categories,hasAttachments,internetMessageHeaders"
    )
    since = (datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    filter_clause = f"receivedDateTime ge {since}"
    if allowed_sender:
        filter_clause += f" and from/emailAddress/address eq '{allowed_sender}'"

    messages = []
    url = None
    while True:
        if url is None:
            resp = client.get(
                f"{GRAPH_BASE}/users/{mailbox}/mailFolders/Inbox/messages",
                params={"$filter": filter_clause, "$select": select_fields, "$top": "50"},
                headers=_graph_headers(token),
            )
        else:
            resp = client.get(url, headers=_graph_headers(token))
        resp.raise_for_status()
        data = resp.json()
        messages.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
        if not url:
            break
    return messages


def _graph_get_attachments(client: httpx.Client, token: str, mailbox: str, msg_id: str) -> list[dict]:
    url = f"{GRAPH_BASE}/users/{mailbox}/messages/{msg_id}/attachments"
    resp = client.get(url, headers=_graph_headers(token))
    resp.raise_for_status()
    attachments = []
    import base64
    for att in resp.json().get("value", []):
        if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
            continue
        raw = att.get("contentBytes", "")
        attachments.append({
            "filename": att.get("name", "attachment"),
            "content_type": att.get("contentType", "application/octet-stream"),
            "data": base64.b64decode(raw) if raw else b"",
        })
    return attachments


def _graph_mark_read(client: httpx.Client, token: str, mailbox: str, msg_id: str):
    url = f"{GRAPH_BASE}/users/{mailbox}/messages/{msg_id}"
    client.patch(url, headers=_graph_headers(token), json={"isRead": True})


def _parse_graph_addresses(recipients: list[dict]) -> str:
    return ", ".join(r["emailAddress"]["address"] for r in recipients if r.get("emailAddress", {}).get("address"))


def _get_internet_header(msg: dict, name: str) -> str:
    for h in msg.get("internetMessageHeaders") or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


# ------------------------------------------------------------------ #
#  Email polling                                                       #
# ------------------------------------------------------------------ #

@celery_app.task(name="app.tasks.tasks.poll_email_inbox", bind=True, max_retries=3)
def poll_email_inbox(self):
    try:
        _run(_poll_inbox_async())
    except Exception as exc:
        raise self.retry(exc=exc, countdown=10)


async def _poll_inbox_async():
    import redis.asyncio as aioredis
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
    from app.models.booking import Booking
    from app.models.agent import Agent
    from app.models.attendance import Attendance
    from app.models.email_message import EmailMessage

    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    redis = aioredis.from_url(settings.REDIS_URL)

    token = get_graph_token(settings)
    mailbox = settings.MAILBOX_EMAIL

    # Parse cutoff datetime once
    cutoff_dt: datetime | None = None
    if settings.PROCESS_EMAILS_SINCE:
        try:
            cutoff_dt = datetime.fromisoformat(settings.PROCESS_EMAILS_SINCE)
            if cutoff_dt.tzinfo is None:
                cutoff_dt = cutoff_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            print(f"[BTS] Invalid PROCESS_EMAILS_SINCE format, ignoring: {settings.PROCESS_EMAILS_SINCE}")

    with httpx.Client(timeout=30) as client:
        messages = _graph_get_messages(client, token, mailbox, settings.ALLOWED_SENDER)
        print(f"[BTS] Messages from Graph API (last 60 min): {len(messages)} | sender-filter: '{settings.ALLOWED_SENDER}' | cutoff: {cutoff_dt}")

        for msg in messages:
            graph_msg_id = msg["id"]

            # Parse received time
            received_str = msg.get("receivedDateTime", "")
            try:
                sent_at = datetime.fromisoformat(received_str.replace("Z", "+00:00")) if received_str else datetime.now(timezone.utc)
            except ValueError:
                sent_at = datetime.now(timezone.utc)

            # Skip emails before cutoff
            if cutoff_dt and sent_at < cutoff_dt:
                print(f"[BTS] Skipping email before cutoff ({sent_at})")
                continue

            subject = msg.get("subject") or "(No Subject)"
            sender_email = msg.get("from", {}).get("emailAddress", {}).get("address", "")
            raw_message_id = msg.get("internetMessageId")
            to_emails = _parse_graph_addresses(msg.get("toRecipients") or [])
            cc_emails = _parse_graph_addresses(msg.get("ccRecipients") or []) or None

            # Outlook categories come as a native list — no parsing needed
            categories: list[str] = msg.get("categories") or []
            category_priority: str | None = None
            category_agent_name: str | None = None
            for cat in categories:
                category_agent_name = category_agent_name or cat

            # Email body
            body_obj = msg.get("body") or {}
            body_type = body_obj.get("contentType", "text")
            body_content = body_obj.get("content", "")
            body_text = body_content if body_type == "text" else None
            body_html = body_content if body_type == "html" else None

            # Attachments
            raw_attachments = []
            if msg.get("hasAttachments"):
                raw_attachments = _graph_get_attachments(client, token, mailbox, graph_msg_id)

            # Threading headers
            in_reply_to = _get_internet_header(msg, "In-Reply-To")
            references = _get_internet_header(msg, "References")

            print(f"[BTS] Processing email from: {sender_email} | Subject: {subject}")

            # Dedup by internetMessageId
            if raw_message_id:
                async with AsyncSessionLocal() as check_db:
                    from sqlalchemy import select as sa_select
                    from app.models.processed_email import ProcessedEmail
                    exists = await check_db.scalar(
                        sa_select(ProcessedEmail.message_id)
                        .where(ProcessedEmail.message_id == raw_message_id)
                        .limit(1)
                    )
                    if exists:
                        print(f"[BTS] Already processed {raw_message_id[:40]}… — skipping")
                        _graph_mark_read(client, token, mailbox, graph_msg_id)
                        continue
                    check_db.add(ProcessedEmail(message_id=raw_message_id))
                    await check_db.commit()

            email_msg_id = uuid.uuid4()

            async with AsyncSessionLocal() as db:
                try:
                    existing_booking_id = await _find_existing_booking_id(
                        db, in_reply_to, references, sender_email, subject
                    )

                    if existing_booking_id:
                        booking_obj = await db.get(Booking, existing_booking_id)
                        reopened = False
                        # if booking_obj and booking_obj.status == "Completed":
                        #     booking_obj.status = "In Progress"
                        #     reopened = True
                        #     from app.models.booking import BookingEvent
                        #     db.add(BookingEvent(
                        #         booking_id=existing_booking_id,
                        #         event="status_changed",
                        #         actor_name="System",
                        #         old_value="Completed",
                        #         new_value="In Progress",
                        #     ))
                        #     db.add(BookingEvent(
                        #         booking_id=existing_booking_id,
                        #         event="reply_received",
                        #         actor_name="System",
                        #         new_value=f"Reply from {sender_email}",
                        #     ))

                        email_record = EmailMessage(
                            id=email_msg_id,
                            booking_id=existing_booking_id,
                            message_id=raw_message_id,
                            in_reply_to=in_reply_to or None,
                            direction="inbound",
                            from_email=sender_email,
                            to_email=to_emails or mailbox,
                            cc_emails=cc_emails,
                            subject=subject,
                            body_text=body_text,
                            body_html=body_html,
                            sent_at=sent_at,
                        )
                        db.add(email_record)
                        await db.flush()
                        await _save_attachments(db, raw_attachments, existing_booking_id, email_msg_id)
                        await db.commit()
                        _graph_mark_read(client, token, mailbox, graph_msg_id)
                        print(f"[BTS] Reply appended to existing booking: {existing_booking_id} | Reopened: {reopened} | Attachments: {len(raw_attachments)}")
                        import json as _json
                        await redis.publish("bts:events", _json.dumps({"type": "new_message", "booking_id": existing_booking_id, "reopened": reopened}))

                    else:
                        # Priority: Outlook category takes precedence over subject keywords
                        if category_priority:
                            priority = category_priority
                        else:
                            priority = "Not Urgent"

                        booking_id = f"BKG-{date.today().year}-{randint(10000, 99999):05d}"

                        booking = Booking(
                            id=booking_id,
                            subject=subject,
                            priority=priority,
                            sender_email=sender_email,
                            status="Pending",
                        )
                        db.add(booking)
                        await db.flush()

                        from app.models.booking import BookingEvent
                        db.add(BookingEvent(
                            booking_id=booking_id,
                            event="created",
                            actor_name="System",
                            new_value=f"Priority: {priority}",
                        ))

                        email_record = EmailMessage(
                            id=email_msg_id,
                            booking_id=booking_id,
                            message_id=raw_message_id,
                            direction="inbound",
                            from_email=sender_email,
                            to_email=to_emails or mailbox,
                            cc_emails=cc_emails,
                            subject=subject,
                            body_text=body_text,
                            body_html=body_html,
                            sent_at=sent_at,
                        )
                        db.add(email_record)
                        await db.flush()
                        await _save_attachments(db, raw_attachments, booking_id, email_msg_id)

                        # Agent assignment: Outlook category name → direct assign, else round-robin
                        from app.models.allocation import AllocationLog
                        today = date.today()
                        assigned = None
                        log_pointer_value = -1
                        log_pool_size = 0

                        if category_agent_name:
                            cat_result = await db.execute(
                                select(Agent)
                                .join(Attendance, (Attendance.agent_id == Agent.id) & (Attendance.date == today), isouter=True)
                                .where(
                                    Attendance.status == "Present",
                                    Agent.name.ilike(f"%{category_agent_name}%"),
                                )
                                .limit(1)
                            )
                            assigned = cat_result.scalars().first()
                            if assigned:
                                print(f"[BTS] Category-assigned to: {assigned.name} (category: {category_agent_name})")

                        if not assigned:
                            rr_result = await db.execute(
                                select(Agent)
                                .join(Attendance, (Attendance.agent_id == Agent.id) & (Attendance.date == today), isouter=True)
                                .where(Attendance.status == "Present")
                                .order_by(Agent.name)
                            )
                            agents = rr_result.scalars().all()
                            if agents:
                                pool_size = len(agents)
                                pointer = await redis.incr(POINTER_KEY)
                                pointer -= 1
                                await redis.set(POINTER_KEY, (pointer + 1) % pool_size)
                                assigned = agents[pointer % pool_size]
                                log_pointer_value = pointer % pool_size
                                log_pool_size = pool_size
                                print(f"[BTS] Round-robin allocated to: {assigned.name}")

                        if assigned:
                            booking.agent_id = assigned.id
                            booking.status = "In Progress"
                            booking.assigned_at = datetime.now(timezone.utc)
                            db.add(AllocationLog(
                                booking_id=booking_id,
                                agent_id=assigned.id,
                                pointer_value=log_pointer_value,
                                pool_size=log_pool_size,
                                allocated_at=datetime.now(timezone.utc),
                            ))
                            method = "Category assignment" if log_pointer_value == -1 else f"Round-robin (pool: {log_pool_size})"
                            db.add(BookingEvent(
                                booking_id=booking_id,
                                event="agent_assigned",
                                actor_name="System",
                                new_value=assigned.name,
                                old_value=method,
                            ))
                            db.add(BookingEvent(
                                booking_id=booking_id,
                                event="status_changed",
                                actor_name="System",
                                old_value="Pending",
                                new_value="In Progress",
                            ))
                        else:
                            print(f"[BTS] No present agents — booking stays Pending")
                            db.add(BookingEvent(
                                booking_id=booking_id,
                                event="no_agents_available",
                                actor_name="System",
                                new_value="Booking stays Open — no present agents",
                            ))

                        await db.commit()
                        _graph_mark_read(client, token, mailbox, graph_msg_id)
                        print(f"[BTS] New booking: {booking_id} | Status: {booking.status} | Priority: {priority} | Attachments: {len(raw_attachments)}")
                        import json as _json
                        await redis.publish("bts:events", _json.dumps({"type": "new_booking", "id": booking_id}))

                except Exception as e:
                    await db.rollback()
                    print(f"[BTS] Error processing email: {e}")

    await redis.aclose()
    await engine.dispose()
