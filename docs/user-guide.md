# msdr user guide

Task-oriented guide to using msdr, for anyone running it with an SDR. The [README](../README.md) covers installation. Most screens also have a **? Help** button.

## Listening

- **Tune** with `Cmd/Ctrl-K`, the frequency display (`F`) or the arrow keys. Press **S** to save what you're hearing as a memory.
- **Antenna:** the **ANT** control switches antenna A, B or C. Memories and favourites remember the antenna and restore it when tuned.
- **Locks:** the padlock beside **ANT** or **MODE** keeps the current antenna or mode when a memory, profile, catalog entry or Back/Forward is tuned. The ANT and MODE controls themselves still switch. Both locks are remembered across restarts. Tip: use the mode lock to listen in SAM to memories saved as AM.
- **FAV** in the frequency panel saves the frequency if it isn't saved yet, adds it to Favourites and opens the editor, so you can name it and set its mode and bandwidth.
- **Audio & DSP** (Listen) adjusts SDRconnect's AGC, noise reduction and squelch.
- **Native Audio & DSP** (Review and the Scope row) is msdr's own listening path: AGC, bandwidth, SSB passband, CW pitch, a notch, FM de-emphasis, noise reduction and an impulse blanker. It doesn't affect decoders or recorded IQ. [More](receiver-dsp.md)
- **Profiles** restore a saved receiver setup. When a profile uses an SDRconnect native profile, msdr marks it *unverified*, because SDRconnect doesn't confirm which profile was selected.

### Instrument panel

The panel at the top has three rows:

| Row | Contains |
|---|---|
| Status | Lamps (LINK, ALERT, owner, REC when armed or recording), source, Wi-Fi/BLE chips, **STOP**, icon actions (☆ favourite, ＋ save to library, ←, →, ⌕, **◐** theme, **⚙** setup) and the **⋯** tools menu (New profile, Diagnostics, Layout, Presets, Recordings). A `DECODE · <mode>` chip appears here while a decoder runs. |
| Readout | Frequency with fine-tune ±, step control, S-meter with SNR/PWR, band plan, allocation pill, **Details** / **Open chart** / ⓘ. |
| Settings | **RECEIVER**: Ant + lock, LNA (shows the state index 0–59, not dB), Profile, Mode, Bandwidth + lock, AGC/NR/SQL, Levels. **OPERATIONS**: recorder, **▶ Band scan · range** and **Channel scan**, which open their controls below the strip. |

Click the allocation pill to open the band's details in a popover; the ✎ button edits the frequency instead.

## Finding signals

**Discover** has these tabs: **Live survey** · **Discovery inbox** · **Saved signals / decodes** · **Wi-Fi** · **BLE** · **Remote ID** · **Timeline** · **Watch rules** · **Beacons**. The Listen panel has the tabs **Recent** · **Favorites** · **Nearby** · **Suggestions** · **Watch** · **Log**.

### Live survey

The survey lists signals found in the current span. New signals are added as they appear, signals that go away are marked **quiet**, and the rows keep their order until you press **Re-sort rows**. While your pointer is over the table it stops updating, so you can read and click. Changing the span or pressing **Clear** starts a new list.

### Band scanning

Sweep a frequency range span by span and keep everything found.

1. Press **▶ Band scan · range** in the instrument panel. It opens the Band scan form in **Discover → Live survey**, where you choose the range, span, dwell time, overlap and peak sensitivity, then start. You can pause, resume or stop; a running scan is stopped from the owner lamp's **STOP**.
2. Open **BAND SCANS**: the **Scans** list sits at the top, and picking a scan opens its report below. A report shows what was found, the stitched spectrum, and which parts of the range were actually covered.
3. To compare, open a completed session and pick an earlier one for the same range. The comparison lists **new**, **disappeared** and **changed** signals.

A frequency that wasn't covered stays unknown, and a missed transmission doesn't mean the station went off air. [More](epics/listening-workbench/survey-band-scanning.md)

