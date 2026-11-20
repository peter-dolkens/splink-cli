# splink-cli

A cross-platform, **read-only** bridge for Selectronic SP PRO inverters, with optional
Fronius Solar API aggregation. Speaks the SP PRO's native serial protocol directly, so it
runs anywhere .NET runs — no Windows, no SP LINK installation.

The project is `splink-cli`; the command it installs is `splink`.

Built because SP LINK, the vendor's configuration and monitoring tool, is Windows-only.

## What it does

| | |
| --- | --- |
| **Live data** | battery SoC, voltage, current and power; AC load, voltage and frequency; charger, generator and AC source state; inverter mode and power |
| **AC-coupled solar** | the SP PRO's own measurement of managed AC-coupled inverters — total and per inverter, plus the commanded output limit and the detected inverter models |
| **Accumulators** | today's energy in and out, run hours, and the lifetime, 7-, 30- and 365-day histories |
| **Every display block** | 15 register blocks, 392 fields, 338 decoded to engineering units — everything SP LINK itself puts on screen |
| **Configuration** | all 563 settings, decoded with the vendor's own names and labels |
| **Logged data** | the four on-device logs — alert events, operational events, daily summary and detailed — decoded to engineering units |
| **Clock** | read the inverter's real-time clock, and report drift against an NTP-disciplined host — withheld when that host clock is not disciplined |
| **Fronius** | aggregate any number of Fronius inverters over the Solar API, including per-MPPT DC strings |
| **Serving** | a read-only JSON API and Prometheus `/metrics`, or MQTT discovery for Home Assistant |

### Read-only by design

This talks to live power equipment. `SpProClient` refuses every write unless explicitly
constructed with `allowWrites: true`, and the CLI requires `--allow-writes`. The only
exceptions are two connection-lifecycle registers — the login challenge response and the
disconnect notification — which are volatile protocol state rather than device settings.

`splink serve` can never write. Configuration writes and firmware update are **not
implemented and not planned**: the risk of bricking an inverter or corrupting battery
charge parameters is not worth it.

`serve` exposes `/raw`, which reads an arbitrary register block, only when given
`--allow-raw`, and then only to requests arriving directly on loopback. Every other endpoint
answers from a short cache, so no volume of external traffic becomes more than one inverter
read per cache interval; `/raw` drives a round trip per request, which is not something to
leave reachable from a tunnel.

## Quick start

```sh
splink ports                                   # find the inverter
splink probe   --port auto --verbose           # connect, log in, show the clock
splink now     --port auto                     # live data
splink config  --port auto --json              # all 563 settings
splink logs    --port auto                     # what logged data exists
splink download --port auto --log daily        # logged records as CSV
splink read    --port auto 41048 85            # dump a raw register block
splink serve   --port auto --fronius a=10.0.0.1,b=10.0.0.2
```

Connect over USB/serial (`--port`, with auto-baud), an Ethernet serial adaptor
(`--host`/`--tcp-port`), or remotely through a select.live gateway (`--select-live`).
`--simulate` runs everything against a built-in simulated inverter, no hardware required.

### Home Assistant add-on

This repository is also a Home Assistant add-on repository. Add
`https://github.com/peter-dolkens/splink-cli` under Settings → Add-ons → Add-on Store →
Repositories and install **splink**: it runs `splink serve` on the add-on network, so
other add-ons can read `http://<hostname>:8080/`. See [splink/DOCS.md](splink/DOCS.md).

## Modbus

The SP PRO also has a vendor-documented Modbus RTU slave interface on its Advanced
Communication Card, which this project does not use. Its register map, scalings and timing
requirements are written up in [docs/modbus.md](docs/modbus.md) — useful for cross-checking a
decode, and for anyone who would rather build on a supported interface than a recovered one.
It reaches 55 registers against the native protocol's ~945 words, 563 settings and four logs.

## Protocol notes

Recovered by reverse engineering for interoperability, then verified against hardware.
These are facts about the wire format, not vendor source.

