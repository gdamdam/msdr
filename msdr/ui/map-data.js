// Normalize decoded messages and explicitly georeferenced station references
// for the Map/Table view. Never infer coordinates from prose or frequency.

const finiteNumber = value => typeof value === 'number' && Number.isFinite(value);

function coordinates(source) {
  if (!source || typeof source !== 'object') return null;
  const fields = source.fields && typeof source.fields === 'object' ? source.fields : source;
  const lat = fields.lat_deg ?? fields.latitude ?? fields.lat;
  const lon = fields.lon_deg ?? fields.longitude ?? fields.lon;
  if (!finiteNumber(lat) || !finiteNumber(lon)) return null;
  const latitude = lat, longitude = lon;
  return Math.abs(latitude) <= 90 && Math.abs(longitude) <= 180
    ? { lat: latitude, lon: longitude } : null;
}

// WGS84 ECEF (metres) to geodetic latitude/longitude/ellipsoidal altitude.
function ecefToGeodetic(ecef) {
  if (!Array.isArray(ecef) || ecef.length < 3 || !ecef.slice(0, 3).every(finiteNumber)) return null;
  const [x, y, z] = ecef;
  const a = 6378137, f = 1 / 298.257223563, e2 = f * (2 - f);
  const p = Math.hypot(x, y);
  const radius = Math.hypot(p, z);
  if (radius < 6_300_000 || radius > 6_500_000) return null;
  if (p < 1) {
    const altitude = Math.abs(z) - a * Math.sqrt(1 - e2);
    return { lat: Math.sign(z) * 90, lon: 0, altitude };
  }
  let lat = Math.atan2(z, p * (1 - e2)), h = 0;
  for (let i = 0; i < 12; i++) {
    const sin = Math.sin(lat), n = a / Math.sqrt(1 - e2 * sin * sin);
    h = p / Math.cos(lat) - n;
    const next = Math.atan2(z, p * (1 - e2 * n / (n + h)));
    if (Math.abs(next - lat) < 1e-12) { lat = next; break; }
    lat = next;
  }
  const sin = Math.sin(lat), n = a / Math.sqrt(1 - e2 * sin * sin);
  h = p / Math.cos(lat) - n;
  if (h < -1000 || h > 100000) return null;
  return { lat: lat * 180 / Math.PI, lon: Math.atan2(y, x) * 180 / Math.PI, altitude: h };
}

function layerFor(event, signal) {
  const decoder = String(event.decoder || '').toLowerCase();
  if (decoder === 'ais' || decoder === 'dsc' || decoder.includes('marine') || /\bmarine\b|\bmaritime\b/i.test(signal?.station || '')) return 'maritime';
  if (decoder === 'aprs' || decoder.includes('packet') || /\bham\b|\bamateur\b/i.test(signal?.station || '')) return 'amateur';
  if (decoder === 'rs41' || decoder.includes('sonde')) return 'sondes';
  if (decoder === 'adsb' || decoder.includes('acars') || decoder.includes('mode-s')) return 'aircraft';
  return 'stations';
}

function signalFor(event, signals) {
  return event.signal_id ? signals.find(s => s.id === event.signal_id) || null : null;
}

function entityId(fields, decoder) {
  const id = decoder === 'adsb' ? (fields.icao ?? fields.hex) :
    decoder === 'ais' ? fields.mmsi :
    decoder === 'dsc' ? fields.sender_mmsi :
    decoder === 'aprs' || decoder.includes('packet') ? fields.from :
    decoder === 'rs41' || decoder.includes('sonde') ? fields.serial :
    fields.entity_id ?? fields.address;
  return id === null || id === undefined || id === '' ? null : String(id);
}

function aprsPosition(event) {
  if (String(event.decoder || '').toLowerCase() !== 'aprs') return null;
  const text = String(event.text || '');
  const addressEnd = text.indexOf('>');
  const payloadStart = addressEnd >= 0 ? text.indexOf(':', addressEnd) : -1;
  if (payloadStart < 0) return null;
  const payload = text.slice(payloadStart + 1);
  const type = payload[0];
  const start = type === '!' || type === '=' ? 1 : type === '/' || type === '@' ? 8 : -1;
  if (start < 0) return null;
  const match = /^(\d{2})(\d{2}\.\d{2})([NS])[\/\\A-Z0-9a-j](\d{3})(\d{2}\.\d{2})([EW])/.exec(payload.slice(start));
  if (!match) return null;
  const [, latD, latM, ns, lonD, lonM, ew] = match;
  const minutesLat = Number(latM), minutesLon = Number(lonM);
  const degreesLat = Number(latD), degreesLon = Number(lonD);
  if (minutesLat >= 60 || minutesLon >= 60 || degreesLat > 90 || degreesLon > 180 ||
      (degreesLat === 90 && minutesLat !== 0) || (degreesLon === 180 && minutesLon !== 0)) return null;
  return {
    lat: (degreesLat + minutesLat / 60) * (ns === 'S' ? -1 : 1),
    lon: (degreesLon + minutesLon / 60) * (ew === 'W' ? -1 : 1),
  };
}