### Channel watch

Watch a list of channels and capture activity. Open the **Watch** tab of the Listen panel (available in Listen and in **Discover → Live survey**).

- **What to watch:** enter frequencies in MHz and pick the mode, channel width and an optional priority channel. As you type, msdr shows which channels fit in the receiver span.
- **Follow** plays the priority channel when it's active, otherwise the strongest one.
- **Capture** arms the pre-roll IQ recorder and saves activity on the priority (or first) channel.
- **Cycle receiver spans** reaches channels too far apart for one span. It pauses during activity or recording.
- **Running now** lists everything active, each with its own stop button.

Levels are relative to the local noise floor, not calibrated power.

### Beacons

Beacons live in **Discover → Beacons**. In NCDXF mode:

- The **Now** strip shows the five stations on air this second (one per band, with a countdown) and outlines them in the matrix.
- The `±` control shifts the schedule by up to 30 s if your clock is off.
- In NDB mode, **Setup → Beacons → Pick nearby NDBs** lists the NDBs nearest your location (from the FAA navaid catalog) and adds the ones you tick to the NDB frequency list.

### Catalog

**Catalog** searches the imported station schedules and reference directories ([sources](catalogs.md)). Type a station, country or language (short words like `china` or `korea` work), or a call sign. **ON AIR** puts stations transmitting now first under any ordering; **NEARBY** sorts by distance from your location; **Sort** orders by frequency, start time (UTC) or name. **TUNE** on a row tunes it.

### Frequency context

For any frequency msdr shows:

- **Allocation:** which services the band is allocated to, with **Details** for footnotes and the ITU table (clicking a band in Allocations opens the same details in a popover; **×**, `Esc` or a click outside closes it). Pick the jurisdiction (US or ITU Region 1–3) in **Setup → Catalog**. [More](allocations.md)
- **Band plan:** the usual channel or application for that frequency (US only).
- **Catalog matches:** stations scheduled on that frequency. See [catalog sources](catalogs.md).

Every label carries a badge naming its source: DSP measurement, decoded payload, schedule, band allocation or your own note. Hover a badge to see its limits. A schedule or band-plan match is a hint, not proof that a signal was received.

## Recording and replay

- **Set up** the folder, pre-roll, free-space reserve and size limit in **Setup → Recordings**.
- **Record:** with SDRconnect playing, **ARM** starts buffering IQ, **REC** keeps it, and **STOP** saves a SigMF recording.
- **Capture health** shows data rates, drops and estimated time remaining.
- **Repair:** if a recording was interrupted, **Recordings → Interrupted recordings → Repair** recovers what was written.
- **Replay** a recording in **Review**: tune the virtual receiver inside it and run decoders or the Scope on it.

### Scope and carrier drift

Open the **Scope** row under the survey (live) or in Review (recording). Press **▶ Start** to inspect a channel without retuning the radio.

- **Views:** audio waveform, IQ envelope, I/Q trace or audio spectrum. Set a window and trigger, freeze the trace and measure with two cursors.
- **Carrier drift:** select an unmodulated carrier and press **Measure carrier offset**. You can optionally correct the offset in msdr's software channel. Weak or modulated signals give unreliable estimates.

[More](epics/signal-workbench.md)

## Decoders

Open **Decode setup (⚙)** to choose decoders; **Search decoders** filters the list. Decoder output has one place, the decode deck.

- **DECODE chip:** while a decoder runs, a `DECODE · <mode>` chip in the instrument panel's status row shows it. Hover for the last decoded line; click to jump to the deck.
- **Messages** shows decoded history. While you're scrolled down or looking at a message, new ones wait behind an "N new messages" button.