- **Framing** — `op` (`Q` = 0x51 read, `W` = 0x57 write), `wordCount - 1`, a 32-bit
  little-endian register address, then CRC-16/KERMIT over those six bytes. Writes append
  the data words plus a CRC over the data. Responses echo the 8-byte header, then data and
  CRC; a write response echoes the request. The receiver resynchronises on the next
  `Q`/`W` byte.
- **Serial** — 8N1, no flow control, RTS and DTR asserted. Auto-baud tries 57600, 115200,
  230400, 128000, 9600, 2400, 1200, 4800, 19200, 38400.
- **Login** — read the link-port register; `0xFFFF` means a login is required. Read a
  16-byte challenge, reply with `MD5(challenge ‖ password padded to 32 ASCII bytes)` as
  eight words, then read the result register, which is `1` on success.
- **Analogue scaling** — raw counts times a model-specific factor over 327680. The factors
  are read from the device, not assumed.
- **Timestamps** — seconds since **2001-01-01**, low word first.
- **Service Settings** — SP LINK's service-level settings are not in any of the four
  configuration blocks; they live in their own 127-word block at register **49665**, of which
  39 words are named and the rest are spacers reading `0000` or `FFFF`. The block was located
  and mapped by GitHub user [**mallinss**](https://github.com/peter-dolkens/splink-cli/issues/1),
  by value-searching a SP LINK export against the register space, and is verified here on a
  second unit two firmware revisions apart. `splink config --service` reads it. Raw counts
  only: the names are known, the scalings are not, and several of these are grid-protection
  parameters where a guessed scaling would be worse than a raw number. One of them,
  `AllowPowerOverride`, gates the Modbus registers `8032`/`8033` that [the Modbus
  reference](docs/modbus.md) lists as writable.
- **Logged data** — four circular logs, each with its own register block. Entry size and
  sector layout are read from the device. Paging walks *backwards* from the newest record,
  wrapping between sectors. Records carry their own scale factors in trailing words.

## Architecture

```
src/SpLink.Protocol    framing, CRC, login, codecs, transports, Fronius client, simulator
src/SpLink.Cli         the splink command
tests/                 132 tests, including a simulated inverter and a fake Fronius
tools/                 scripts that mine register maps and label tables (see below)
deploy/                provisioning for a Raspberry Pi bridge
splink/                the Home Assistant add-on
```

The register map, the 30 configuration enum tables, the 733 event labels and the display
block map are **not hand-transcribed**. `tools/extract_*.py` and `tools/gen_*.py` mine them
and generate the `*.g.cs` files, so they can be regenerated against a different firmware
revision rather than maintained by hand. The tools expect a local decompilation, which is
deliberately not distributed here.

Deriving the display blocks mechanically is harder than a scrape, because a control is
assigned in several branches of a memory-map version test, and the AC-coupled controls are
reused for a network power meter when one is fitted — chosen at runtime. The generator
resolves both by preferring the branch whose converter agrees with what the control's name
claims, then by the candidate that continues its numbered family's stride. A field whose
converter has not been transcribed is still emitted, carrying its raw words and the
converter's name, so an undecoded register says so rather than being dropped or guessed at.

## Status

Validated against an SP PRO SPMC482 (hardware revision 25) and two Fronius Primo GEN24 10.0:
connection and auto-baud, the MD5 login, live data, the clock, all 563 configuration
settings, all four logged-data types, and a 945-word sweep of every block SP LINK displays.

Scalings are cross-checked rather than assumed. The SP PRO's own AC-coupled measurement
agrees with what the Fronius report over their own API to within 1%; the model codes it
stores decode to the inverters actually installed; and its stored AC-coupled capacity
matches their combined nameplate.

## Legal

An independent interoperability project. Not affiliated with, endorsed by, or supported by
Selectronic Australia Pty Ltd or Fronius International GmbH. "SP PRO", "SP LINK" and
"Fronius" are the trademarks of their respective owners.

The protocol was determined by reverse engineering a lawfully obtained copy of the vendor's
software, for the sole purpose of interoperating with hardware the author owns. No vendor
source code is reproduced in this repository.

Use at your own risk. This software reads from power equipment that people depend on.

## Licence

MIT — see [LICENSE](LICENSE).
