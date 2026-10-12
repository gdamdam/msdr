# Architecture

How msdr is put together, for anyone reading or changing the code. For using msdr, see the [user guide](user-guide.md).

## Overview

msdr is a single Python process that sits next to SDRplay's **SDRconnect**. SDRconnect owns the radio hardware; msdr talks to it over SDRconnect's WebSocket API, adds memory, decoding, surveying and recording on top, and serves a browser UI.

```
 SDRconnect ──WebSocket──┐                       ┌── browser UI (Lit, no build step)
   (port 5454)           │                       │      REST + /ws push + audio sockets
                         ▼                       │
               ┌──────────────────── msdr process ────────────────────┐
               │  radio adapter ─► engine ─► AppService ─► FastAPI app │
               │  IQ / audio / spectrum streams ─► decoders, survey,   │
               │  recorder                                             │
               │  Wi-Fi / BLE helper processes ─► device tables        │
               │  SQLite (msdr.db, history.db) · settings.json         │
               └───────────────────────────────────────────────────────┘
```

Everything runs on one asyncio event loop. CPU-heavy numpy work (channelizers, raw-IQ decoders, analysis) runs off the loop with `asyncio.to_thread`, and external tools run as subprocesses.

## Startup

`msdr` (the console script) runs `msdr.api.main:main`:

1. Parse the command line (`--host`, `--port`, `--web-port`, `--db`, `--settings`, `--no-browser`, `--debug`).
2. Load `Settings` from `settings.json` (default `~/Library/Application Support/msdr/settings.json`).
3. `create_app()` builds the FastAPI app and one `AppService`.
4. uvicorn serves the app on `web_port` (default 8765) and a browser tab opens.

`AppService.start()` then connects the database, runs migrations, restores history, starts the radio adapter, and starts each optional runtime (recorder, decoders, survey, beacons, Wi-Fi, BLE, history, Remote ID, rules, integrations) according to `settings.features` and the per-feature settings.

## Layers

### Radio boundary: `msdr/radio`

`RadioAdapter` (`protocol.py`) is the only interface the rest of the code uses to reach a receiver. It exposes property get/set, connection state and an event stream (`PropertyChanged`, `ConnectionChanged`, `SpectrumFrame`).

- `sdrconnect.py`: the real adapter, a WebSocket client for SDRconnect (GUI or headless).
- `fake_server.py`: a fake SDRconnect used by tests and `scripts/dev_fake.py`.

SDRconnect delivers bulk data on separate WebSocket connections so it cannot starve control traffic:

| Stream | Module | Used by |
|---|---|---|
| Control + spectrum | `radio/sdrconnect.py` | engine, scanner, survey |
| Spectrum fan-out | `survey/spectrum_hub.py` | shares one spectrum stream between scanner and survey |
| IQ | `recorder/iq_stream.py` | recorder, band decoder, analyzer, raw-IQ decoders |
| Demodulated audio | `decoder/audio_stream.py` | audio decoders |

Notes on the SDRconnect protocol are in [research/2026-09-22-sdrconnect-websocket-api.md](research/2026-09-22-sdrconnect-websocket-api.md).

### Receiver engine: `msdr/engine`

Keeps track of what the receiver is doing and drives it to where msdr wants it.

- `state.py`, `confirmed.py`: the receiver state model and the last state SDRconnect has confirmed.
- `restore.py`: diffs a desired state against the confirmed one and applies the steps one at a time, confirming each. This is how recalling a memory or profile works, and it copes with dropped echoes and someone changing SDRconnect's own GUI meanwhile.
- `dwell.py`: notices when the operator settles on a signal in SDRconnect and records it in the journal.
- `scanner.py`: channel, range and spectrum scanning, holding on signals above an SNR threshold.
- `lna_auto.py`: automatic LNA selection by band, with backoff on overload.
- `workbench.py`: live inspection and spectrum-watch lifecycle.

### Application service: `msdr/api/service.py` and `msdr/api/services/`

`AppService` is the core of the app and has no web-framework code. It owns the adapter, the database, the engine objects and every runtime, and exposes plain async methods.

Each feature area is a mixin in `msdr/api/services/` (`listen`, `library`, `catalog`, `decoders`, `survey`, `scanner`, `recorder`, `review`, `wifi`, `ble`, `history`, `remoteid`, `rules`, `sky`, `allocations`, and others). `AppService` combines them; shared attributes are set up in `AppService.__init__`.

`Broadcaster` pushes status messages to connected browsers. Each browser gets its own bounded queue; a client that falls behind has older messages of the same type dropped, and one that stays behind is disconnected.

### Web front end: `msdr/api/app.py`

The only module that imports FastAPI, Starlette or uvicorn. It maps REST routes onto `AppService` methods, defines request/response models (`schemas.py`), and provides three WebSockets:

- `/ws`: status and event push to the UI.
- `/ws/audio`, `/ws/live-audio`: audio for playback in the browser.

It also serves the static UI from `msdr/ui/`.

### UI: `msdr/ui`

