import base64
import html as _html
import uuid
import email.utils as email_utils
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.core.config import settings as core_settings
from app.dependencies import get_current_user, get_db
from app.models.booking import Booking
from app.models.email_message import EmailAttachment, EmailMessage
from app.schemas.email_message import EmailMessageOut
from app.tasks.oauth2 import get_graph_token

router = APIRouter(tags=["email-messages"])

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _to_html(text: str) -> str:
    """Convert plain text (with \n line breaks) to minimal HTML for email clients."""
    return _html.escape(text).replace("\n", "<br>").replace("\r", "")


async def _reply_via_graph(
    mailbox: str,
    graph_message_id: str,
    recipients: list[str],
    body_text: str,
    attachments: list[dict],
    cc_recipients: list[str] | None = None,
    bts_message_id: str | None = None,
):
    """Send a threaded reply using Graph's /reply endpoint.

    This preserves In-Reply-To and References headers automatically, keeping
    the email chain intact in the recipient's mail client.
    """
    token = get_graph_token(core_settings)
    msg: dict = {
        "toRecipients": [{"emailAddress": {"address": r}} for r in recipients],
    }
    if cc_recipients:
        msg["ccRecipients"] = [{"emailAddress": {"address": r}} for r in cc_recipients]
    if attachments:
        msg["attachments"] = [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": att["filename"],
                "contentType": att["content_type"],
                "contentBytes": base64.b64encode(att["data"]).decode(),
            }
            for att in attachments
        ]
    msg["body"] = {"contentType": "HTML", "content": _to_html(body_text)}
    # Stamp custom header so the Sent Items poller can dedup this message
    if bts_message_id:
        msg["internetMessageHeaders"] = [{"name": "X-BTS-Message-ID", "value": bts_message_id}]
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{GRAPH_BASE}/users/{mailbox}/messages/{graph_message_id}/reply",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"message": msg},
        )
    if resp.status_code != 202:
        raise HTTPException(502, f"Graph reply error: {resp.status_code} {resp.text[:300]}")


async def _send_via_graph(
    sender: str,
    recipients: list[str],
    subject: str,
    body_text: str,
    attachments: list[dict],
    cc_recipients: list[str] | None = None,
    message_id: str | None = None,
):
    """Fallback: send a new (non-threaded) email via sendMail.

    Only used when no graph_message_id is available for an inbound message
    (e.g. bookings created before graph_message_id was introduced).
    """
    token = get_graph_token(core_settings)
    msg: dict = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": _to_html(body_text)},
        "toRecipients": [{"emailAddress": {"address": r}} for r in recipients],
    }
    if cc_recipients:
        msg["ccRecipients"] = [{"emailAddress": {"address": r}} for r in cc_recipients]
    if attachments:
        msg["attachments"] = [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": att["filename"],
                "contentType": att["content_type"],
                "contentBytes": base64.b64encode(att["data"]).decode(),
            }
            for att in attachments
        ]
    if message_id:
        msg["internetMessageHeaders"] = [{"name": "X-BTS-Message-ID", "value": message_id}]
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{GRAPH_BASE}/users/{sender}/sendMail",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"message": msg, "saveToSentItems": True},
        )
    if resp.status_code != 202:
        raise HTTPException(502, f"Graph sendMail error: {resp.status_code} {resp.text[:300]}")


