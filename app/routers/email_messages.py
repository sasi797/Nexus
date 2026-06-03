import base64
import uuid
import email.utils as email_utils

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
    # Stamp custom header so the Sent Items poller can dedup this message
    if bts_message_id:
        msg["internetMessageHeaders"] = [{"name": "X-BTS-Message-ID", "value": bts_message_id}]
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{GRAPH_BASE}/users/{mailbox}/messages/{graph_message_id}/reply",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"message": msg, "comment": body_text},
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
        "body": {"contentType": "Text", "content": body_text},
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
