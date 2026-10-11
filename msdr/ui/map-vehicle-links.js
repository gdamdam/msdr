// Public website lookups only: received identifiers never become URL hosts.
const identifier = (value, pattern) => {
  const text = typeof value === "string" || typeof value === "number" ? String(value).trim().toUpperCase() : "";
  return pattern.test(text) ? text : null;
};
export function vehicleExternalLinks(row) {
  if (row.layer === "aircraft") {
    const registration = identifier(row.registration, /^[A-Z0-9][A-Z0-9-]{1,11}$/);
    const callsign = identifier(row.callsign, /^[A-Z0-9]{2,12}$/);
    const icao = identifier(row.icao, /^[A-F0-9]{6}$/);
    if (!registration && !callsign && !icao) return [];
    const flight = callsign || registration?.replaceAll("-", "");
    return [
      { name: "FlightAware", url: flight ? `https://www.flightaware.com/live/flight/${flight}`
        : `https://www.flightaware.com/live/modes/${icao.toLowerCase()}/redirect`,
        note: flight ? `Identifier ${flight}` : `ICAO ${icao}` },
      { name: "Flightradar24", url: "https://www.flightradar24.com/data",
        note: `Search ${icao ? `ICAO ${icao}` : registration ? `registration ${registration}` : `callsign ${callsign}`} on Flightradar24` },
    ];
  }
  if (row.layer === "maritime") {
    const mmsi = identifier(row.mmsi, /^\d{9}$/);
    return mmsi ? [
      { name: "VesselFinder", url: `https://www.vesselfinder.com/vessels/details/${mmsi}`, note: `MMSI ${mmsi}` },
      { name: "MarineTraffic", url: "https://www.marinetraffic.com/en/ais/home",
        note: `Search MMSI ${mmsi} on MarineTraffic` },
    ] : [];
  }
  return [];
}
