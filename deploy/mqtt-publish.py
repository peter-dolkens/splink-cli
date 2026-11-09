#!/usr/bin/env python3
"""Publish solar bridge readings to Home Assistant over MQTT discovery.

Runs on the Pi, reads the bridge over localhost and publishes to the Home Assistant broker.

Topics are namespaced per device, so a second inverter can be added without colliding:

    solar-bridge/status                              bridge availability (retained LWT)
    solar-bridge/selectronic/<inverter>/state        fast-moving measurements
    solar-bridge/selectronic/<inverter>/state_60s    status and limits that rarely move
    solar-bridge/selectronic/<inverter>/state_300s   today's accumulators
    solar-bridge/selectronic/<inverter>/blocks       the full register sweep
    solar-bridge/fronius/<name>/state

A topic republishes whenever any field on it changes, so splitting by rate is what gives each
reading its own cadence: a slow value cannot drag the live topic along with it, and the live
topic carries only what is worth watching at full speed. Entities are mapped onto the topic
matching how often the underlying reading actually moves.

Discovery follows the layout zigbee2mqtt uses -- <component>/<device_id>/<object_id>/config --
so entities group under a stable device identifier rather than relying on a name prefix.

Everything site-specific lives in the config file: which inverter this is, the bucket
intervals, which entities to leave out, and whether to round anything on the way out.
"""
import fnmatch
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.request

import paho.mqtt.client as mqtt

CONF = os.environ.get("MQTT_ENV", "/etc/splink/mqtt.env")
CONFIG = os.environ.get("MQTT_CONFIG", "/etc/splink/publisher.json")
# The bridge omits the block sweep unless asked, since it is most of the payload and the public
# feed rarely wants it. The block sensors depend on it, so request it explicitly.
# Asked for without the block sweep by default. The bridge only re-reads the blocks from the
# inverter on its own timer, so requesting them on every poll costs no inverter traffic -- but
# it does serialise and parse 102 KB instead of 4.5 KB each time, to use it once every fifteen
# minutes. The query is added only on the polls that will actually publish them.
BRIDGE = os.environ.get("BRIDGE_URL", "http://localhost:8080/").split("?")[0]
INTERVAL = int(os.environ.get("INTERVAL", "60"))
# Republish even when unchanged this often, so retained values stay fresh.
FORCE_EVERY = int(os.environ.get("FORCE_EVERY", "900"))
PREFIX = "solar-bridge"
DISCOVERY = "homeassistant"
# Touched whenever a cycle completes with the broker connected, so the watchdog can tell
# "publishing is working" from "this process is alive but achieving nothing". It lives on
# tmpfs: this is written every cycle and the SD card should not wear out recording it.
HEARTBEAT = os.environ.get("MQTT_HEARTBEAT", "/run/splink/mqtt-ok")

running = True
# Per-topic send records moved into Target: see the note there on why they cannot be shared.
_last_forced = 0.0
# When the blocks bucket was last published, which decides whether to ask the bridge for the
# 102 KB block payload at all. Tracked here rather than per target: it is a question about what
# to fetch, and fetching once serves every target.
_blocks_published_at = 0.0


def load_env(path):
    conf = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip()
    return conf


def load_config(path):
    """Site configuration. Every key is optional and the defaults suit a single SP PRO."""
    cfg = {}
    try:
        with open(path) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        print(f"no {path}; using defaults", flush=True)
    except (OSError, ValueError) as e:
        print(f"config {path}: {e}; using defaults", file=sys.stderr, flush=True)
    cfg.setdefault("inverter_name", "sppro")
    # Prefixes object_id and unique_id for this inverter's entities. Home Assistant keys
    # entities on unique_id, so changing this orphans their history -- it is deliberately
    # separate from inverter_name, which only names topics. A second inverter needs its own.
    cfg.setdefault("entity_prefix", "sp_pro")
    # Minimum seconds between publishes of each topic. 0 publishes on every poll.
    cfg.setdefault("buckets", {"state": 0, "state_60s": 60, "state_300s": 300, "blocks": 900})
    # Globs matched against object_id. An excluded entity is never announced at all, so it
    # costs nothing downstream -- no discovery config, no state, no recorder rows.
    cfg.setdefault("exclude", [])
    # Per-entity rounding as {object_id_glob: decimal_places}. Empty publishes raw readings.
    cfg.setdefault("quantize", {})
    return cfg


CFG = load_config(CONFIG)
INVERTER = CFG["inverter_name"]
EPREFIX = CFG["entity_prefix"]
BUCKETS = CFG["buckets"]
SPPRO_BASE = f"{PREFIX}/selectronic/{INVERTER}"