- **Raw-IQ decoders** (ADS-B / Mode S, RS41, FT8, DSC, End-of-Train, VOR, ILS, LoRa) work on the live receiver and on recordings in Review. They analyse one channel at a time. Recordings need enough sample rate, and FT8 also needs the capture time.
- **Audio decoders** include pagers, APRS, CW, RTTY, NAVTEX, WEFAX, SSTV and digital voice (DMR, P25, NXDN via `dsd-fme`). Digital voice calls can be played back from Messages.
- **CW** decodes into one continuous transcript you can select and copy.

A decoder shows *Ready* when its code or tool is installed. That doesn't mean it has been tested on real signals. [Decoder scope](decoder-expansion.md)

### LoRa, MeshCore and Meshtastic

LoRa needs nothing installed. In **Decode setup**, select **LoRa / mesh** and expand **LoRa profiles**:

| Profile | Frequency | Spreading factor | Bandwidth | Coding rate |
|---|---|---|---|---|
| MeshCore US | 910.525 MHz | SF7 | 62.5 kHz | 4/5 |
| Meshtastic US LongFast | 906.875 MHz | SF11 | 250 kHz | 4/5 |
| Custom | You choose | You choose | You choose | You choose |

Choose a profile, press **Preview receiver preset**, then **Apply receiver preset** and **START**. **Restore previous receiver settings** undoes the preset. For other regions, use **Custom** and match the sender's settings in **Setup → Decoder & validation**.

msdr shows mesh packet headers but doesn't decode message contents, and it never transmits. [More](lora-native.md)

### ADS-B / Mode S

ADS-B needs nothing installed; `dump1090-fa` can be used instead if you have it. Aircraft appear on the **Map**. Decoded: identification, position and velocity (DF17/18) and all-call replies (DF11). [More](adsb-native.md)

Other decoder notes: [FT8](ft8.md) · [RS41](rs41.md) · [DSC](dsc.md) · [SSTV](sstv.md)

## Satellites

Set your location in Setup and press **↻ TLE** to fetch orbital elements. In **Satellites**, choose the days and satellites to list passes. Click a pass for its sky path and elevation chart; **Tune** tunes its saved memory or catalog downlink, and **Track** follows its Doppler shift. **Optically visible only** is for visual observing; leave it off for radio.

## Wi-Fi and BLE

Both are optional, passive (msdr never transmits, probes or connects) and independent of the SDR.

### Wi-Fi networks

Needs an ALFA AWUS036ACH and its capture helper; see [Wi-Fi setup](wifi.md).

1. Open **Discover → Wi-Fi**, pick a channel and press **Start**.
2. **Networks** lists what was heard on that channel. Select a row for its details, history and raw frames. **Live frames** shows frames as they arrive.
3. **Record packets** saves a capture; **Review → Wi-Fi sessions** exports it as PCAP for Wireshark, or imports one.
4. **Channel survey** hops across a list of channels, with an estimate of how long it takes, a live marker for the channel being heard, and a verdict (busiest channel, most networks, least congested per band, overlap) as a starting point for choosing a router channel. **Activity** shows packets per channel over time (needs device history on, see below).

Reading the table:
- The tiles above it summarise what's around: probable access points, bands, security, makers and hidden networks.
- **Vendor names:** **Setup → Wi-Fi → Download** fetches the IEEE MAC address registry (about 5 MB, into your data folder, not shipped with msdr) so each BSSID shows its maker. **Locally administered** means an address a router made up for a guest or mesh network, or a random MAC, so it has no maker.
- **Hidden network** means the access point broadcasts no name. If a phone that knows the name reveals it, the row reads **hidden (seen as …)**. **Group** collects rows by network name or by probable access point (BSSIDs that differ only in the last digit or the locally-administered bit).
- The adapter hears one 20 MHz channel at a time. Networks on a neighbouring 2.4 GHz channel can still be heard; these show a **mismatch** marker.
- Signal is the median of recent frames, shown as a bar. The ALFA reports raw adapter codes, not dBm, so a raw bar is striped and relative to the other networks listed. Networks heard fewer than 10 times are dimmed and get no bar.

### BLE advertisements

