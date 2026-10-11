# Discover Map and Table (0.33.0)

For operators investigating where decoded signals were reported. It is a reception investigation view, not an aircraft navigation system or a geolocation estimate for unidentified RF signals.

- Map is a top-level workspace between Discover and Review. Discover keeps Spectrum / survey and Table.
- Map and Table retain selected evidence as you switch.
- Select a marker or table entry to read the original decoded event.
- Saved station and catalog locations offer Tune when live receiver control is available; replay does not retune hardware.

The view uses the latest 500 decoder events already loaded by msdr, saved stations with coordinates, and up to 500 located catalog entries below 30 MHz.

## Positions and evidence

| Layer | What gets a marker |
|---|---|
| Aircraft | ADS-B/Mode S reports are expanded into individual entries. Only usable reported positions get markers; non-position messages remain in Table. ACARS events with structured positions use the same layer. |
| Radiosondes | RS41 ECEF positions are converted to WGS84 coordinates and ellipsoidal altitude. Failed-integrity payloads never get markers. |
| Amateur | APRS uncompressed `!`, `=`, `/` and `@` position reports, shown with packet evidence. Compressed, Mic-E, object/item and ambiguous positions remain unpositioned unless the decoder supplies explicit numeric coordinates. |
| Maritime | AIS positions and DSC events carrying structured coordinates share the maritime layer. The standalone DSC implementation currently emits symbol data without semantic position reports; those remain unpositioned in Table. Adding the map does not claim to complete DSC decoding or add an ACARS decoder. |
| Stations / HF | Explicitly supplied memory/repeater and catalog coordinates are reference locations, not proof that a station was received. No location is inferred from a frequency, country, station name or ambiguous place code. |

### Age, tracks and glow

- Markers fade after 15 minutes or when position age is unknown.
- The ADS-B BaseStation output does not expose per-position timestamps: report age is shown separately from unknown position age, and tracks reflect snapshots of reported positions.
- Tracks connect received fixes only for a known entity/source, and split at long gaps and at the date line.
- Missing coordinates and failed checks do not become positions.
- Original decoder fields, timestamp, frequency and integrity remain available in the selection detail.
- Fresh decoded positions glow for two minutes, fading as the fix ages. Saved station and catalog points do not glow.
- Aircraft and vessel emoji rotate when decoded track/course (or heading) is present; unknown direction stays unrotated.

### Buttons

- **Clear map** hides current points locally while new reports can appear.
- **Refresh map** reloads decoded events, saved stations and catalog locations.

## Station setup

Distance and true bearing from the configured receiver location appear in Table and selected details when that location is available.

- Edit a saved signal in Library and supply both latitude and longitude in decimal degrees. Clear both to remove its map location.
- Existing memories and databases migrate with no coordinates.
- HF catalog entries need explicit coordinates; normal schedule/site-name data alone cannot place a marker.
- User catalogs accept `latitude`/`longitude` in SDR++ JSON bookmarks, or appended GQRX CSV columns `frequency;mode;bandwidth;name;tags;latitude;longitude`.
- Saving a catalog entry as a memory allows its location to be entered in the regular signal editor.

## Basemap and satellites

- Leaflet 1.9.4 is bundled locally.
- The optional online basemap requests visible OpenStreetMap tiles directly from the browser with attribution and normal browser caching. There is no tile prefetch/download.
- Turn off Online basemap to stop tile requests; markers, tracks, map controls and Table still work without tiles.
- See the [Leaflet guide](https://leafletjs.com/examples/quick-start/) and [OpenStreetMap tile policy](https://operations.osmfoundation.org/policies/tiles/).

Existing Satellites already provides pass predictions, azimuth/elevation sky tracks and receiver tracking controls. These remain in their dedicated workspace; this release does not duplicate them or import nextpass code. Geographic satellite ground tracks can be a separate reuse investigation against `~/dev/radio/nextpass`.

See also: [Remote ID](remote-id.md), [Watch rules](rules.md), [BLE](ble.md).
