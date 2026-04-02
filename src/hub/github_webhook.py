# src/hub/github_webhook.py
"""GitHub webhook handler -- receives PR events and routes to dev-team channel."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import uuid

from a2a.types import (
    Message as A2AMessage,
    MessageSendParams,
    Part,
    Role,
    TextPart,
)
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger("hub.github_webhook")


def create_github_webhook_handler(hub):
    """Create GitHub webhook handler with access to hub for message routing.

    Uses factory pattern (same as telegram_webhook) so the inner function
    has a closure reference to the hub instance for on_message_send calls.
    """

    async def github_webhook(request: Request) -> JSONResponse:
        """Receive GitHub webhook events and route to dev-team channel."""

        # 1. Verify HMAC-SHA256 signature
        secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
        if not secret:
            logger.error("GITHUB_WEBHOOK_SECRET not configured")
            return JSONResponse({"error": "Webhook not configured"}, status_code=503)

        signature = request.headers.get("X-Hub-Signature-256")
        if not signature:
            logger.warning(
                "GitHub webhook request missing X-Hub-Signature-256 header (source: %s)",
                request.client.host if request.client else "unknown",
            )
            return JSONResponse({"error": "Missing signature"}, status_code=401)

        body = await request.body()

        expected = "sha256=" + hmac.new(
            secret.encode(), body, hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(signature, expected):
            logger.warning(
                "Invalid GitHub webhook signature (source: %s)",
                request.client.host if request.client else "unknown",
            )
            return JSONResponse({"error": "Invalid signature"}, status_code=403)

        # 2. Parse event type and payload
        event_type = request.headers.get("X-GitHub-Event", "")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            logger.warning(
                "GitHub webhook received invalid JSON (event=%s, body_len=%d): %s",
                event_type, len(body), exc,
            )
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)

        logger.info("GitHub webhook: event=%s", event_type)

        # 3. Handle ping event (GitHub sends this on webhook creation)
        if event_type == "ping":
            return JSONResponse({"ok": True, "msg": "pong"})

        # 4. Route PR events to dev-team channel
        if event_type == "pull_request":
            action = payload.get("action", "")
            if action in ("opened", "synchronize", "reopened"):
                pr = payload.get("pull_request", {})
                pr_number = pr.get("number", "?")
                pr_title = pr.get("title", "Untitled")
                pr_author = pr.get("user", {}).get("login", "unknown")
                pr_url = pr.get("html_url", "")
                pr_base = pr.get("base", {}).get("ref", "")
                pr_head = pr.get("head", {}).get("ref", "")

                message_text = (
                    f"[GitHub PR #{pr_number}] {pr_title}\n"
                    f"Action: {action}\n"
                    f"Author: {pr_author}\n"
                    f"Branch: {pr_head} -> {pr_base}\n"
                    f"URL: {pr_url}\n"
                    f"@rex Please review this PR."
                )

                try:
                    msg = A2AMessage(
                        role=Role.user,
                        parts=[Part(root=TextPart(text=message_text))],
                        messageId=str(uuid.uuid4()),
                        metadata={
                            "channel_id": "dev-team",
                            # sender_id omitted -- external webhook bypasses member check
                            "source": "github-webhook",
                        },
                    )
                    params = MessageSendParams(message=msg)
                    result = await hub.on_message_send(params)

                    # Check if hub returned an error Message instead of a Task
                    if isinstance(result, A2AMessage):
                        error_text = ""
                        if result.parts:
                            part = result.parts[0]
                            if hasattr(part, "root") and hasattr(part.root, "text"):
                                error_text = part.root.text
                        logger.error(
                            "Hub rejected PR #%s routing: %s", pr_number, error_text
                        )
                        return JSONResponse(
                            {"error": f"Hub rejected: {error_text}"}, status_code=502
                        )

                    logger.info(
                        "Routed PR #%s (%s) to dev-team", pr_number, action
                    )
                except Exception:
                    logger.exception(
                        "Failed to route PR #%s event to dev-team", pr_number
                    )
                    return JSONResponse(
                        {"error": "Routing failed"}, status_code=500
                    )
            else:
                logger.debug(
                    "Ignoring PR action=%s for PR #%s",
                    action, payload.get("pull_request", {}).get("number", "?"),
                )

        # 5. Acknowledge all other events
        return JSONResponse({"ok": True})

    return github_webhook
