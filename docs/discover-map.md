# Discover map and table

The **Map** and **Table** views show where decoded signals were reported. They are for investigating reception. They are not an aircraft navigation system, and they do not estimate the location of unidentified signals.

## Using the map

- **Map** is a top-level workspace between Discover and Review. Discover keeps Spectrum / survey and Table.
- Map and Table keep the selected item as you switch between them.
- Select a marker or table entry to read the original decoded event.
- Saved station and catalog locations offer **Tune** when live receiver control is available. Replay does not retune hardware.
- **Clear map** hides the current points locally. New reports can still appear.
- **Refresh map** reloads decoded events, saved stations and catalog locations.

The view uses the latest 500 decoder events already loaded by msdr, saved stations with coordinates, and up to 500 located catalog entries below 30 MHz.

When your receiver location is set, Table and the selected-item details also show distance and true bearing from it.

## What gets a marker

| Layer | What gets a marker |
|---|---|
| Aircraft | ADS-B/Mode S reports are expanded into individual entries. Only usable reported positions get markers; non-position messages stay in Table. ACARS events with structured positions use the same layer. |
| Radiosondes | RS41 ECEF positions, converted to WGS84 coordinates and ellipsoidal altitude. Payloads that fail the integrity check never get markers. |
| Amateur | APRS uncompressed `!`, `=`, `/` and `@` position reports, shown with packet evidence. Compressed, Mic-E, object/item and ambiguous positions stay unpositioned unless the decoder supplies explicit numeric coordinates. |
| Maritime | AIS positions, and DSC events that carry structured coordinates. DSC events without decoded coordinates stay unpositioned in Table. |
| Stations / HF | Memory/repeater and catalog coordinates that were explicitly supplied. These are reference locations, not proof that a station was received. No location is inferred from a frequency, country, station name or ambiguous place code. |

Missing coordinates and failed checks never become positions. The original decoder fields, timestamp, frequency and integrity result stay available in the selection detail.

### Age, tracks and glow

- Markers fade after 15 minutes, or when the position age is unknown.
- Fresh decoded positions glow for two minutes, fading as the fix ages. Saved station and catalog points do not glow.
- The ADS-B BaseStation output has no per-position timestamps. Report age is shown separately from unknown position age, and tracks reflect snapshots of reported positions.
- Tracks connect received fixes only for a known entity and source. They split at long gaps and at the date line.
- Aircraft and vessel emoji rotate when a decoded track, course or heading is present. Unknown direction stays unrotated.

## Adding station locations

1. In Library, edit a saved signal.
2. Enter both latitude and longitude in decimal degrees. Clear both to remove its map location.

Other ways to supply coordinates:

- HF catalog entries need explicit coordinates. Schedule and site-name data alone cannot place a marker.
- User catalogs accept `latitude`/`longitude` in SDR++ JSON bookmarks, or two appended GQRX CSV columns: `frequency;mode;bandwidth;name;tags;latitude;longitude`.
- Saving a catalog entry as a memory lets you enter its location in the regular signal editor.

Existing memories and databases migrate with no coordinates.

## Basemap

- Leaflet 1.9.4 is bundled locally.
- The optional online basemap requests visible OpenStreetMap tiles directly from the browser, with attribution and normal browser caching. msdr does not prefetch or download tiles.
- Turn off **Online basemap** to stop tile requests. Markers, tracks, map controls and Table still work without tiles.

See the [Leaflet guide](https://leafletjs.com/examples/quick-start/) and the [OpenStreetMap tile policy](https://operations.osmfoundation.org/policies/tiles/).

## Satellites

Pass predictions, azimuth/elevation sky tracks and receiver tracking stay in the **Satellites** workspace, not on this map. Geographic satellite ground tracks are not shown.

See also: [Remote ID](remote-id.md), [Watch rules](rules.md), [BLE](ble.md).
