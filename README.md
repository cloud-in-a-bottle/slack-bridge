# openhost-slack-bridge

A tiny HTTP service for Slack slash commands that resolve to URLs on
the OpenHost zone hosting it.

Currently exposes one command:

* `/jitsi <room>` → posts an in-channel message with the Jitsi URL
  for the given room. Jitsi creates rooms on first join, so no
  pre-allocation is needed.

The service is stateless apart from a single operator-supplied
secret (the Slack app's signing secret), which is held in the
app's persistent data directory.

## Why a separate app

This could have lived inside `openhost-jitsi`, but the lifecycles
are unrelated and the routing surface inside that container is
already busy. Keeping it standalone means:

* Iterating on the bridge does not require restarting Jitsi.
* The bridge can grow to handle more slash commands (mirotalk,
  peertube quick-links, etc.) without coupling them to any single
  upstream app.
* The container is one Python process; the security surface is
  exactly Slack's HMAC signature check plus the URL-sanitiser.

## Setup

### 1. Deploy the app to your OpenHost zone

Using the OpenHost dashboard or CLI, install this repo as an app.
Once it's running, the public URL is whatever your zone's router
allocates for it (typically `https://<zone-domain>/slack-bridge`).

### 2. Create a Slack app

1. Go to <https://api.slack.com/apps> → **Create New App** → **From scratch**.
2. Pick your workspace.
3. Open **Basic Information** → copy the **Signing Secret** and
   keep it in a temporary buffer; you will paste it into the app's
   data dir in the next step.
4. Open **Slash Commands** → **Create New Command**:
   * **Command**: `/jitsi`
   * **Request URL**: `https://<your-zone>/slack-bridge/slack/jitsi`
     (substitute the actual hostname/path of your deployment).
   * **Short description**: "Get a Jitsi meeting link"
   * **Usage hint**: `[room name]`
5. **Install to Workspace**.

### 3. Drop the signing secret into the app's data dir

The signing secret is read from
`$OPENHOST_APP_DATA_DIR/slack_signing_secret.txt` on every request,
so rotating it is "edit a file, no restart." On the OpenHost host:

```sh
echo -n 'paste-the-signing-secret-here' \
  > <OPENHOST_DATA_ROOT>/persistent_data/app_data/slack-bridge/slack_signing_secret.txt
```

Make sure there is no trailing newline. If the file is missing,
empty, or unreadable the app rejects every request with HTTP 401.

That is the entire setup. Type `/jitsi standup` in any Slack
channel; the bot replies with the Jitsi URL for room `standup`.

## How it works

```
Slack workspace
       │  (POST with HMAC-signed body)
       ▼
  /slack/jitsi  (this app)
       │  verify signature, sanitise room name
       ▼
  reply 200 with JSON:
    { response_type: "in_channel",
      text: "Jitsi room: https://jitsi.<zone>/<room>" }
       │
       ▼
  Slack posts the message.
  Anyone clicks the link → Jitsi creates the room on join.
```

The request signature is checked using `hmac.compare_digest` over
`v0:<timestamp>:<raw-body>` per Slack's
[documented protocol](https://api.slack.com/authentication/verifying-requests-from-slack).
Requests older than five minutes are rejected to bound replay.
The room name is lowercased, stripped of non-alphanumeric
characters (replaced with dashes), de-duplicated of consecutive
dashes, and truncated at 64 characters.

## Configuration

| Env var                | Purpose                                                                                                | Default                                    |
| ---------------------- | ------------------------------------------------------------------------------------------------------ | ------------------------------------------ |
| `OPENHOST_APP_DATA_DIR`| Persistent data dir; injected by compute_space at container start.                                     | `/data/app_data/slack-bridge`              |
| `OPENHOST_ZONE_DOMAIN` | Zone domain; injected by compute_space. Used to compose the default Jitsi URL.                        | `localhost`                                |
| `JITSI_BASE_URL`       | Override for the full Jitsi base URL. Set this if Jitsi is on a non-default subdomain or path.        | `https://jitsi.<OPENHOST_ZONE_DOMAIN>`     |

## Endpoints

* `POST /slack/jitsi` — Slack slash-command handler. Requires a
  valid Slack signature.
* `GET /health` — Liveness probe used by OpenHost's
  `[routing] health_check`. Always returns `ok`.
* `GET /` — Plain-text setup hint for humans.

## Adding more slash commands

Add another `@app.post(...)` route in `app/main.py` with the same
signature-verification call and pattern. The HMAC verifier and
sanitiser helpers are shared. Then register the new command in the
Slack app config.