# The bridge reads a 14-register window off the inverter far more often than it reads the
# 85-word live block, so a handful of readings can be published seconds after they change
# rather than at the main poll interval. That matters for load shedding: the SP PRO can
# sustain a large overload only briefly, so Home Assistant needs to see the spike while there
# is still time to act on it.
#
# These are not new entities. Each already exists on a slower topic, and moving it here keeps
# its unique_id, so Home Assistant reuses the registry entry and the history carries over.
# Only the fields the fast window actually covers are listed -- anything else would have to be
# read from the live block and would gain nothing.
#
# short name -> field in the /fast payload. Units match the slow path's, so the sensor's
# existing multiplier and precision still apply.
FAST_FIELDS = {
    "ac_load_power": "ac_load_kilowatts",
    "inverter_power": "inverter_kilowatts",
    "battery_soc": "battery_soc_percent",
    "battery_current": "battery_amps",
    "dc_current": "dc_amps",
    "charger_state": "charger",
    "inverter_mode": "inverter_mode",
    "ac_source_status": "ac_source_status",
}
# Opt-in: without a state_fast bucket the fast loop never starts and every sensor above stays
# on the topic it is on today.
FAST_BUCKET = "state_fast"
FAST_INTERVAL = BUCKETS.get(FAST_BUCKET) or 0


def excluded(object_id):
    return any(fnmatch.fnmatch(object_id, pat) for pat in CFG["exclude"])


def quantize(object_id, value):
    """Round only where configured; unset means the raw reading goes out untouched."""
    if not isinstance(value, float):
        return value
    for pat, places in CFG["quantize"].items():
        if fnmatch.fnmatch(object_id, pat):
            return round(value, places)
    return value


def slug(text):
    """CamelCase to snake_case, keeping acronyms intact: ACOutStatus -> ac_out_status."""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", text)
    s = re.sub(r"[^0-9A-Za-z]+", "_", s).lower()
    return re.sub(r"_+", "_", s).strip("_")


# --- devices ---------------------------------------------------------------------------------

def sppro_device(unit):
    serial = str((unit or {}).get("serial") or INVERTER)
    return f"sppro_{serial}", {
        "identifiers": [f"sppro_{serial}"],
        "name": "SP PRO",
        "manufacturer": "Selectronic",
        "model": (unit or {}).get("model") or "SP PRO",
        "serial_number": serial,
        "suggested_area": "Build Site",
    }


def fronius_device(name, label):
    return f"fronius_{name}", {
        "identifiers": [f"fronius_{name}_buildsite"],
        "name": label,
        "manufacturer": "Fronius",
        "model": "Solar Inverter",
        "suggested_area": "Build Site",
    }


# --- sensor definitions ----------------------------------------------------------------------
# (object_id, name, path, unit, device_class, state_class, icon, precision, bucket, multiplier)
#
# The bucket is the slowest topic that still gives a useful reading. Everything on the live
# topic contributes to how often it republishes, so only genuinely fast values belong there.