export function mapRows(events = [], signals = [], catalog = []) {
  const rows = [];
  events.forEach((originalEvent, index) => {
    const event = originalEvent;
    if (!event || typeof event !== 'object') return;
    const baseFields = event.fields || {};
    const report = baseFields.report || {};
    const aircraft = String(event.decoder || '').toLowerCase() === 'adsb'
      ? (report.aircraftState || baseFields.aircraftState) : null;
    const entities = Array.isArray(aircraft) && aircraft.length
      ? aircraft.map((state, entityIndex) => ({
          state, id: `${event.id ?? `adsb:${event.ts ?? index}`}:${state.icao ?? entityIndex}`,
        })) : [{ state: null, id: event.id ?? `${event.decoder || 'message'}:${event.ts ?? index}:${index}` }];
    for (const entity of entities) {
      const fields = entity.state ? { ...entity.state } : baseFields;
      const signal = signalFor(event, signals);
      let point = coordinates(fields) || aprsPosition(event), altitude;
      if (fields.positionValid === false || fields.validPosition === false) point = null;
      if (!point && !entity.state && String(event.decoder || '').toLowerCase() === 'rs41' && event.integrity !== 'failed') {
        const converted = ecefToGeodetic(fields.ecef_position_m);
        if (converted && Math.abs(converted.lat) <= 90 && Math.abs(converted.lon) <= 180) {
          point = converted; altitude = converted.altitude;
        }
      }
      // A failed integrity result must never create a geographic point.
      if (event.integrity === 'failed') { point = null; altitude = undefined; }
      const decoder = String(event.decoder || 'message').toLowerCase();
      const identity = entityId(fields, decoder);
      const source = event.source_id || 'unknown-source';
      const positionDate = fields.positionDateTime ? Date.parse(fields.positionDateTime) / 1000 : null;
      const hasPositionDate = finiteNumber(positionDate);
      const course = fields.cog_deg ?? fields.track_deg ?? fields.course_deg ??
        fields.trueTrack ?? fields.courseOverGround ?? fields.heading_deg ?? fields.heading;
      const row = {
        id: entity.id,
        trackId: identity ? `${source}:${decoder}:${identity}` : null,
        layer: layerFor(event, signal),
        label: fields.name || fields.callsign || fields.mmsi || fields.sender_mmsi || fields.from || fields.icao || fields.serial || signal?.name || decoder.toUpperCase(),
        lat: point?.lat ?? null, lon: point?.lon ?? null,
        ts: entity.state && hasPositionDate ? positionDate :
          (finiteNumber(event.ts) ? event.ts : null),
        positionTimeKnown: entity.state ? hasPositionDate : undefined,
        courseDeg: finiteNumber(course) && course >= 0 && course < 360 ? course : null,
        // Vehicle summary fields come only from this row's decoded entity.
        decodedFields: { ...fields },
        callsign: fields.callsign ?? null,
        icao: fields.icao ?? fields.hex ?? null,
        registration: fields.registration ?? null,
        mmsi: fields.mmsi ?? fields.sender_mmsi ?? null,
        speedKn: finiteNumber(fields.sog_kn) ? fields.sog_kn :
          finiteNumber(fields.speed_knots) ? fields.speed_knots :
            finiteNumber(fields.ground_speed_knots) ? fields.ground_speed_knots :
              finiteNumber(fields.ground_speed_kt) ? fields.ground_speed_kt : null,
        receivedAt: finiteNumber(event.ts) ? event.ts : null,
        evidence: `${point ? 'decoded position' : 'decoded message'}${event.integrity_note ? ` · ${event.integrity_note}` : event.integrity ? ` · integrity ${event.integrity}` : ''}`,
        text: event.text || '', frequency_hz: event.frequency_hz ?? signal?.frequency_hz ?? null,
        event, signal,
      };
      if (altitude !== undefined) row.altitude = altitude;
      else if (finiteNumber(fields.altitude_m)) row.altitude = fields.altitude_m;
      else if (decoder === 'adsb' && finiteNumber(fields.altitude_ft)) row.altitude = fields.altitude_ft * 0.3048;
      else if (entity.state && finiteNumber(fields.altitude)) row.altitude = fields.altitude * 0.3048;
      rows.push(row);
    }
  });
  for (const [kind, records] of [['station', signals], ['catalog', catalog]]) {
    for (const record of records || []) {
      const point = coordinates(record);
      if (!point) continue;
      rows.push({
        id: `${kind}:${record.id ?? rows.length}`,
        trackId: null,
        kind, refId: record.id ?? null,
        tuneTarget: `${kind === 'catalog' ? 'catalog' : 'signal'}:${record.id ?? ''}`,
        layer: 'stations', label: record.name || record.label || record.station || record.source || 'Station',
        lat: point.lat, lon: point.lon, ts: null,
        evidence: kind === 'catalog' ? 'catalog station coordinates' : 'signal station coordinates', text: record.notes || '',
        frequency_hz: record.frequency_hz ?? null, event: null,
        signal: kind === 'station' ? record : null,
        catalog: kind === 'catalog' ? record : null,
      });
    }
  }
  return rows;
}