Plain ES modules with [Lit](https://lit.dev) web components and Leaflet for maps, both vendored in `msdr/ui/vendor/`. There is no build step: edit a file and reload. `api.js` wraps the REST calls, `app.js` is the shell, and `components/` holds one file per panel or widget. Pure formatting and data helpers (`format.js`, `map-data.js`, …) are tested from Python in `tests/ui/`.

## Feature areas

### Library, catalog and journal

- `library/`: saved signals (memories), tags and receiver profiles.
- `catalog/`: searchable frequency schedules (EiBi, Aoki, HFCC, imports) and reference catalogs. See [catalogs.md](catalogs.md) and [reference-catalogs.md](reference-catalogs.md).
- `allocations/`: the regulatory band allocation tables. See [allocations.md](allocations.md).
- `providers/`: offline signal identification against bundled band plans, plus satellite pass prediction (skyfield).
- `journal/`: a log of receiver actions and "heard again" events, linked to saved signals.
- `observations/`: sessions, observations and their supporting claims and recordings.

### Decoding: `msdr/decoder`

`registry.py` is a data-only list of decoders: what input each needs (audio or raw IQ), the sample rate and bandwidth, and which binary, if any, it depends on. `manager.py` starts and stops decoders as the tuned signal changes.

Most decoders read SDRconnect's demodulated audio. Decoders marked `iq_only` read the IQ stream instead and demodulate it inside msdr. There are four kinds of backend:

| Kind | Decoders |
|---|---|
| External program (`backends.py`) | multimon-ng (POCSAG, FLEX, EAS, APRS, 9600 baud packet, DTMF, CW), minimodem (RTTY), direwolf (300 baud packet), dsd-fme (DMR, P25, NXDN, D-STAR, YSF, dPMR, M17), jt9 (FT8) |
| In-process Python (`pydecoders/`) | RTTY, SSTV, WEFAX, NAVTEX, WWV/DCF77/MSF time codes, AIS |
| Native IQ receivers (`pydecoders/`, `iq_backend.py`, `iq_session.py`) | ADS-B, LoRa, RS41, DSC, End-of-Train, VOR, ILS |
| Band decode (`channelizer.py`, `band.py`) | many NFM channels decoded at once from one wideband IQ stream |

A missing binary makes that decoder unavailable without breaking startup. Per-decoder notes are linked from the [README](../README.md#decoders).

### Survey and scanning: `msdr/survey`

Spectrum, waterfall and occupancy for the current span (`survey.py`), band scans that stitch several spans together (`bandscan.py`), signal classification on IQ (`analyzer.py`, `classify.py`), RDS and CW, and beacon monitoring (`beacons.py`).

### Recording and replay: `msdr/recorder`, `msdr/source`

- `recorder/`: a rolling pre-roll IQ buffer on disk. Pressing REC, or an activity trigger, turns it into a SigMF recording.
- `source/`: one `IQSource` interface for both the live radio (`live.py`) and a recording on a timeline. `VirtualReceiver` demodulates a channel from either, so Review can listen to and decode a recording the same way as live. See [raw-iq-replay.md](raw-iq-replay.md).

### Passive Wi-Fi, BLE and Remote ID

These receivers are independent of SDRconnect and never import `msdr.radio`, `msdr.api` or `msdr.recorder`.

- `wifi/`: `WifiSupervisor` runs the external `msdr-wifi-capture` helper and parses its pcap output (radiotap, 802.11 management frames) into a table of networks. See [wifi.md](wifi.md).
- `ble/`: `BleSupervisor` runs a capture subprocess for a raw HCI dongle or a Sniffle dongle and parses advertisements. See [ble.md](ble.md).
- `remoteid/`: decodes ASTM F3411 drone Remote ID from the Wi-Fi and BLE frames above and tracks each aircraft. See [remote-id.md](remote-id.md).
- `history/`: per-minute device history for Wi-Fi and BLE, in its own `history.db`. See [history.md](history.md).
- `rules/`: watch rules (beacon presence, sensor thresholds, drone geofences) evaluated on a timer. See [rules.md](rules.md).

### External applications: `msdr/integrations`

Status and received traffic from JS8Call, Pat (Winlink) and FreeDATA running alongside msdr. See [integrations.md](integrations.md).

### Validation: `msdr/validation`, `msdr/contracts`

- `validation/`: a synthetic signal corpus run through the real decoders to produce a scorecard (`scripts/scorecard.py` regenerates [validation/scorecard-synthetic.md](validation/scorecard-synthetic.md)).
- `decoder/capability_matrix.py`: per-decoder validation status, including off-air results.
- `contracts/`: small shared types for sources, evidence and readiness.

## Storage

| Data | Location |
|---|---|
| Settings | `settings.json` (pydantic `Settings` in `msdr/settings.py`) |
| Main database | `msdr.db`, SQLite, one connection owned by the event loop; numbered migrations in `msdr/storage/migrations/` |
| Device history | `history.db` next to `msdr.db`, own migrations in `msdr/history/migrations/` |
| Recordings | under the recorder root: `preroll/` (disposable) and `recordings/` (SigMF, kept) |

Both default to `~/Library/Application Support/msdr/`. `storage/backup.py` makes versioned full-database backups and evidence bundles. `storage/transfer.py` exports and merges the library (signals and profiles) between machines.

## Development

```sh
.venv/bin/python scripts/dev_fake.py   # fake SDRconnect on port 5455
.venv/bin/msdr --port 5455
```

Tests live in `tests/`, one directory per package. `tests/e2e/` drives the real UI in a browser. Other useful scripts in `scripts/`:

- `soak.py`: long-running stability check.
- `setup_lora_runtime.sh`, `validate_lora_iq.py`, `make_lora_vectors.py`: the optional gr-lora_sdr runtime and LoRa test vectors.