SPPRO_SENSORS = [
    ("battery_soc", "Battery SoC", "battery.soc_percent", "%", "battery", "measurement", None, 2, "state", 1),
    ("battery_voltage", "Battery Voltage", "battery.volts", "V", "voltage", "measurement", None, 2, "state", 1),
    ("battery_current", "Battery Current", "battery.amps", "A", "current", "measurement", None, 2, "state", 1),
    ("battery_power", "Battery Power", "battery.kilowatts", "W", "power", "measurement", None, 0, "state", 1000),
    ("ac_load_power", "AC Load Power", "ac.load_kilowatts", "W", "power", "measurement", None, 0, "state", 1000),
    ("ac_voltage", "AC Voltage", "ac.volts", "V", "voltage", "measurement", None, 1, "state", 1),
    ("ac_frequency", "AC Frequency", "ac.frequency_hz", "Hz", "frequency", "measurement", None, 2, "state", 1),
    ("dc_current", "DC Current", "dc.amps", "A", "current", "measurement", None, 2, "state", 1),
    ("solar_total_power", "Solar Total Power", "solar_total_watts", "W", "power", "measurement", None, 0, "state", 1),
    ("ac_coupled_power", "AC Coupled Solar Power", "ac_coupled.kilowatts", "W", "power", "measurement", None, 0, "state", 1000),
    ("ac_coupled_power_1", "AC Coupled Solar Power 1", "ac_coupled.per_inverter.0", "W", "power", "measurement", None, 0, "state", 1000),
    ("ac_coupled_power_2", "AC Coupled Solar Power 2", "ac_coupled.per_inverter.1", "W", "power", "measurement", None, 0, "state", 1000),
    ("ac_coupled_power_3", "AC Coupled Solar Power 3", "ac_coupled.per_inverter.2", "W", "power", "measurement", None, 0, "state", 1000),
    ("ac_coupled_power_4", "AC Coupled Solar Power 4", "ac_coupled.per_inverter.3", "W", "power", "measurement", None, 0, "state", 1000),
    ("ac_coupled_power_5", "AC Coupled Solar Power 5", "ac_coupled.per_inverter.4", "W", "power", "measurement", None, 0, "state", 1000),
    ("ac_coupled_percent", "AC Coupled Commanded Limit", "ac_coupled.percent", "%", None, "measurement", "mdi:speedometer", 1, "state", 1),
    ("inverter_power", "Inverter AC Power", "inverter.kilowatts", "W", "power", "measurement", None, 0, "state", 1000),
    ("inverter_current", "Inverter AC Current", "inverter.amps", "A", "current", "measurement", None, 2, "state", 1),
    ("battery_load_5min", "Battery Load 5 min", "battery_trend.load_5min_kilowatts", "W", "power", "measurement", None, 0, "state", 1000),
    ("battery_load_15min", "Battery Load 15 min", "battery_trend.load_15min_kilowatts", "W", "power", "measurement", None, 0, "state", 1000),
    ("shunt1_current", "Shunt 1 Current", "shunts.shunt1_amps", "A", "current", "measurement", None, 2, "state", 1),
    ("shunt2_current", "Shunt 2 Current", "shunts.shunt2_amps", "A", "current", "measurement", None, 2, "state", 1),
    ("shunt1_power", "Shunt 1 Power", "shunts.shunt1_kilowatts", "W", "power", "measurement", None, 0, "state", 1000),
    ("shunt2_power", "Shunt 2 Power", "shunts.shunt2_kilowatts", "W", "power", "measurement", None, 0, "state", 1000),

    ("charger_state", "Charger State", "charger", None, None, None, "mdi:battery-charging", None, "state_60s", 1),
    ("generator_state", "Generator State", "generator.status", None, None, None, "mdi:engine", None, "state_60s", 1),
    ("generator_reason", "Generator Reason", "generator.reason", None, None, None, "mdi:engine-outline", None, "state_60s", 1),
    ("generator_power", "Generator Power", "generator.kilowatts", "W", "power", "measurement", None, 0, "state_60s", 1000),
    ("generator_voltage", "Generator Voltage", "generator.volts", "V", "voltage", "measurement", None, 1, "state_60s", 1),
    ("generator_frequency", "Generator Frequency", "generator.frequency_hz", "Hz", "frequency", "measurement", None, 2, "state_60s", 1),
    ("inverter_mode", "Inverter Mode", "inverter.mode", None, None, None, "mdi:sine-wave", None, "state_60s", 1),
    ("ac_source_status", "AC Source Status", "inverter.ac_source_status", None, None, None, "mdi:transmission-tower", None, "state_60s", 1),
    ("clock_drift", "Clock Offset vs Bridge", "inverter_clock_drift_seconds", "s", None, "measurement", "mdi:clock-alert-outline", 1, "state_60s", 1),
    ("charge_power_limit", "Charge Power Limit", "regulation.charge_power_limit_kilowatts", "W", "power", "measurement", None, 0, "state_60s", 1000),
    ("export_power_limit", "Export Power Limit", "regulation.export_power_limit_kilowatts", "W", "power", "measurement", None, 0, "state_60s", 1000),
    ("input_power_limit", "Input Power Limit", "regulation.input_power_limit_kilowatts", "W", "power", "measurement", None, 0, "state_60s", 1000),
    ("support_power_limit", "Support Power Limit", "regulation.support_power_limit_kilowatts", "W", "power", "measurement", None, 0, "state_60s", 1000),

    ("today_ac_coupled_energy", "Solar Energy Today", "today.ac_coupled_kwh", "kWh", "energy", "total_increasing", None, 3, "state_300s", 1),
    ("today_ac_load_energy", "Load Energy Today", "today.ac_load_kwh", "kWh", "energy", "total_increasing", None, 3, "state_300s", 1),
    ("today_battery_in_energy", "Battery In Today", "today.battery_in_kwh", "kWh", "energy", "total_increasing", None, 3, "state_300s", 1),
    ("today_battery_out_energy", "Battery Out Today", "today.battery_out_kwh", "kWh", "energy", "total_increasing", None, 3, "state_300s", 1),
    ("today_ac_coupled_peak", "Solar Peak Today", "today.ac_coupled_peak_kilowatts", "W", "power", "measurement", None, 0, "state_300s", 1000),
    ("today_inverter_run_hours", "Inverter Run Hours Today", "today.inverter_run_hours", "h", "duration", "total_increasing", None, 2, "state_300s", 1),
    ("today_float_hours", "Float Hours Today", "today.float_hours", "h", "duration", "total_increasing", None, 2, "state_300s", 1),

    # Lifetime run hours, promoted out of the block sweep so it carries a name worth reading.
    # One counter per inverter is enough to show it has not been down: it rises with wall-clock
    # time while the inverter is up, so a stall against elapsed time is the gap.
    ("inverter_hours", "Inverter Hours", "Technical.InverterRunHrsTotalAcc", "h", "duration", "total_increasing", None, 2, "blocks", 1),
]