// Live Remote ID (docs/remote-id.md "Live"): GET /api/remoteid/live / the
// "remoteid" ws message's `items` (one per UAS group, see
// msdr/api/services/remoteid.py) -> up to three map rows per UAS (drone,
// operator, takeoff), one per explicitly reported position type. A position
// type missing from a group (never reported, or decoded as "unknown"/
// invalid) produces no row at all -- only explicitly reported, valid
// positions are ever mapped, matching mapRows' own position validation.
// `event` is a minimal truthy stand-in so these rows take the same "this is
// a live reception, not a static reference" path as decoder-event rows
// elsewhere in discover-map.js (age, evidence text, trackSegments inclusion).
const REMOTEID_POSITION_KINDS = ["drone", "operator", "takeoff"];

function remoteIdRow(group, kind, pos) {
  if (!pos || !finiteNumber(pos.latitude_deg) || !finiteNumber(pos.longitude_deg)) return null;
  const label = group.uas_id_text || (group.uas_id_hex ? `UAS ${group.uas_id_hex.slice(0, 8)}` : group.key);
  return {
    id: `remoteid:${group.key}:${kind}`,
    trackId: `remoteid:${group.key}:${kind}`,
    groupKey: group.key,
    posKind: kind,
    layer: "remoteid",
    label: `${label} · ${kind}`,
    lat: pos.latitude_deg, lon: pos.longitude_deg,
    ts: finiteNumber(pos.received_at) ? pos.received_at : null,
    positionTimeKnown: true,
    receivedAt: finiteNumber(pos.received_at) ? pos.received_at : null,
    stale: !!pos.stale,
    ageS: finiteNumber(pos.age_s) ? pos.age_s : null,
    altitudeBaroM: kind === "drone" ? (pos.altitude_baro_m ?? null) : null,
    altitudeGeoM: kind === "drone" ? (pos.altitude_geo_m ?? null) : (pos.altitude_m ?? null),
    altitude: kind === "drone" ? (pos.altitude_geo_m ?? pos.altitude_baro_m ?? null) : (pos.altitude_m ?? null),
    heightM: kind === "drone" ? (pos.height_m ?? null) : null,
    speedHorizontalMs: kind === "drone" ? (pos.speed_horizontal_ms ?? null) : null,
    speedVerticalMs: kind === "drone" ? (pos.speed_vertical_ms ?? null) : null,
    headingDeg: kind === "drone" ? (pos.direction_deg ?? null) : null,
    horizAccuracyM: kind === "drone" ? (pos.horiz_accuracy_m ?? null) : null,
    vertAccuracyM: kind === "drone" ? (pos.vert_accuracy_m ?? null) : null,
    uasIdText: group.uas_id_text ?? null,
    uasIdHex: group.uas_id_hex ?? null,
    uaType: group.ua_type ?? null,
    transports: Array.isArray(group.sources) ? group.sources : [],
    conflict: !!group.conflict,
    evidence: `reported ${kind} position (Remote ID claim, never inferred)`,
    text: "",
    event: { source: "remoteid" },
    signal: null, catalog: null,
  };
}

export function remoteIdRows(items = []) {
  const rows = [];
  for (const group of items || []) {
    if (!group || typeof group !== "object") continue;
    for (const kind of REMOTEID_POSITION_KINDS) {
      const row = remoteIdRow(group, kind, group[`${kind}_position`]);
      if (row) rows.push(row);
    }
  }
  return rows;
}

// Consecutive valid fixes are split on long time gaps and longitude wrap so
// map renderers never draw a false line across the world.
export function trackSegments(rows = [], { maxGapSeconds = 1800 } = {}) {
  const grouped = new Map();
  for (const row of rows) {
    if (!row.event || !row.trackId || row.positionTimeKnown === false ||
        !finiteNumber(row.ts) || !finiteNumber(row.lat) || !finiteNumber(row.lon)) continue;
    const key = row.trackId;
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(row);
  }
  const segments = [];
  for (const points of grouped.values()) {
    points.sort((a, b) => Number(a.ts) - Number(b.ts));
    let segment = [];
    const flush = () => { if (segment.length > 1) segments.push(segment); segment = []; };
    for (const point of points) {
      const prev = segment[segment.length - 1];
      const gap = prev && finiteNumber(prev.ts) && finiteNumber(point.ts) ? Number(point.ts) - Number(prev.ts) : 0;
      if (prev && (Math.abs(Number(point.lon) - Number(prev.lon)) > 180 || gap > maxGapSeconds || gap < 0)) flush();
      segment.push(point);
    }
    flush();
  }
  return segments;
}