@router.get("/bookings/{booking_id}/messages", response_model=list[EmailMessageOut])
async def list_messages(
    booking_id: str,
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    result = await db.execute(
        select(EmailMessage)
        .where(EmailMessage.booking_id == booking_id)
        .options(selectinload(EmailMessage.attachments))
        .order_by(EmailMessage.sent_at.desc())
    )
    return result.scalars().all()


@router.post("/bookings/{booking_id}/reply", response_model=EmailMessageOut, status_code=201)
async def reply_to_booking(
    booking_id: str,
    body_text: str = Form(...),
    files: list[UploadFile] = File(default=[]),
    to_emails: str | None = Form(None),
    cc_emails: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    booking = await db.get(Booking, booking_id)
    if not booking:
        raise HTTPException(404, "Booking not found")

    sender_addr = core_settings.MAILBOX_EMAIL
    if not sender_addr:
        raise HTTPException(503, "Email sending not configured (MAILBOX_EMAIL not set)")

    # Find the most recent inbound message that has a graph_message_id (for proper threading).
    # Fall back to any inbound message for recipient/subject info.
    result = await db.execute(
        select(EmailMessage)
        .where(
            EmailMessage.booking_id == booking_id,
            EmailMessage.direction == "inbound",
            EmailMessage.graph_message_id.is_not(None),
        )
        .order_by(EmailMessage.sent_at.desc())
        .limit(1)
    )
    thread_anchor = result.scalar_one_or_none()

    if thread_anchor is None:
        # Fallback: any inbound message (pre-migration records without graph_message_id)
        fallback_result = await db.execute(
            select(EmailMessage)
            .where(EmailMessage.booking_id == booking_id, EmailMessage.direction == "inbound")
            .order_by(EmailMessage.sent_at.asc())
            .limit(1)
        )
        thread_anchor = fallback_result.scalar_one_or_none()

    reply_subject = booking.subject
    if not reply_subject.lower().startswith("re:"):
        reply_subject = f"Re: {reply_subject}"

    # Use explicit recipients if provided (Reply vs Reply All vs Forward distinction)
    if to_emails:
        all_recipients = [a.strip() for a in to_emails.split(',') if a.strip() and a.strip() != sender_addr]
    else:
        all_recipients = []
        if thread_anchor:
            for addr_source in [thread_anchor.from_email, thread_anchor.to_email, thread_anchor.cc_emails]:
                if not addr_source:
                    continue
                for addr in addr_source.split(','):
                    addr = addr.strip()
                    if addr and addr != sender_addr and addr not in all_recipients:
                        all_recipients.append(addr)
    if not all_recipients:
        all_recipients.append(booking.sender_email)

    # Parse CC recipients (exclude the mailbox itself)
    cc_list = [a.strip() for a in cc_emails.split(',') if a.strip() and a.strip() != sender_addr] if cc_emails else []

    # Generate a Message-ID so future inbound replies can be threaded back
    from pathlib import Path
    from app.storage import s3_key, upload_bytes

    msg_uuid = uuid.uuid4()
    outbound_mid = email_utils.make_msgid(idstring=str(msg_uuid).replace("-", ""), domain=sender_addr.split("@")[-1])

    # Read uploaded files into memory for Graph API (base64) and S3 storage
    saved_files: list[dict] = []
    graph_attachments: list[dict] = []

    for upload in files:
        if not upload.filename:
            continue
        data = await upload.read()
        safe_name = Path(upload.filename).name
        content_type = upload.content_type or "application/octet-stream"
        key = s3_key(booking_id, str(msg_uuid), safe_name)
        await upload_bytes(data, key, content_type)
        saved_files.append({"filename": safe_name, "content_type": content_type, "size_bytes": len(data), "storage_path": key})
        graph_attachments.append({"filename": safe_name, "content_type": content_type, "data": data})

    # Send via Graph reply endpoint if we have a graph_message_id (preserves thread).
    # Fall back to sendMail for old bookings ingested before graph_message_id was added.
    if thread_anchor and thread_anchor.graph_message_id:
        await _reply_via_graph(
            mailbox=sender_addr,
            graph_message_id=thread_anchor.graph_message_id,
            recipients=all_recipients,
            body_text=body_text,
            attachments=graph_attachments,
            cc_recipients=cc_list if cc_list else None,
            bts_message_id=outbound_mid,
        )
    else:
        await _send_via_graph(
            sender=sender_addr,
            recipients=all_recipients,
            subject=reply_subject,
            body_text=body_text,
            attachments=graph_attachments,
            cc_recipients=cc_list if cc_list else None,
            message_id=outbound_mid,
        )

    # Persist outbound message record
    email_msg = EmailMessage(
        id=msg_uuid,
        booking_id=booking_id,
        message_id=outbound_mid,
        conversation_id=thread_anchor.conversation_id if thread_anchor else None,
        direction="outbound",
        from_email=sender_addr,
        to_email=", ".join(all_recipients),
        cc_emails=", ".join(cc_list) if cc_list else None,
        subject=reply_subject,
        body_text=body_text,
        in_reply_to=thread_anchor.message_id if thread_anchor else None,
    )
    db.add(email_msg)
    await db.flush()

    for sf in saved_files:
        db.add(EmailAttachment(
            message_id=email_msg.id,
            filename=sf["filename"],
            content_type=sf["content_type"],
            size_bytes=sf["size_bytes"],
            storage_path=sf["storage_path"],
        ))

    # Mark this message as processed so the Sent Items poller skips it
    from app.models.processed_email import ProcessedEmail
    db.add(ProcessedEmail(message_id=outbound_mid))
    await db.commit()
    await db.refresh(email_msg)
    result2 = await db.execute(
        select(EmailMessage)
        .where(EmailMessage.id == email_msg.id)
        .options(selectinload(EmailMessage.attachments))
    )
    return result2.scalar_one()


@router.post("/bookings/{booking_id}/sync-emails")
async def sync_booking_emails(
    booking_id: str,
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    """Fetch the full conversation history from Graph API for a booking and
    add any emails that are missing from the local database."""
    from datetime import timezone
    from app.models.processed_email import ProcessedEmail
    from app.storage import s3_key, upload_bytes

    booking = await db.get(Booking, booking_id)
    if not booking:
        raise HTTPException(404, "Booking not found")

    mailbox = core_settings.MAILBOX_EMAIL
    if not mailbox:
        raise HTTPException(503, "MAILBOX_EMAIL not configured")

    # Resolve the Outlook conversationId from any existing message for this booking
    conv_result = await db.execute(
        select(EmailMessage.conversation_id)
        .where(EmailMessage.booking_id == booking_id, EmailMessage.conversation_id.is_not(None))
        .limit(1)
    )
    conversation_id = conv_result.scalar_one_or_none()
    if not conversation_id:
        return {"synced": 0}

    token = get_graph_token(core_settings)
    auth_hdr = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    SELECT = (
        "id,subject,from,toRecipients,ccRecipients,body,"
        "receivedDateTime,sentDateTime,internetMessageId,conversationId,"
        "hasAttachments,internetMessageHeaders"
    )

    import asyncio as _asyncio

    all_msgs: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as client:

        async def _graph_get(req_url: str | None) -> dict:
            """One Graph request with a single 429 retry honouring Retry-After."""
            for attempt in range(2):
                if req_url is None:
                    r = await client.get(
                        f"{GRAPH_BASE}/users/{mailbox}/messages",
                        params={
                            "$filter": f"conversationId eq '{conversation_id}'",
                            "$select": SELECT,
                            "$top": "50",
                        },
                        headers=auth_hdr,
                    )
                else:
                    r = await client.get(req_url, headers=auth_hdr)

                if r.status_code == 429:
                    wait = int(r.headers.get("Retry-After", "10"))
                    if attempt == 0:
                        await _asyncio.sleep(wait)
                        continue
                    raise HTTPException(429, "Microsoft Graph is rate-limited — please try again in a few seconds")

                if r.status_code != 200:
                    raise HTTPException(502, f"Graph error {r.status_code}: {r.text[:200]}")

                return r.json()
            raise HTTPException(502, "Graph request failed after retry")

        url: str | None = None
        while True:
            data = await _graph_get(url)
            all_msgs.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
            if not url:
                break

        synced = 0
        for msg in all_msgs:
            raw_mid = msg.get("internetMessageId")
            graph_id = msg["id"]

            hdrs = msg.get("internetMessageHeaders") or []
            def _hdr(name: str) -> str:
                for h in hdrs:
                    if h.get("name", "").lower() == name.lower():
                        return h.get("value", "")
                return ""

            # Skip messages already saved — check both the real internetMessageId
            # and the BTS-internal ID stamped on app-sent replies (X-BTS-Message-ID).
            # This prevents duplicating BTS replies whose Graph ID differs from the
            # stored fake outbound_mid.
            if raw_mid:
                already_saved = await db.scalar(
                    select(EmailMessage.id)
                    .where(EmailMessage.message_id == raw_mid)
                    .limit(1)
                )
                if already_saved:
                    continue

            x_bts_id = _hdr("X-BTS-Message-ID")
            if x_bts_id:
                bts_already_saved = await db.scalar(
                    select(EmailMessage.id)
                    .where(EmailMessage.message_id == x_bts_id)
                    .limit(1)
                )
                if bts_already_saved:
                    continue

            from_email = msg.get("from", {}).get("emailAddress", {}).get("address", "")
            direction = "outbound" if from_email.lower() == mailbox.lower() else "inbound"

            time_str = msg.get("receivedDateTime") or msg.get("sentDateTime") or ""
            try:
                sent_at = datetime.fromisoformat(time_str.replace("Z", "+00:00")) if time_str else datetime.now(timezone.utc)
            except ValueError:
                sent_at = datetime.now(timezone.utc)

            def _addrs(lst: list | None) -> str:
                return ", ".join(
                    r["emailAddress"]["address"]
                    for r in (lst or [])
                    if r.get("emailAddress", {}).get("address")
                )

            body_obj = msg.get("body") or {}
            body_type = body_obj.get("contentType", "text").lower()
            body_content = body_obj.get("content", "")

            email_msg_id = uuid.uuid4()
            record = EmailMessage(
                id=email_msg_id,
                booking_id=booking_id,
                message_id=raw_mid,
                in_reply_to=_hdr("In-Reply-To") or None,
                conversation_id=msg.get("conversationId"),
                graph_message_id=graph_id,
                direction=direction,
                from_email=from_email,
                to_email=_addrs(msg.get("toRecipients")) or mailbox,
                cc_emails=_addrs(msg.get("ccRecipients")) or None,
                subject=msg.get("subject"),
                body_text=body_content if body_type == "text" else None,
                body_html=body_content if body_type == "html" else None,
                sent_at=sent_at,
            )
            db.add(record)
            await db.flush()

            if msg.get("hasAttachments"):
                att_resp = await client.get(
                    f"{GRAPH_BASE}/users/{mailbox}/messages/{graph_id}/attachments",
                    headers=auth_hdr,
                )
                if att_resp.status_code == 200:
                    import re as _re
                    inline_map: dict[str, str] = {}
                    for att in att_resp.json().get("value", []):
                        if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
                            continue
                        raw_bytes = att.get("contentBytes", "")
                        data_bytes = base64.b64decode(raw_bytes) if raw_bytes else b""
                        filename = att.get("name", "attachment")
                        ct = att.get("contentType", "application/octet-stream")
                        content_id = att.get("contentId", "").strip("<>").strip()
                        is_image = ct.lower().startswith("image/")
                        # Inline CID images go into the HTML as data URIs (not S3)
                        if content_id and is_image and raw_bytes:
                            inline_map[content_id] = f"data:{ct};base64,{raw_bytes}"
                        else:
                            key = s3_key(booking_id, str(email_msg_id), filename)
                            await upload_bytes(data_bytes, key, ct)
                            db.add(EmailAttachment(
                                message_id=email_msg_id,
                                filename=filename,
                                content_type=ct,
                                size_bytes=len(data_bytes),
                                storage_path=key,
                            ))
                    # Replace cid: references in the stored HTML with base64 data URIs
                    if inline_map and record.body_html:
                        for cid, data_uri in inline_map.items():
                            pattern = _re.compile(_re.escape(f"cid:{cid}"), _re.IGNORECASE)
                            record.body_html = pattern.sub(data_uri, record.body_html)

            if raw_mid:
                from sqlalchemy.dialects.postgresql import insert as pg_insert
                from app.models.processed_email import ProcessedEmail
                await db.execute(
                    pg_insert(ProcessedEmail)
                    .values(message_id=raw_mid)
                    .on_conflict_do_nothing()
                )

            synced += 1

    await db.commit()
    return {"synced": synced}


@router.get("/email-attachments/{attachment_id}")
async def download_attachment(
    attachment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    att = await db.get(EmailAttachment, attachment_id)
    if not att:
        raise HTTPException(404, "Attachment not found")
    from app.storage import presigned_url
    url = await presigned_url(att.storage_path)
    return {"url": url, "filename": att.filename}