FRONIUS_SENSORS = [
    ("power", "Power", "ac.power_watts", "W", "power", "measurement", 0, 1),
    ("ac_voltage", "AC Voltage", "ac.volts", "V", "voltage", "measurement", 1, 1),
    ("ac_frequency", "AC Frequency", "ac.frequency_hz", "Hz", "frequency", "measurement", 2, 1),
    ("energy_total", "Total Energy", "energy.total_wh", "kWh", "energy", "total_increasing", 3, 0.001),
]

# Numeric fields the decoder emits without a unit, because SP LINK's converter carries none.
# Without a state_class Home Assistant files them as text and will not graph or average them.
# object_id glob -> (unit, device_class, state_class)
NUMERIC_HINTS = [
    ("*_cpu_load", ("%", None, "measurement")),
    ("*_now_percent_kaco", ("%", None, "measurement")),
    ("*_days_since_*", ("d", None, "measurement")),
    ("*_charge_index", (None, None, "measurement")),
    ("*_inverter_power_factor", (None, "power_factor", "measurement")),
]

# Numeric, but identities rather than measurements: serial numbers, board revisions and
# firmware versions. Averaging a serial number is meaningless, so these stay plain sensors.
IDENTIFIER_PATTERNS = [
    "*serial_number*", "*_rev_number", "*revision_number*",
    "*version_number*", "*software_version*", "*_version_in_rd",
]


def numeric_hint(object_id, value, unit):
    """Decide unit/device_class/state_class for a decoded block field."""
    for pat, hint in NUMERIC_HINTS:
        if fnmatch.fnmatch(object_id, pat):
            return hint
    if unit:
        dclass, sclass = _UNIT_CLASS.get(unit, (None, None))
        return unit, dclass, sclass
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, None, None
    if any(fnmatch.fnmatch(object_id, pat) for pat in IDENTIFIER_PATTERNS):
        return None, None, None
    # Numeric with no unit and not an identity: still a measurement.
    return None, None, "measurement"


_UNIT_CLASS = {
    "kWh": ("energy", "total_increasing"), "kW": ("power", "measurement"),
    "W": ("power", "measurement"), "V": ("voltage", "measurement"),
    "A": ("current", "measurement"), "°C": ("temperature", "measurement"),
    "Hz": ("frequency", "measurement"), "h": ("duration", "total_increasing"),
    "%": (None, "measurement"),
}


def jinja_path(path, multiplier=1):
    """Dotted path to a Jinja expression; a numeric segment indexes a list."""
    expr = "value_json"
    for part in path.split("."):
        if part.isdigit():
            expr += f"[{part}]"            # list index
        elif part.isidentifier():
            expr += f".{part}"
        else:
            # Register names can start with a digit (5MinBattLoad), which is not a valid
            # attribute, so subscript it instead.
            expr += f"['{part}']"
    if multiplier == 1:
        return f"{{{{ {expr} }}}}"
    return f"{{{{ ({expr} * {multiplier}) if {expr} is not none else none }}}}"


def sensor_config(object_id, name, topic, path, device, unit, dclass, sclass, icon,
                  precision, multiplier=1, diagnostic=False, availability_extra=None):
    cfg = {
        "name": name,
        "object_id": object_id,
        "unique_id": f"solarbridge_{object_id}",
        "state_topic": topic,
        "value_template": jinja_path(path, multiplier),
        "device": device,
        "availability": [{"topic": f"{PREFIX}/status"}],
    }
    if unit: cfg["unit_of_measurement"] = unit
    if dclass: cfg["device_class"] = dclass
    if sclass: cfg["state_class"] = sclass
    if icon: cfg["icon"] = icon
    if precision is not None: cfg["suggested_display_precision"] = precision
    if diagnostic: cfg["entity_category"] = "diagnostic"
    if availability_extra:
        cfg["availability"] = cfg["availability"] + availability_extra
        cfg["availability_mode"] = "all"
    return cfg


def discovery_topic(component, device_id, object_id):
    """<component>/<device_id>/<object_id>/config, as zigbee2mqtt builds it."""
    return f"{DISCOVERY}/{component}/{device_id}/{object_id}/config"


