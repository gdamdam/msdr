# External digital-mode integrations

How msdr links to already-running JS8Call, Pat and FreeDATA applications, and what it does and does not do with them. For users enabling these under **Setup → Integrations**.

Each connection is opt-in and disabled in a new settings file. The integrations are receive and status readers:

- msdr sends no JS8Call `TX.*` commands and makes no Pat or FreeDATA `POST`, `PUT`, `PATCH` or `DELETE` requests.
- A received item is labeled with the external application that supplied it; the UI does not claim to decode radio audio itself.

## JS8Call

1. Enable JS8Call's TCP Network Message API in its Reporting settings.
2. Enable the JS8Call receive feed under **Setup → Integrations**.

The default endpoint is `127.0.0.1:2242`; JS8Call allows changing the API port. msdr listens to the JSON packets JS8Call emits and displays non-empty decoded text from:

| Packet | Text source |
|---|---|
| `RX.DIRECTED` | `params.TEXT`, falling back to `value` |
| `RX.ACTIVITY` | `value` |

Any reported sender, recipient, dial/RF frequency, SNR, grid square and event time is shown too. Connection failures are shown and retried automatically.

Basis and limits:

- The implementation follows the current upstream [JS8Call Network Message API](https://js8call.com/JS8Call-improved/d7/d15/md_docs_2API.html) documentation, which lists the default TCP port and the `RX.*` receive packet fields.
- The documented API calls this a JSON API over TCP and UDP; this integration uses only the TCP receive stream.
- No JS8Call version has been tested against a real running application in this task.

## Pat / Winlink

1. Start Pat's built-in HTTP interface (`pat http`).
2. Enable **Pat / Winlink** under **Setup → Integrations**.

The default endpoint is `http://127.0.0.1:8080`. msdr refreshes Pat every 10 seconds and retains at most 200 normalized receive records in memory.

| Route | Purpose |
|---|---|
| `GET /api/status` | Connection check |
| `GET /api/mailbox/in` | Recent inbound items |
| `GET /api/mailbox/in/{MID}` | Detail of each recent item |

- The current upstream Pat source registers these GET routes and serializes the mailbox list/detail as JSON: [Pat API routes](https://github.com/la5nta/pat/blob/master/api/api.go), [Pat mailbox handlers](https://github.com/la5nta/pat/blob/master/api/mailbox.go).
- Pat's wiki documents the HTTP service as its Web GUI server and says it binds to localhost by default: [Pat Web GUI](https://github.com/la5nta/pat/wiki/The-web-GUI).
- Pat does not publish a versioned local JSON API contract in its user guide. Compatibility is therefore based on the current upstream route and response shape, not on a guaranteed minimum Pat release. A Pat update that changes those source routes or JSON fields may require an msdr update.
- The integration never calls Pat's connect/send, message-delete, mark-read, compose or position-report routes.
- The UI includes a link to open a received item in Pat.

## FreeDATA

1. Start the FreeDATA server.
2. Enable **FreeDATA** under **Setup → Integrations**.

The default endpoint is `http://127.0.0.1:5000`; change the URL if the server listens elsewhere. msdr verifies the API root and polls every 10 seconds:

| Route | Purpose |
|---|---|
| `GET /modem/state` | Modem state |
| `GET /freedata/messages` | Received text, sender, destination, unread state, file names when the message has attachments |
| `GET /freedata/messages/{id}/attachments` | File metadata, fetched only for a newly seen message |

- File contents stay in FreeDATA; msdr does not download them or control the modem.
- The status card distinguishes an accessible API from a running modem and connected radio.
- Messages are a bounded recent in-memory feed in msdr; the FreeDATA app remains the source of truth.
- Paths and response fields come from the upstream [FreeDATA server routes](https://github.com/DJ2LS/FreeDATA/blob/main/freedata_server/server.py), [message API](https://github.com/DJ2LS/FreeDATA/blob/main/freedata_server/api/freedata.py) and [modem state API](https://github.com/DJ2LS/FreeDATA/blob/main/freedata_server/api/modem.py). FreeDATA documents the local REST interface at its [API guide](https://wiki.freedata.app/en/rest-api-redoc).

## Scope and verification

- All three integrations can report unavailable/connection-error states without affecting the SDR connection.
- Their external-process protocol handling is verified with deterministic local TCP/HTTP test servers.
- Those tests validate packet parsing and HTTP response handling only; they do not simulate RF, prove off-air decoding performance, or verify behavior against installed JS8Call, Pat or FreeDATA binaries.
- Mailbox content is labeled with the source app.

See also: [decoder expansion](decoder-expansion.md), [receiver audio and DSP](receiver-dsp.md).
