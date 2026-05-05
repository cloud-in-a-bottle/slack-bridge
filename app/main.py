"""HTTP responder for Slack slash commands that map to OpenHost URLs.

Currently exposes a single command:

    /jitsi <room>

which returns an in-channel message containing
``https://<jitsi-host>/<room>`` so anyone in the channel can click
through to a Jitsi meeting on this zone.  Jitsi creates rooms
on-demand the first time someone joins the URL, so no allocation
or pre-registration is needed on the Jitsi side.

Auth: Slack signs every slash-command request with HMAC-SHA256 over
the request body, using the app's signing secret.  The signing
secret is read from
``$OPENHOST_APP_DATA_DIR/slack_signing_secret.txt`` (one line, no
quotes, no trailing newline) at request time so the operator can
rotate it without a redeploy.  If the file is missing or empty the
endpoint returns 500 with a clear pointer back to the README — we
fail closed rather than silently accept unsigned requests.

Other endpoints:

* ``GET /health``  — liveness probe (no auth, returns ``ok``).
* ``GET /``        — operator-readable page that explains how to
                     wire the slash command up in Slack.  This is
                     visited by humans configuring the app; Slack
                     itself only POSTs to ``/slack/jitsi``.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
from typing import Final

from flask import Flask, Response, jsonify, request

# ---------------------------------------------------------------------
# Constants and configuration
# ---------------------------------------------------------------------

# Where Slack will POST the slash command.  Wired into the Slack
# app's "Slash Commands > Request URL" field as
# ``https://<this-app's-public-url>/slack/jitsi``.
JITSI_PATH: Final = "/slack/jitsi"

# Path inside the container where compute_space mounts our
# persistent data dir.  See openhost.toml: [data] app_data = true.
APP_DATA_DIR: Final = os.environ.get(
    "OPENHOST_APP_DATA_DIR", "/data/app_data/slack-bridge"
)

# File the operator drops the Slack signing secret into.  See the
# README for setup instructions.  We re-read on every request so
# that rotating the secret is "edit a file, no restart" — at the
# cost of a sub-millisecond stat() per request, which is fine.
SIGNING_SECRET_PATH: Final = os.path.join(APP_DATA_DIR, "slack_signing_secret.txt")

# Optional override for the Jitsi host.  In normal operation the
# app deduces the Jitsi URL from the zone domain that compute_space
# advertises in OPENHOST_ZONE_DOMAIN, so the operator does not have
# to set this.  Useful if Jitsi lives on a non-default subdomain.
JITSI_BASE_URL: Final = os.environ.get(
    "JITSI_BASE_URL",
    f"https://jitsi.{os.environ.get('OPENHOST_ZONE_DOMAIN', 'localhost')}",
)

# Slack's signature design: requests older than this many seconds
# are rejected.  This bounds replay attacks, since once an attacker
# has captured a signed request, the window in which they can
# replay it is limited.  Slack's docs recommend 5 minutes.
REPLAY_WINDOW_SEC: Final = 60 * 5

# Sanitise the room name before pasting it into a URL.  Jitsi
# handles arbitrary URL-encoded UTF-8 fine, but a stricter sanitise
# protects us from anything weird the user might type and prevents
# the URL from being used to inject extra path segments.  Keep
# alphanumerics, dash, underscore; map everything else (including
# spaces) to a single dash; collapse consecutive dashes; lowercase.
_SANITIZE_RE: Final = re.compile(r"[^a-z0-9_-]+")
_DEDUP_DASH_RE: Final = re.compile(r"-{2,}")
_MAX_ROOM_LEN: Final = 64

# ---------------------------------------------------------------------
# App
# ---------------------------------------------------------------------

app = Flask(__name__)


def _sanitize_room(raw: str) -> str:
    """Normalise the user-typed room name into a URL-safe slug.

    Behaviour:
      * lowercased
      * all runs of disallowed chars become a single ``-``
      * leading/trailing dashes are stripped
      * truncated at 64 chars (Jitsi accepts more, but operators
        appreciate predictably short links)
      * if the result is empty the caller should treat that as a
        usage error and not call this function's output verbatim
    """
    s = _SANITIZE_RE.sub("-", raw.strip().lower())
    s = _DEDUP_DASH_RE.sub("-", s).strip("-")
    return s[:_MAX_ROOM_LEN]


def _read_signing_secret() -> str | None:
    """Return the Slack signing secret as configured by the operator.

    Returns ``None`` when the secret file is absent or empty so that
    callers can decide whether to log + 500 (production) or warn
    (test).  Re-read on every call; the cost is a single open()
    on a one-line file.
    """
    try:
        with open(SIGNING_SECRET_PATH, encoding="utf-8") as fh:
            secret = fh.read().strip()
    except FileNotFoundError:
        return None
    return secret or None


def _verify_slack_signature(req) -> bool:
    """Validate the X-Slack-Signature header on this request.

    Slack signs ``v0:<timestamp>:<raw-body>`` with HMAC-SHA256
    using the app's signing secret and sends the result as
    ``v0=<hex>`` in ``X-Slack-Signature``.  We:
      1. Ensure the timestamp is within REPLAY_WINDOW_SEC of now.
      2. Recompute the signature using ``compare_digest`` to avoid
         a timing oracle leak.
    Returns True iff the request is authentically from Slack.
    """
    secret = _read_signing_secret()
    if not secret:
        return False

    timestamp = req.headers.get("X-Slack-Request-Timestamp", "")
    signature = req.headers.get("X-Slack-Signature", "")
    if not timestamp or not signature:
        return False

    try:
        ts_int = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - ts_int) > REPLAY_WINDOW_SEC:
        return False

    body = req.get_data(as_text=True)
    base = f"v0:{timestamp}:{body}"
    expected = "v0=" + hmac.new(
        secret.encode("utf-8"), base.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------


@app.get("/health")
def health() -> Response:
    """Liveness probe used by OpenHost's [routing] health_check."""
    return Response("ok\n", mimetype="text/plain")


