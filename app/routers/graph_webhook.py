"""
Microsoft Graph change notification webhook endpoint.

Graph POSTs here whenever a new email arrives in the inbox, replacing the
30-second polling loop. Each notification enqueues the existing
poll_email_inbox Celery task, which runs in the worker container (not this
API process) and uses the existing dedup table to avoid reprocessing.
"""
from typing import Optional

from fastapi import APIRouter, Query, Request, Response

from app.core.config import settings

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/graph")
async def graph_webhook(
    request: Request,
    validationToken: Optional[str] = Query(None),
):
    # Validation handshake — Graph sends a POST with this query param when the
    # subscription is first created. Must echo it back as plain text within 10s.
    if validationToken:
        return Response(content=validationToken, media_type="text/plain", status_code=200)

    try:
        body = await request.json()
    except Exception:
        return Response(status_code=400)

    for notification in body.get("value", []):
        # Reject any notification that doesn't carry our secret — prevents spoofing.
        if notification.get("clientState") != settings.GRAPH_WEBHOOK_SECRET:
            continue
        if notification.get("changeType") == "created":
            # Enqueue on the Celery worker — poll_email_inbox makes synchronous
            # Graph API calls and must never run inline on the API's event
            # loop, or it blocks every other request (including health
            # checks) for the duration of each Graph call.
            from app.tasks.tasks import poll_email_inbox
            poll_email_inbox.delay()
            break  # One poll covers all notifications in this batch

    # Graph requires 202 — any other status triggers an automatic retry storm.
    return Response(status_code=202)
