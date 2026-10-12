# External digital-mode integrations

msdr can show messages from JS8Call, Pat (Winlink) and FreeDATA when those applications are already running on the same machine. This page is for users enabling them under **Setup → Integrations**, and explains what msdr reads and what it never does.

Each connection is opt-in and disabled in a new settings file. The integrations only read messages and status:

- msdr sends no JS8Call `TX.*` commands.
- msdr makes no Pat or FreeDATA `POST`, `PUT`, `PATCH` or `DELETE` requests.
- Each received item is labeled with the external application that supplied it. msdr does not decode the radio audio itself.
- If an application is unavailable or returns a connection error, the status shows it and the SDR connection is unaffected.

## JS8Call

1. Enable JS8Call's TCP Network Message API in its Reporting settings.
2. Enable the JS8Call receive feed under **Setup → Integrations**.

The default endpoint is `127.0.0.1:2242`. JS8Call allows changing the API port. msdr listens to the JSON packets JS8Call emits and shows non-empty decoded text from:

| Packet | Text source |
|---|---|
| `RX.DIRECTED` | `params.TEXT`, falling back to `value` |
| `RX.ACTIVITY` | `value` |

Any reported sender, recipient, dial/RF frequency, SNR, grid square and event time is shown too. Connection failures are shown and retried automatically.

The implementation follows the upstream [JS8Call Network Message API](https://js8call.com/JS8Call-improved/d7/d15/md_docs_2API.html) documentation, which lists the default TCP port and the `RX.*` receive packet fields. That API is documented as JSON over TCP and UDP; msdr uses only the TCP receive stream.

## Pat / Winlink

1. Start Pat's built-in HTTP interface (`pat http`).
2. Enable **Pat / Winlink** under **Setup → Integrations**.

The default endpoint is `http://127.0.0.1:8080`. msdr refreshes Pat every 10 seconds and keeps at most 200 normalized receive records in memory. The UI includes a link to open a received item in Pat.

| Route | Purpose |
|---|---|
| `GET /api/status` | Connection check |
| `GET /api/mailbox/in` | Recent inbound items |
| `GET /api/mailbox/in/{MID}` | Detail of each recent item |

msdr never calls Pat's connect/send, message-delete, mark-read, compose or position-report routes.

Pat does not publish a versioned local JSON API in its user guide. Compatibility is therefore based on the current upstream route and response shape, not on a guaranteed minimum Pat release. A Pat update that changes those routes or JSON fields may require an msdr update.

Sources:

- [Pat API routes](https://github.com/la5nta/pat/blob/master/api/api.go) and [Pat mailbox handlers](https://github.com/la5nta/pat/blob/master/api/mailbox.go): register these GET routes and serialize the mailbox list and detail as JSON.
- [Pat Web GUI](https://github.com/la5nta/pat/wiki/The-web-GUI): documents the HTTP service as its Web GUI server, bound to localhost by default.

## FreeDATA

1. Start the FreeDATA server.
2. Enable **FreeDATA** under **Setup → Integrations**.

The default endpoint is `http://127.0.0.1:5000`; change the URL if the server listens elsewhere. msdr verifies the API root and polls every 10 seconds:

| Route | Purpose |
|---|---|
| `GET /modem/state` | Modem state |
| `GET /freedata/messages` | Received text, sender, destination, unread state, file names when the message has attachments |
| `GET /freedata/messages/{id}/attachments` | File metadata, fetched only for a newly seen message |

- File contents stay in FreeDATA. msdr does not download them or control the modem.
- The status card distinguishes an accessible API from a running modem and a connected radio.
- Messages are a bounded recent feed held in memory. The FreeDATA app remains the source of truth.

Paths and response fields come from the upstream [FreeDATA server routes](https://github.com/DJ2LS/FreeDATA/blob/main/freedata_server/server.py), [message API](https://github.com/DJ2LS/FreeDATA/blob/main/freedata_server/api/freedata.py) and [modem state API](https://github.com/DJ2LS/FreeDATA/blob/main/freedata_server/api/modem.py). FreeDATA documents the local REST interface in its [API guide](https://wiki.freedata.app/en/rest-api-redoc).

## Testing status

Protocol handling for all three is tested against deterministic local TCP/HTTP test servers. Those tests cover packet parsing and HTTP response handling only. They do not simulate RF, show off-air decoding performance, or check behavior against installed JS8Call, Pat or FreeDATA binaries. No JS8Call version has been tested against a real running application.

See also: [decoder expansion](decoder-expansion.md), [receiver audio and DSP](receiver-dsp.md).