def build_entities(snapshot):
    """Every entity, plus which bucket and path each reads from.

    Returns (discovery, routes); routes maps bucket -> [(object_id, path)] so the publisher
    knows which fields belong on which topic.
    """
    discovery, routes = [], {name: [] for name in BUCKETS}
    sp = snapshot.get("sp_pro") or {}
    dev_id, device = sppro_device(sp.get("unit"))

    for short, name, path, unit, dclass, sclass, icon, prec, bucket, mult in SPPRO_SENSORS:
        obj = f"{EPREFIX}_{short}" if EPREFIX else short
        if FAST_INTERVAL and short in FAST_FIELDS:
            # The fast window carries this one, so it reads from there instead. The payload is
            # flat, so the path is just the field name.
            bucket, path = FAST_BUCKET, FAST_FIELDS[short]
        if excluded(obj) or bucket not in routes:
            continue
        topic = f"{SPPRO_BASE}/{bucket}"
        extra = None
        if short == "clock_drift":
            # Only meaningful while the host clock it is measured against is disciplined.
            extra = [{"topic": topic,
                      "value_template": "{{ 'online' if value_json.inverter_clock_drift_seconds is defined"
                                        " and value_json.inverter_clock_drift_seconds is not none"
                                        " else 'offline' }}"}]
        discovery.append((discovery_topic("sensor", dev_id, obj),
                          sensor_config(obj, name, topic, path, device, unit, dclass, sclass,
                                        icon, prec, mult, availability_extra=extra)))
        routes[bucket].append((obj, path))

    obj = f"{EPREFIX}_bridge_clock_synced" if EPREFIX else "bridge_clock_synced"
    if not excluded(obj) and "state_60s" in routes:
        discovery.append((discovery_topic("binary_sensor", dev_id, obj), {
            "name": "Bridge Clock Synchronised",
            "object_id": obj,
            "unique_id": f"solarbridge_{obj}",
            "state_topic": f"{SPPRO_BASE}/state_60s",
            "value_template": "{{ 'ON' if (value_json.host_clock | default({}, true)).synchronized"
                              " | default(false, true) else 'OFF' }}",
            "device_class": "connectivity",
            "entity_category": "diagnostic",
            "device": device,
            "availability": [{"topic": f"{PREFIX}/status"}],
        }))
        routes["state_60s"].append((obj, "host_clock"))

    # Block fields, taken from whatever the bridge currently decodes rather than a fixed list.
    for block, blk in (sp.get("blocks") or {}).items():
        for field, spec in blk.get("fields", {}).items():
            if spec.get("value") is None:
                continue
            obj = f"{EPREFIX}_{slug(block)}_{slug(field)}" if EPREFIX else f"{slug(block)}_{slug(field)}"
            obj = obj[:60]
            if excluded(obj) or "blocks" not in routes:
                continue
            unit, dclass, sclass = numeric_hint(obj, spec.get("value"), spec.get("unit"))
            discovery.append((discovery_topic("sensor", dev_id, obj),
                              sensor_config(obj, f"{block} {field}", f"{SPPRO_BASE}/blocks",
                                            f"{block}.{field}", device, unit, dclass, sclass,
                                            None, None, diagnostic=True)))
            routes["blocks"].append((obj, f"{block}.{field}"))

    for f in snapshot.get("fronius") or []:
        name = f.get("name")
        fdev_id, fdevice = fronius_device(name, f.get("custom_name") or f"Fronius {name}")
        topic = f"{PREFIX}/fronius/{name}/state"
        for suffix, label, path, unit, dclass, sclass, prec, mult in FRONIUS_SENSORS:
            obj = f"fronius_{name}_{suffix}"
            if excluded(obj):
                continue
            discovery.append((discovery_topic("sensor", fdev_id, obj),
                              sensor_config(obj, label, topic, path, fdevice, unit, dclass,
                                            sclass, None, prec, mult)))
        for s in f.get("strings") or []:
            i = s.get("index")
            # (payload field, object_id suffix, label, ...). The suffix differs from the field
            # on purpose: it is part of unique_id, and renaming it would orphan the history.
            for field, suffix, flabel, unit, dclass, prec in [
                ("volts", "voltage", "Voltage", "V", "voltage", 1),
                ("amps", "current", "Current", "A", "current", 2),
                ("watts", "power", "Power", "W", "power", 0),
            ]:
                obj = f"fronius_{name}_string{i}_{suffix}"
                if excluded(obj):
                    continue
                discovery.append((discovery_topic("sensor", fdev_id, obj),
                                  sensor_config(obj, f"String {i} {flabel}", topic,
                                                f"strings.{i - 1}.{field}", fdevice, unit,
                                                dclass, "measurement", None, prec)))
    return discovery, routes


# --- payload assembly ------------------------------------------------------------------------

def dig(obj, path):
    for part in path.split("."):
        if obj is None:
            return None
        if part.isdigit() and isinstance(obj, list):
            obj = obj[int(part)] if int(part) < len(obj) else None
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


def put(target, path, value):
    """Place a value at a dotted path, growing dicts and lists as needed."""
    parts = path.split(".")
    for i, part in enumerate(parts[:-1]):
        nxt = parts[i + 1]
        child = [] if nxt.isdigit() else {}
        if isinstance(target, list):
            idx = int(part)
            while len(target) <= idx:
                target.append(None)
            if not isinstance(target[idx], (dict, list)):
                target[idx] = child
            target = target[idx]
        else:
            if not isinstance(target.get(part), (dict, list)):
                target[part] = child
            target = target[part]
    last = parts[-1]
    if isinstance(target, list):
        idx = int(last)
        while len(target) <= idx:
            target.append(None)
        target[idx] = value
    else:
        target[last] = value


