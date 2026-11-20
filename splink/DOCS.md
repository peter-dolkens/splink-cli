# splink

A read-only bridge for a Selectronic SP PRO, running inside Home Assistant. It is the
`splink serve` command from [splink-cli](https://github.com/peter-dolkens/splink-cli),
packaged as an add-on: it reads the inverter on demand and serves a JSON API and
Prometheus `/metrics` on the add-on network, where other add-ons can poll it.

It does not create Home Assistant entities and does not publish MQTT. Something that
reads the JSON (your own add-on, a REST sensor, Prometheus) does that.

It cannot write to the inverter: `serve` has no write path at all.

## Connecting to the SP PRO

The SP PRO admits **one session at a time**. Run only one client against it: not this
add-on and SP LINK, or this add-on and another splink, at the same time. Stop the other
client before you start this one.

**Serial-to-TCP adaptor** (`connection: tcp`). An Ethernet adaptor, or an ESP32 bridge that
exposes the SP PRO's USB serial as a TCP stream:

```yaml
connection: tcp
host: sp-pro-adaptor.lan
tcp_port: 6639
```

**USB or serial on this machine** (`connection: serial`). Plug the SP PRO into the Home
Assistant host. `serial_port: auto` picks the only candidate port and fails if there are
none or several; name a `/dev/serial/by-id/...` path if you have other serial hardware.
Leave `baud` empty to auto-detect.

`password` is only needed if the SP PRO's login password was changed from the factory default.
It is passed to splink through the environment, not the command line.

## Reading it from other add-ons

The API listens on `http_port` (8080) on the internal add-on network. Use the
**Hostname** shown on this add-on's Info page:

```
http://<hostname>:8080/          SP PRO snapshot (+ Fronius, if configured)
http://<hostname>:8080/fast      SoC, load, battery current: cheap enough to poll every second
http://<hostname>:8080/sppro     SP PRO only
http://<hostname>:8080/fronius   Fronius only (404 if none are configured)
http://<hostname>:8080/health    200 when the SP PRO reads, 503 when it doesn't
http://<hostname>:8080/metrics   Prometheus text format
```

The port is not published on the host. To reach it from outside Home Assistant, set a host
port under **Network**. The API has no authentication, so think before you do.

Nothing is read until a request arrives. The first request connects and logs in, which
takes a few seconds; the link is released after `idle_timeout` seconds without requests,
so keep that above your poll interval.

There is no web page, so the add-on has no sidebar panel.

## Fronius inverters

Optional. List them by name and host:

```yaml
fronius:
  - name: left
    host: 192.168.1.51
  - name: right
    host: 192.168.1.52
```

Or leave the list empty and set `bridge_config: bridge.json`, a file in this add-on's
config folder (`/addon_configs/<slug>/bridge.json` from the Samba or SSH add-ons):

```json
{ "fronius": [ { "name": "left", "host": "192.168.1.51" } ] }
```

With neither, the add-on serves the SP PRO only: `fronius` is `null` in the snapshot,
`solar_total_watts` is `null`, and `/fronius` returns 404. The SP PRO's own AC-coupled
measurement (`sp_pro.ac_coupled`) is still there.

## Options

| Option | Default | |
| --- | --- | --- |
| `connection` | `tcp` | `tcp` or `serial` |
| `host`, `tcp_port` | `""`, `6639` | the adaptor (tcp) |
| `serial_port`, `baud` | `auto`, empty | the serial port (serial); empty baud = auto |
| `password` | empty | SP PRO login password, if not the default |
| `http_port` | `8080` | API port inside the add-on network |
| `fronius` | `[]` | `name` + `host` per inverter |
| `bridge_config` | empty | bridge.json in the add-on config folder, used when `fronius` is empty |
| `idle_timeout` | `300` | seconds without requests before the link is released |
| `live_interval` | `120` | seconds between full live reads |
| `today_interval` | `600` | seconds between reads of today's counters |
| `clock_interval` | `3600` | seconds between clock reads |
| `sweep_interval` | `1800` | seconds between block sweeps (`?blocks=true`) |
| `log_interval` | `3600` | seconds between logged-data checks (`?logs=true`) |
| `fast_cache` | `1` | seconds a `/fast` reading is reused |
| `cache` | unset | override of the short cache the other endpoints answer from |
| `allow_raw` | `false` | enable `/raw`; it answers only requests from inside the container |
| `log_level` | `info` | `debug` hex-dumps every frame (very noisy) |

The interval defaults suit a bridge polled every few seconds for `/fast` and every minute or
two for the full snapshot.

## Moving from a standalone splink

If splink already runs on another machine (for example a Raspberry Pi with the
`deploy/` systemd unit), move it like this. Only one of them may hold the SP PRO session.

1. Install this add-on and configure it, but leave it stopped (Start on boot: off).
2. Stop the old bridge and anything that would restart it (`sudo systemctl stop splink`,
   plus any watchdog that restarts it or reboots when it is unreachable).
3. Start the add-on and check `http://<hostname>:8080/health` returns `ok` (from another
   add-on, or the SSH add-on: `curl http://<hostname>:8080/health`).
4. Turn **Start on boot** on (and the watchdog, if you want it).
5. Point your consumers at `http://<hostname>:8080/`.
6. Disable the old bridge (`sudo systemctl disable splink`) so a reboot doesn't bring it back.

To go back: stop the add-on, turn Start on boot off, then start the old bridge.

## Building

The Supervisor builds the image on your device: the `Dockerfile` fetches the
repository at the commit in `build.yaml` (`SPLINK_REF`), publishes it with the .NET SDK,
and copies the result onto the .NET runtime (Alpine). The first install pulls the SDK image
and takes a few minutes.
