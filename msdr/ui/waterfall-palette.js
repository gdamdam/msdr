// Waterfall gradient palettes, each built once into a 256-entry LUT so
// per-frame painting is a table lookup, not per-pixel color math.

// Classic SDR# (SDRSharp) waterfall gradient: black -> deep blue -> blue ->
// cyan -> green -> yellow -> red -> white.
const SDRSHARP_STOPS = [
  [0 / 7, 0x00, 0x00, 0x00],
  [1 / 7, 0x00, 0x00, 0x20],
  [2 / 7, 0x00, 0x00, 0xff],
  [3 / 7, 0x00, 0xff, 0xff],
  [4 / 7, 0x00, 0xff, 0x00],
  [5 / 7, 0xff, 0xff, 0x00],
  [6 / 7, 0xff, 0x00, 0x00],
  [7 / 7, 0xff, 0xff, 0xff],
];

// SatDump's default waterfall look: black -> dark indigo -> blue -> cyan ->
// yellow -> red-orange -> white.
const SATDUMP_STOPS = [
  [0 / 6, 0x00, 0x00, 0x00],
  [1 / 6, 0x0b, 0x0b, 0x3b],
  [2 / 6, 0x1e, 0x5b, 0xd6],
  [3 / 6, 0x21, 0xc7, 0xc7],
  [4 / 6, 0xf2, 0xf2, 0x10],
  [5 / 6, 0xff, 0x3b, 0x00],
  [6 / 6, 0xff, 0xff, 0xff],
];

const PALETTE_STOPS = {
  sdrsharp: SDRSHARP_STOPS,
  satdump: SATDUMP_STOPS,
};

function buildStopsPalette(stops, size) {
  const lut = new Uint8ClampedArray(size * 3);
  for (let i = 0; i < size; i++) {
    const t = i / (size - 1);
    let lo = 0;
    while (lo < stops.length - 2 && t > stops[lo + 1][0]) lo++;
    const [t0, r0, g0, b0] = stops[lo];
    const [t1, r1, g1, b1] = stops[lo + 1];
    const span = t1 - t0 || 1;
    const k = (t - t0) / span;
    const o = i * 3;
    lut[o] = r0 + (r1 - r0) * k;
    lut[o + 1] = g0 + (g1 - g0) * k;
    lut[o + 2] = b0 + (b1 - b0) * k;
  }
  return lut;
}

/** Linear black -> white ramp. */
function buildGrayPalette(size) {
  const lut = new Uint8ClampedArray(size * 3);
  for (let i = 0; i < size; i++) {
    const v = Math.round((i / (size - 1)) * 255);
    const o = i * 3;
    lut[o] = v;
    lut[o + 1] = v;
    lut[o + 2] = v;
  }
  return lut;
}

const _paletteCache = new Map();

/** 256x[r,g,b] lookup table, flattened, for the named palette ("sdrsharp",
 * "satdump" or "gray") -- built once per name and cached. */
export function buildPalette(name = "sdrsharp", size = 256) {
  const key = `${name}:${size}`;
  let lut = _paletteCache.get(key);
  if (lut) return lut;
  lut = name === "gray" ? buildGrayPalette(size) : buildStopsPalette(PALETTE_STOPS[name] ?? SDRSHARP_STOPS, size);
  _paletteCache.set(key, lut);
  return lut;
}

/** Default palette, kept for callers that haven't been updated to pick one. */
export const WATERFALL_PALETTE = buildPalette("sdrsharp");

/** dB value -> palette index (0..255), clamped. */
export function dbToIndex(db, minDb, maxDb) {
  if (!(maxDb > minDb)) return 0;
  const t = (db - minDb) / (maxDb - minDb);
  return Math.max(0, Math.min(255, Math.round(t * 255)));
}

/** uint8 bin value (0..255) -> dB, given the frame's base_db..ref_level_db range. */
export function binToDb(bin, baseDb, refDb) {
  const t = Math.max(0, Math.min(255, Number(bin) || 0)) / 255;
  return baseDb + (refDb - baseDb) * t;
}