@app.get("/")
def index() -> Response:
    """Human-readable setup hint.  Visited only by operators."""
    body = (
        "openhost-slack-bridge\n"
        "=====================\n\n"
        "This app responds to Slack slash commands by returning URLs that\n"
        "point at resources on the OpenHost zone hosting it.\n\n"
        "Currently registered command: /jitsi <room>\n"
        f"Slack request URL:           https://<this-app>{JITSI_PATH}\n\n"
        "Setup:\n"
        "  1. Create a Slack app at https://api.slack.com/apps.\n"
        "  2. Add a slash command /jitsi pointing at the URL above.\n"
        f"  3. Drop the app's Signing Secret into\n"
        f"     {SIGNING_SECRET_PATH}\n"
        "     (one line, no quotes, no trailing newline).\n"
        "  4. Install the Slack app to your workspace.\n\n"
        "See README.md in the repo for full instructions.\n"
    )
    return Response(body, mimetype="text/plain")


@app.post(JITSI_PATH)
def jitsi() -> tuple[Response, int] | Response:
    """Handle Slack's /jitsi <room> command.

    Returns an in-channel message containing the Jitsi URL.  An
    empty room name (just ``/jitsi``) returns an ephemeral usage
    hint that only the typing user sees.
    """
    if not _verify_slack_signature(request):
        # Distinguish "secret not configured" from "bad signature"
        # in the log (helps operators debug), but return the same
        # status to clients to avoid leaking the difference.
        if _read_signing_secret() is None:
            app.logger.error(
                "Slack signing secret missing at %s; refusing request",
                SIGNING_SECRET_PATH,
            )
        else:
            app.logger.warning("Rejected request with bad Slack signature")
        return jsonify({"error": "unauthorized"}), 401

    raw_room = request.form.get("text", "")
    room = _sanitize_room(raw_room)
    if not room:
        # Empty / all-disallowed-chars input.  Return an ephemeral
        # usage hint so the user can correct it without spamming
        # the channel.
        return jsonify(
            {
                "response_type": "ephemeral",
                "text": "Usage: /jitsi <room>",
            }
        )

    url = f"{JITSI_BASE_URL}/{room}"
    return jsonify(
        {
            "response_type": "in_channel",
            "text": f"Jitsi room: {url}",
        }
    )