def flatten_blocks(sp):
    out = {}
    for block, blk in (sp.get("blocks") or {}).items():
        for field, spec in blk.get("fields", {}).items():
            if spec.get("value") is not None:
                out[f"{block}.{field}"] = spec["value"]
    return out


def bucket_payload(sp, blocks_flat, routes, bucket):
    """Exactly the fields routed to this bucket, nested as the templates expect."""
    out = {}
    for object_id, path in routes[bucket]:
        value = blocks_flat.get(path) if bucket == "blocks" else dig(sp, path)
        put(out, path, quantize(object_id, value))
    return out


def heartbeat():
    """Record that a publish cycle got all the way through to a connected broker."""
    try:
        os.makedirs(os.path.dirname(HEARTBEAT), exist_ok=True)
        with open(HEARTBEAT, "w") as fh:
            fh.write(str(time.time()))
    except OSError:
        # Never let the health marker take down the thing it is reporting on.
        pass


class Target:
    """One publish destination: its own broker connection and its own de-duplication state.

    The state has to be per-target. With a single shared record of what was last sent, a target
    that happened to be disconnected when a value changed would never receive that value -- the
    next identical payload would be suppressed as a duplicate and the target would sit on a
    stale retained message indefinitely. Keeping the record per-target costs a dictionary and
    removes that whole class of bug.

    Adding a destination that is not MQTT means writing a class with the same four methods --
    connect, publish, publish_if_due and announce -- and returning it from build_targets. The
    publishing code above calls nothing else.
    """

    def __init__(self, name, host, port, username, password):
        self.name = name
        self.host, self.port = host, port
        # Kept for the retirement probe, which needs a second connection to this same broker.
        self.username, self.password = username, password
        self.connected = False
        self._last_sent = {}
        self._last_published = {}
        self._discovery = []
        # Distinct client_id per target: brokers evict an existing session when a second
        # connection claims the same id, so sharing one would make two targets fight.
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                   client_id=f"solar-bridge-{name}")
        self._client.username_pw_set(username, password)
        self._client.will_set(f"{PREFIX}/status", "offline", retain=True)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect

    def _on_connect(self, client, _userdata, _flags, reason, _props=None):
        """Re-announce on every connect, not just the first.

        A dropped link makes the broker publish our retained last-will, so Home Assistant sees
        "offline". paho reconnects by itself, but unless we say "online" again nothing clears
        that, and the entities stay unavailable while we publish state into the void.
        """
        if reason != 0:
            print(f"[{self.name}] connect failed: {reason}", file=sys.stderr, flush=True)
            return
        self.connected = True
        for topic, cfg in self._discovery:
            client.publish(topic, json.dumps(cfg), retain=True)
        client.publish(f"{PREFIX}/status", "online", retain=True)
        # Everything is resent after a reconnect: this broker may have been restarted and lost
        # its retained messages, and we cannot tell from here.
        self._last_sent.clear()
        self._last_published.clear()
        print(f"[{self.name}] connected: announced {len(self._discovery)} discovery configs",
              flush=True)

    def _on_disconnect(self, _client, _userdata, *args):
        self.connected = False
        print(f"[{self.name}] disconnected", file=sys.stderr, flush=True)

    def connect(self):
        self._client.connect(self.host, self.port, keepalive=60)
        self._client.loop_start()

    def announce(self, discovery):
        """Remember the entity set and publish it, so a later reconnect can replay it."""
        self._discovery = discovery
        for topic, cfg in discovery:
            self._client.publish(topic, json.dumps(cfg), retain=True)

    def publish(self, topic, payload, retain=True):
        self._client.publish(topic, payload, retain=retain)

    def publish_if_due(self, topic, payload, bucket, force):
        """Publish when the bucket's interval has elapsed and the payload actually changed."""
        now = time.time()
        if not force:
            if now - self._last_published.get(topic, 0) < BUCKETS.get(bucket, 0):
                return 0
            if self._last_sent.get(topic) == payload:
                return 0
        self._client.publish(topic, payload, retain=True)
        self._last_sent[topic] = payload
        self._last_published[topic] = now
        return 1

    def stop(self):
        self._client.publish(f"{PREFIX}/status", "offline", retain=True)
        self._client.loop_stop()
        self._client.disconnect()

    @property
    def client(self):
        """For the one caller that needs the raw client: stale-discovery retirement."""
        return self._client


def publish_if_due(targets, topic, payload, bucket, force):
    """Fan a payload out to every target. Returns how many actually sent it.

    A target that suppresses the payload as unchanged is not a failure, so the count is used
    only for the hourly summary.
    """
    return sum(t.publish_if_due(topic, payload, bucket, force) for t in targets)