Needs an external USB Bluetooth dongle (`pip install '.[ble]'`); see [BLE setup](ble.md).

1. Open **Discover → BLE** and press **Start**.
2. The tiles above the table count the devices heard in the last minute by kind: Apple (iPhone/iPad/Mac nearby, AirPods, Find My, HomeKit…), other makers, sensor beacons, drones.
3. **Advertisers** lists what was heard: address, name, maker, services, sensor readings and signal. Select a row for its data and history.

What msdr names:
- **Apple devices** show their Continuity message types (Handoff, Nearby Info, AirPlay, Find My…) and, for AirPods and Beats, the model and battery levels. Nothing about what a device or its owner is doing (screen, call or lock state) is decoded, not even as a count: in a home that would point at one person. These names come from public reverse-engineering and are best effort.
- **Bluetooth names:** **Setup → BLE → Download** fetches the Bluetooth SIG's company, service and device-type lists into your data folder (not shipped with msdr) so unknown makers, services and appearances get names. The IEEE vendor download above also names devices with a public address.

A Bluetooth 4 dongle hears standard advertising only. A SONOFF ZBDongle-P with Sniffle firmware also hears extended and long-range advertising and can hop channels 37/38/39 (**Setup → BLE → Channel**); it has been checked on real hardware for standard advertising and hopping. [More](ble.md)

### Drone Remote ID

Remote ID broadcasts heard by either receiver are decoded and shown in **Discover → Remote ID** and as a layer on the **Map**. A saved Wi-Fi session can be checked for Remote ID from Review.

Transports: **Wi-Fi Beacon** and **Wi-Fi NAN** (Wi-Fi Aware; its discovery windows are on channel 6, so capture there), **Bluetooth 4** standard advertising, and with the Sniffle dongle **Bluetooth 5** extended advertising; long-range (Coded PHY) needs the dongle's PHY set to Coded. Positions are what the drone broadcasts, not a measurement. [More](remote-id.md)

### History, timeline and labels

- **History** is off by default, because it keeps a log of other people's devices. To turn it on, add `"history": {"enabled": true}` to `~/Library/Application Support/msdr/settings.json` and restart; msdr then keeps a per-minute history of every device heard for 30 days. The Wi-Fi **Activity** tab needs it too.
- **Timeline:** **Discover → Timeline** shows networks and advertisers over time. Hatched areas mean msdr wasn't listening, not that the device was absent.
- **Labels:** name a device and mark it *mine*, *known* or *ignore* from its details pane.

Phones and many gadgets rotate their BLE address about every 15 minutes, so their history breaks there and they can't be labelled. [More](history.md)

## Appearance and themes

- The **◐** button in the instrument strip (left of **⚙ Setup**) cycles through five looks: Default (instrument), Midnight, Blueprint, Avionics and Monochrome. Hover it to see which one is active.
- The choice is remembered per browser (localStorage `msdr.theme`), not on the server.
- Theme fonts load from Google Fonts when you are online; offline, msdr uses system fonts instead.

## Library, backup and sharing

- **Library** holds memories, profiles, notes, tags, lists and favourites. Library JSON export/import merges libraries.
- **Full backup:** **Setup → Backup and restore → Download full backup** saves the whole database, optionally with recordings and decoder output. **Restore** replaces the current database; stop active work first. Wi-Fi/BLE history and receiver settings aren't included.
- **Signal evidence:** a saved signal's **Signal evidence** downloads a ZIP with its notes, observations, decodes and a short IQ clip when a recording exists.

## Integrations

**Setup → Integrations** shows messages from a running JS8Call, Pat (Winlink) or FreeDATA. msdr only reads; send from those applications. [More](integrations.md)

## Optional RF ML

The experimental signal classifier and detector are currently disabled. [Status](rf-ml.md)

See also: [README](../README.md) · [Wi-Fi setup](wifi.md) · [BLE setup](ble.md) · [History](history.md) · [Catalog sources](catalogs.md) · [Allocations](allocations.md)
