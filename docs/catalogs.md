# Catalog sources

The catalog holds imported station schedules and reference directories. This page lists each source and how to set it up.

Open **Catalog → Sources & imports** to see each source's entry count, URL and last refresh. Some sources need a URL saved before you can refresh them. If a refresh fails, the previous data is kept.

A catalog match is schedule or reference data, not proof that a signal was received. **Tune** is offered only when an entry has an exact frequency.

## Sources

| Source | What it is | Setup |
|---|---|---|
| [EiBi](https://eibispace.de/) | Shortwave broadcast schedules | Built in; follows the current season. |
| Aoki | Shortwave broadcast schedules | Paste the current season's schedule ZIP URL. |
| [HFCC](https://new.hfcc.org/data/) | Shortwave broadcast schedules | Paste the current season's schedule ZIP URL. |
| [NDXC](https://ndxc.info/) | Broadcast frequency list | Built in. |
| NOAA Radiofax | Weather fax stations | Bundled; no setup. |
| SigIDWiki | Signal-type reference | Bundled; no setup. |
| [Priyom](https://priyom.org/number-stations/station-schedule) | Numbers-station schedule | Built in (next seven days); an `.ics` feed also works. |
| [SatNOGS](https://db.satnogs.org/) | Satellite transmitters | Built in. |
| NIST time | WWV, WWVH and WWVB stations | Bundled; no setup. |
| [FAA NASR](https://www.faa.gov/air_traffic/flight_info/aeronav/aero_data/NASR_Subscription/) | US navaids and ILS | Bundled copy works offline; refresh for the current cycle. |
| [IOOS HFRNet](https://hfradar.ioos.us/) | HF ocean radar sites | Built in. |
| [SuperDARN](https://github.com/SuperDARN/hdw) | Research radar sites | Bundled; locations only, no frequencies. |

Each source is labeled **static** (bundled), **refreshable** (downloaded on refresh) or **live** (fetched live).

## Updating Aoki and HFCC each season

Aoki and HFCC publish a new file each season (A = summer, B = winter). Update the saved URL when the season changes. For example, the A26 files are:

- Aoki: `https://www1.m2.mediacat.ne.jp/binews/ut/xt/xta26.zip`
- HFCC: `https://new.hfcc.org/data/a26/a26allx2.zip`

See also: [reference catalogs](reference-catalogs.md), [allocations](allocations.md).