def publish_fast(targets, routes, force):
    """One poll of the bridge's fast window, published to its own topic.

    Runs on its own thread so its cadence is independent of the main loop: a slow block sweep
    on the main poll must not delay a load spike. The bridge serialises its own inverter
    access, so this cannot collide with the main read -- it waits instead.
    """
    req = urllib.request.Request(BRIDGE.rstrip("/") + "/fast",
                                 headers={"User-Agent": "solar-bridge-mqtt-fast"})
    with urllib.request.urlopen(req, timeout=30) as r:
        fast = json.loads(r.read())
    payload = {}
    for object_id, path in routes[FAST_BUCKET]:
        put(payload, path, quantize(object_id, fast.get(path)))
    return publish_if_due(targets, f"{SPPRO_BASE}/{FAST_BUCKET}",
                          json.dumps(payload, sort_keys=True), FAST_BUCKET, force)


def fast_loop(targets, state):
    """Poll the fast window until shutdown, once the entity set exists."""
    last_forced, fails = 0.0, 0
    while running:
        routes = state["routes"]
        if routes and routes.get(FAST_BUCKET):
            try:
                force = time.time() - last_forced >= FORCE_EVERY
                if force:
                    last_forced = time.time()
                publish_fast(targets, routes, force)
                if fails:
                    print(f"fast path recovered after {fails} failures", flush=True)
                    fails = 0
            except Exception as e:
                fails += 1
                # The main loop owns the availability topic. A fast-path failure alone does not
                # mean the bridge is down, and marking everything offline over it would be worse
                # than briefly stale load figures.
                if fails in (1, 10) or fails % 100 == 0:
                    print(f"fast read failed ({fails}): {e}", file=sys.stderr, flush=True)
        time.sleep(FAST_INTERVAL)


def retire_stale_discovery(target, keep):
    """Clear retained discovery configs we no longer publish.

    The topic layout changed, so configs left at the previous addresses would keep their
    entities alive forever as duplicates. An empty retained payload is how MQTT discovery
    deletes one. Entities are keyed on unique_id, which is unchanged, so Home Assistant
    reuses the existing registry entry and the history survives.
    """
    stale = []

    def consider(_c, _u, m):
        # Only ever retire our own configs. The discovery prefix is shared with every other
        # integration on the broker, and an empty payload deletes whatever it lands on, so
        # membership is decided by our unique_id prefix appearing in the payload -- not by the
        # topic shape, which other integrations are free to use too.
        if not m.payload or m.topic in keep:
            return
        if b'"unique_id": "solarbridge_' in m.payload or b'"unique_id":"solarbridge_' in m.payload:
            stale.append(m.topic)

    probe = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="solar-bridge-retire")
    probe.username_pw_set(target.username, target.password)
    probe.on_message = consider
    # Both the old three-level layout and the current four-level one.
    probe.on_connect = lambda c, _u, _f, _r, _p=None: (c.subscribe(f"{DISCOVERY}/+/+/config"),
                                                       c.subscribe(f"{DISCOVERY}/+/+/+/config"))
    try:
        probe.connect(target.host, target.port, 30)
        probe.loop_start()
        time.sleep(8)
        probe.loop_stop()
        probe.disconnect()
    except Exception as e:
        print(f"could not scan for stale discovery configs: {e}", file=sys.stderr, flush=True)
        return 0
    for topic in stale:
        client.publish(topic, "", retain=True)
    if stale:
        print(f"retired {len(stale)} discovery config(s) from the previous layout", flush=True)
        # Let Home Assistant finish removing them before the replacements arrive. Re-announcing
        # a unique_id while its old entity is still registered gets the new one a "_2" suffix,
        # and a different entity_id is a different series as far as the recorder is concerned.
        time.sleep(10)
    return len(stale)


def build_targets():
    """Every destination to publish to, from config.

    Each target names an env file for its credentials rather than carrying them in
    publisher.json: that file is site configuration under version control's nose, and a broker
    password does not belong in it. With no "targets" key the single broker from MQTT_ENV is
    used, which is what every existing install has.
    """
    declared = CFG.get("targets") or [{"name": "home-assistant", "env": CONF}]
    targets = []
    for spec in declared:
        name = spec.get("name") or "target"
        env_path = spec.get("env", CONF)
        try:
            conf = load_env(env_path)
        except OSError as e:
            # A broken second target must not take down the first.
            print(f"[{name}] skipped: cannot read {env_path}: {e}", file=sys.stderr, flush=True)
            continue
        host = spec.get("host") or conf.get("MQTT_HOST", "127.0.0.1")
        port = int(spec.get("port") or conf.get("MQTT_PORT", "1883"))
        user = conf.get("MQTT_USERNAME", "")
        password = conf.get("MQTT_PASSWORD", "")
        if not user and not password:
            print(f"[{name}] warning: no credentials in {env_path}", file=sys.stderr, flush=True)
        targets.append(Target(name, host, port, user, password))
    return targets


def main():
    global _last_forced, _blocks_published_at

    targets = build_targets()
    if not targets:
        print("no usable publish targets; nothing to do", file=sys.stderr, flush=True)
        return 1

    state = {"discovery": [], "routes": None, "retired": False}

    for target in targets:
        try:
            target.connect()
        except OSError as e:
            # Keep going: paho retries in the background, and one unreachable broker should not
            # stop the others from being fed.
            print(f"[{target.name}] initial connect to {target.host}:{target.port} failed: {e}",
                  file=sys.stderr, flush=True)
    print(f"publishing to {len(targets)} target(s): "
          + ", ".join(f"{t.name} ({t.host}:{t.port})" for t in targets), flush=True)

    def stop(*_):
        global running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    if FAST_INTERVAL:
        threading.Thread(target=fast_loop, args=(targets, state), daemon=True).start()
        print(f"fast path: {len(FAST_FIELDS)} readings every {FAST_INTERVAL}s "
              f"on {SPPRO_BASE}/{FAST_BUCKET}", flush=True)

    fails = published = skipped = 0
    last_report = time.time()
    while running:
        try:
            # Blocks are needed on the first poll, to build the entity set, and thereafter only
            # when the blocks bucket is due to publish.
            blocks_topic = f"{SPPRO_BASE}/blocks"
            want_blocks = (state["routes"] is None
                           or time.time() - _blocks_published_at
                           >= BUCKETS.get("blocks", 0))
            url = BRIDGE + ("?blocks=true" if want_blocks else "")
            req = urllib.request.Request(url, headers={"User-Agent": "solar-bridge-mqtt"})
            with urllib.request.urlopen(req, timeout=90) as r:
                snap = json.loads(r.read())
            sp = snap.get("sp_pro") or {}
            sp["solar_total_watts"] = snap.get("solar_total_watts")

            if state["routes"] is None and sp:
                discovery, routes = build_entities(snap)
                state["discovery"], state["routes"] = discovery, routes
                for target in targets:
                    target.announce(discovery)
                if not state["retired"]:
                    # After announcing, not before: the scan then sees a settled broker that
                    # already holds everything we intend to keep, so whatever is left over is
                    # unambiguously stale. Scanning first raced our own publishes and once left
                    # a retired entity alive.
                    # Each broker holds its own retained configs, so each needs its own sweep.
                    for target in targets:
                        retire_stale_discovery(target, {t for t, _ in discovery})
                    state["retired"] = True
                counts = ", ".join(f"{b}={len(v)}" for b, v in routes.items())
                print(f"announced {len(discovery)} entities ({counts})", flush=True)

            force = (time.time() - _last_forced) >= FORCE_EVERY
            if force:
                _last_forced = time.time()

            sent = topics = 0
            if state["routes"]:
                blocks_flat = flatten_blocks(sp)
                for bucket in BUCKETS:
                    if not state["routes"][bucket]:
                        continue
                    if bucket == FAST_BUCKET:
                        # Owned by the fast thread, and its fields are not in this snapshot --
                        # publishing it from here would write nulls over good values.
                        continue
                    if bucket == "blocks" and not want_blocks:
                        # Publishing from an absent sweep would overwrite good values with nulls.
                        continue
                    topics += 1
                    if bucket == "blocks":
                        _blocks_published_at = time.time()
                    payload = bucket_payload(sp, blocks_flat, state["routes"], bucket)
                    sent += publish_if_due(targets, f"{SPPRO_BASE}/{bucket}",
                                           json.dumps(payload, sort_keys=True), bucket, force)

            for f in snap.get("fronius") or []:
                g = {k: v for k, v in f.items() if k != "taken_at"}
                if isinstance(g.get("status"), dict):
                    g["status"] = {k: v for k, v in g["status"].items() if k != "device_time"}
                topics += 1
                sent += publish_if_due(targets, f"{PREFIX}/fronius/{f['name']}/state",
                                       json.dumps(g, sort_keys=True), "state", force)
            published += sent
            skipped += topics - sent

            if fails:
                print(f"bridge recovered after {fails} failures", flush=True)
                for target in targets:
                    target.publish(f"{PREFIX}/status", "online")
            fails = 0
            # Only meaningful if the broker is actually there -- paho queues silently while
            # disconnected, so a publish call returning is not evidence of anything.
            # The heartbeat means "we reached at least one broker". The watchdog uses it to
            # decide whether the publish path works at all; one dead target out of two is a
            # problem for that target's consumer, not a reason to restart anything here.
            if any(t.connected for t in targets):
                heartbeat()
        except Exception as e:
            fails += 1
            print(f"bridge read failed ({fails}): {e}", file=sys.stderr, flush=True)
            if fails >= 2:
                for target in targets:
                    target.publish(f"{PREFIX}/status", "offline")

        if time.time() - last_report >= 3600:
            print(f"last hour: {published} publishes, {skipped} skipped", flush=True)
            published = skipped = 0
            last_report = time.time()

        for _ in range(INTERVAL):
            if not running:
                break
            time.sleep(1)

    for target in targets:
        target.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
