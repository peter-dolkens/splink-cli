#!/usr/bin/env python3
"""Recover the bridge unattended when the link to the inverter or the network wedges.

This board is a Pi 3B, where ethernet is not native: it hangs off the same single USB
controller as the SP PRO's serial adapter, while the SD card does not. So when that bus
wedges, the inverter link and the network die together and the OS carries on regardless --
systemd timers keep firing, the filesystem stays writable, and nothing recovers on its own.
That is what cost 19 hours on 2026-10-09: everything stopped at 10:21 and the box sat there
healthy-but-useless until it was power cycled by hand.

Rebooting is a blunt fix, so the trigger has to be narrow. "The MQTT push stopped" on its own
is not enough of a reason: the broker is remote, through a tunnel, so an ordinary Home
Assistant restart would look identical to a local fault and we would reboot the solar bridge
every five minutes for as long as HA was down. The two cases are told apart like this:

  bridge unreachable on loopback  -> our USB/serial is gone. Nothing upstream can cause this,
                                     and only a reboot clears it -- see USB_RESET_AFTER.
  bridge answers, readings fail   -> a software fault inside the bridge: the link and the
                                     network are fine, so restart the bridge service -- not
                                     the USB device, not the machine. Rate limited.
  bridge fine, MQTT stale         -> restart the publish path first, then look at the default
                                     gateway. Unreachable means our own network is gone (the
                                     same wedge, other half) and a reboot is warranted.
                                     Reachable means the fault is beyond this machine -- the
                                     broker or HA itself -- so log and wait.

Reboots are rate limited regardless, because a reboot loop is worse than being down: it would
destroy the on-disk block log and make the real cause harder to find.

Before it touches anything, it writes a diagnostic snapshot. This matters more than it looks:
Raspberry Pi OS ships Storage=volatile for the journal to spare the SD card, so every log of
the fault is destroyed by the very reboot we are about to perform -- which is why the 19-hour
outage left nothing to read. The journal does still exist in /run while the box is up, so
capturing it at fault time preserves the part that matters without journald writing to the
card continuously. One gzipped file per fault, newest twenty kept.
"""
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

BRIDGE = os.environ.get("BRIDGE_URL", "http://localhost:8080")
HEARTBEAT = os.environ.get("MQTT_HEARTBEAT", "/run/splink/mqtt-ok")
STATE = os.environ.get("WATCHDOG_STATE", "/var/lib/splink/watchdog-last-reboot")

CHECK_EVERY = int(os.environ.get("CHECK_EVERY", "60"))
# How long a fault must persist before we act. The bridge's own read schedule is slower than
# the check interval, so a single miss means nothing.
FAIL_SECONDS = int(os.environ.get("FAIL_SECONDS", "300"))
# Issuing USBDEVFS_RESET before rebooting was the original plan, on the assumption that a reset
# clears this wedge. The kernel says otherwise. From the 2026-10-29 snapshot:
#
#   WARN::dwc_otg_hcd_urb_dequeue:639: Timed out waiting for FSM NP transfer to complete  (x157)
#   usb 1-1-port3: cannot reset (err = -110)
#   ls: cannot access '/dev/ttyACM0': No such file or directory
#
# The reset path is itself timing out: -110 is ETIMEDOUT, and port 3 is where the SP PRO sits.
# A reset cannot succeed when the controller's transfer state machine is the thing that is
# wedged, which is why 2026-10-27 and 2026-10-29 both escalated to a reboot anyway. Attempting
# it only delays the one remedy that works, so it is off by default. Set it above zero to try
# again on a kernel where dwc_otg behaves differently.
USB_RESET_AFTER = int(os.environ.get("USB_RESET_AFTER", "0"))
# With no reset to attempt there is nothing to wait for, so the loopback fault reboots sooner
# than the other classes. Two minutes is still long enough to ride out a slow block sweep.
LOOPBACK_FAIL_SECONDS = int(os.environ.get("LOOPBACK_FAIL_SECONDS", "120"))
# A reboot every half hour at worst. If we are rebooting that often the fault is not one a
# reboot fixes, and continuing would just shred the logs that would show what it is.
MIN_REBOOT_GAP = int(os.environ.get("MIN_REBOOT_GAP", "1800"))
# A fault can leave /fast answering while every full reading fails. That cost 6.5 hours on
# 2026-10-12: after a reconnect, a stale handle inside the bridge failed every full read with
# "The port is closed.", the bridge served 503s, nothing reached MQTT, and this watchdog --
# looking only at /fast -- kept reporting that the inverter read fine. The serial link and the
# network were healthy, so the remedy is restarting the bridge service. Rate limited, so a
# fault a restart cannot fix costs one restart per gap rather than a loop on the inverter link.
RESTART_GAP = int(os.environ.get("RESTART_GAP", "1800"))
SPLINK_UNIT = os.environ.get("SPLINK_UNIT", "splink")
# The publish path, in the order it has to come up: the tunnel carries the broker connection,
# and the publisher sits on top of it.
#
# Waiting on the gateway test alone was wrong. On 2026-10-16 and 2026-10-20 publishing stopped
# for 9.8 and 12.4 hours while the gateway stayed reachable, so this declined to reboot -- which
# was correct, and the block logs for both days are full size, proving the bridge was reading
# the inverter the whole time. But declining to reboot was all it did. The fault was the tunnel
# or the publisher's broker connection, and a restart of those two is the remedy neither tier
# was offering.
MQTT_UNITS = [u for u in os.environ.get("MQTT_UNITS", "mqtt-tunnel,mqtt-publish").split(",") if u]
DIAG_DIR = os.environ.get("WATCHDOG_DIAG_DIR", "/var/lib/splink/diagnostics")
DIAG_KEEP = int(os.environ.get("WATCHDOG_DIAG_KEEP", "20"))
# A slow leak is invisible in a spot check and obvious in a trend, and the journal here is
# volatile, so there is nowhere else such a trend could accumulate. One short line per sample:
# ~150 a day, a few tens of KB, which the card will not notice.
SAMPLE_EVERY = int(os.environ.get("WATCHDOG_SAMPLE_EVERY", "600"))
SAMPLE_FILE = os.environ.get("WATCHDOG_SAMPLE_FILE", "/var/lib/splink/resources.csv")
SAMPLE_MAX_BYTES = int(os.environ.get("WATCHDOG_SAMPLE_MAX", "2000000"))
WATCHED_UNITS = ["splink", "mqtt-publish", "block-logger"]

SPPRO_USB = (0x04d8, 0x000a)   # Microchip CDC RS-232, as the SP PRO presents itself
USBDEVFS_RESET = ord('U') << 8 | 20

running = True


def log(msg):
    print(msg, flush=True)


def bridge_alive():
    """Can we still read the inverter? Loopback only, so nothing upstream can affect it."""
    try:
        req = urllib.request.Request(BRIDGE.rstrip("/") + "/fast",
                                     headers={"User-Agent": "splink-watchdog"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception:
        return False


def bridge_problem():
    """None if full readings work, otherwise what the bridge says is wrong with them.

    /health runs the same full read the publisher depends on, so it catches faults that leave
    /fast working. It is served from the bridge's short cache and interval-gated reads, so
    asking once a minute adds next to nothing on the inverter link.
    """
    try:
        req = urllib.request.Request(BRIDGE.rstrip("/") + "/health",
                                     headers={"User-Agent": "splink-watchdog"})
        with urllib.request.urlopen(req, timeout=30):
            return None
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read()).get("sp_pro") or f"HTTP {e.code}"
        except Exception:
            return f"HTTP {e.code}"
    except Exception as e:
        return f"health check failed: {e}"


def restart_units(units, why):
    """Restart a list of services in order, newest fault captured first."""
    capture(f"restarting {', '.join(units)}: {why}")
    for unit in units:
        r = subprocess.run(["systemctl", "restart", unit], capture_output=True, text=True)
        if r.returncode != 0:
            log(f"restart of {unit} failed ({r.returncode}): {r.stderr.strip()}")
        else:
            log(f"restarted {unit}")


def restart_bridge(why):
    capture(f"restarting {SPLINK_UNIT}: {why}")
    log(f"RESTARTING {SPLINK_UNIT}: {why}")
    r = subprocess.run(["systemctl", "restart", SPLINK_UNIT], capture_output=True, text=True)
    if r.returncode != 0:
        log(f"restart of {SPLINK_UNIT} failed ({r.returncode}): {r.stderr.strip()}")


def mqtt_fresh():
    """The publisher touches its heartbeat whenever a cycle completes with the broker up."""
    try:
        return time.time() - os.stat(HEARTBEAT).st_mtime < FAIL_SECONDS
    except FileNotFoundError:
        return False
    except OSError:
        return False


def gateway_reachable():
    """Our own default gateway. This is what separates 'our network died' from 'HA is down'."""
    try:
        route = subprocess.run(["ip", "route", "show", "default"],
                               capture_output=True, text=True, timeout=10).stdout.split()
        gw = route[route.index("via") + 1]
    except Exception:
        return True          # cannot tell -> assume upstream, which is the cautious answer
    try:
        with socket.create_connection((gw, 53), timeout=5):
            return True
    except OSError:
        pass
    return subprocess.run(["ping", "-c1", "-W2", gw],
                          capture_output=True).returncode == 0


def usb_reset():
    """Ask the kernel to re-enumerate the SP PRO. Known to clear this wedge without a reboot."""
    for bus in sorted(os.listdir("/dev/bus/usb")):
        busdir = f"/dev/bus/usb/{bus}"
        if not os.path.isdir(busdir):
            continue
        for dev in sorted(os.listdir(busdir)):
            path = f"{busdir}/{dev}"
            try:
                # rb+ because the same handle has to read the descriptor and take the ioctl.
                with open(path, "rb+") as fh:
                    # Match on the descriptor rather than a fixed device number: the address
                    # changes across re-enumeration, so a hardcoded path goes stale.
                    desc = fh.read(18)
                    if len(desc) < 12:
                        continue
                    vid = desc[8] | desc[9] << 8
                    pid = desc[10] | desc[11] << 8
                    if (vid, pid) != SPPRO_USB:
                        continue
                    fcntl.ioctl(fh, USBDEVFS_RESET, 0)
                    log(f"USB reset issued to {path}")
                    return True
            except Exception as e:
                log(f"USB reset on {path} failed: {e}")
    log("SP PRO not found on the USB bus -- it has gone entirely, so a reset has nothing to act on")
    return False


def unit_pid(unit):
    try:
        out = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", unit],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        pid = int(out)
        return pid or None
    except Exception:
        return None


def proc_stats(pid):
    """RSS in kB, thread count and open descriptors for one process."""
    rss = threads = fds = ""
    try:
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    rss = line.split()[1]
                elif line.startswith("Threads:"):
                    threads = line.split()[1]
    except OSError:
        pass
    try:
        fds = str(len(os.listdir(f"/proc/{pid}/fd")))
    except OSError:
        pass
    return rss, threads, fds


def sample():
    """Append one row of resource counters, so a leak shows up as a trend rather than a guess.

    Deliberately cheap and deliberately boring: if this ever becomes expensive or clever it
    will be the thing that destabilises the box it is meant to be watching.
    """
    row = [time.strftime("%Y-%m-%dT%H:%M:%S")]
    for unit in WATCHED_UNITS:
        pid = unit_pid(unit)
        if pid is None:
            row += ["", "", ""]
            continue
        row += list(proc_stats(pid))
    try:
        with open("/proc/meminfo") as fh:
            avail = next((l.split()[1] for l in fh if l.startswith("MemAvailable:")), "")
    except OSError:
        avail = ""
    row.append(avail)
    try:
        # Rising USB errors would point at the shared controller rather than at our code.
        errs = subprocess.run(["dmesg", "--level=err,warn"], capture_output=True,
                              text=True, timeout=15).stdout.lower()
        row.append(str(sum(1 for l in errs.splitlines() if "usb" in l or "dwc_otg" in l)))
    except Exception:
        row.append("")

    header = "when," + ",".join(f"{u}_rss_kb,{u}_threads,{u}_fds" for u in WATCHED_UNITS) \
             + ",mem_available_kb,usb_errors"
    try:
        os.makedirs(os.path.dirname(SAMPLE_FILE), exist_ok=True)
        if os.path.exists(SAMPLE_FILE) and os.path.getsize(SAMPLE_FILE) > SAMPLE_MAX_BYTES:
            os.replace(SAMPLE_FILE, SAMPLE_FILE + ".1")
        fresh = not os.path.exists(SAMPLE_FILE)
        with open(SAMPLE_FILE, "a") as fh:
            if fresh:
                fh.write(header + "\n")
            fh.write(",".join(row) + "\n")
    except OSError as e:
        log(f"could not record resource sample: {e}")


def capture(reason):
    """Write everything we will wish we had, before recovery destroys it.

    Each command is given a short timeout and its failure recorded rather than raised: this
    runs when the machine is already unwell, and a snapshot that stops halfway is worth less
    than one that notes what it could not collect.
    """
    stamp = time.strftime("%Y-%m-%dT%H-%M-%S")
    path = os.path.join(DIAG_DIR, f"fault-{stamp}.txt.gz")
    probes = [
        ("reason", None),
        ("date", ["date", "-Is"]),
        ("uptime", ["uptime"]),
        ("throttled", ["vcgencmd", "get_throttled"]),       # undervoltage, the usual Pi culprit
        ("memory", ["free", "-h"]),
        ("disk", ["df", "-h"]),
        ("top processes", ["ps", "-eo", "pid,ppid,rss,pcpu,etime,comm", "--sort=-rss"]),
        ("services", ["systemctl", "status", "--no-pager", "--lines=0",
                      "splink", "mqtt-publish", "block-logger", "mqtt-tunnel"]),
        ("usb devices", ["lsusb"]),
        ("usb tree", ["lsusb", "-t"]),
        ("tty devices", ["ls", "-l", "/dev/ttyACM0", "/dev/ttyUSB0"]),
        ("ip addr", ["ip", "-br", "addr"]),
        ("ip route", ["ip", "route"]),
        ("arp", ["ip", "neigh"]),
        ("dmesg tail", ["dmesg", "--ctime", "--level=err,warn,crit", "--notime"]),
        # The journal is volatile here, so this is the only chance to keep it.
        ("journal this boot (tail)", ["journalctl", "-b", "--no-pager", "-n", "1500"]),
        ("journal: our services", ["journalctl", "-b", "--no-pager", "-n", "400",
                                   "-u", "splink", "-u", "mqtt-publish",
                                   "-u", "block-logger", "-u", "mqtt-tunnel"]),
    ]
    try:
        os.makedirs(DIAG_DIR, exist_ok=True)
        import gzip
        with gzip.open(path, "wt", encoding="utf-8", errors="replace") as fh:
            for title, cmd in probes:
                fh.write(f"\n===== {title} =====\n")
                if cmd is None:
                    fh.write(reason + "\n")
                    continue
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
                    fh.write(r.stdout or "")
                    if r.stderr:
                        fh.write("[stderr] " + r.stderr)
                except Exception as e:
                    fh.write(f"[could not collect: {e}]\n")
        os.sync()          # the next step may be a reboot; do not leave this in page cache
        log(f"diagnostics written to {path}")
    except Exception as e:
        log(f"could not write diagnostics: {e}")
        return

    try:
        kept = sorted(f for f in os.listdir(DIAG_DIR) if f.startswith("fault-"))
        for stale in kept[:-DIAG_KEEP]:
            os.remove(os.path.join(DIAG_DIR, stale))
    except Exception as e:
        log(f"could not prune diagnostics: {e}")


def may_reboot():
    try:
        with open(STATE) as fh:
            last = float(fh.read().strip())
    except Exception:
        return True
    gap = time.time() - last
    if gap < MIN_REBOOT_GAP:
        log(f"would reboot, but the last one was {gap / 60:.0f} min ago "
            f"(minimum gap {MIN_REBOOT_GAP / 60:.0f} min) -- holding")
        return False
    return True


def reboot(why):
    if not may_reboot():
        return
    # The reboot wipes the volatile journal, so this is the last moment it can be kept.
    capture(f"about to reboot: {why}")
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w") as fh:
            fh.write(str(time.time()))
        os.sync()
    except Exception as e:
        log(f"could not record the reboot time: {e}")
    log(f"REBOOTING: {why}")
    subprocess.run(["systemctl", "reboot"])


def main():
    def stop(*_):
        global running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    reset_note = (f"USB reset attempted at {USB_RESET_AFTER}s" if USB_RESET_AFTER > 0
                  else "no USB reset -- the kernel cannot reset this wedge")
    log(f"watchdog up: checking every {CHECK_EVERY}s. "
        f"Loopback fault: reboot after {LOOPBACK_FAIL_SECONDS}s ({reset_note}). "
        f"Degraded readings or stale MQTT: act after {FAIL_SECONDS}s. "
        f"Restarts: {SPLINK_UNIT} for degraded readings, {', '.join(MQTT_UNITS)} for stale MQTT, "
        f"at most one per {RESTART_GAP / 60:.0f} min each. "
        f"Reboots at most one per {MIN_REBOOT_GAP / 60:.0f} min. "
        f"Resource sample every {SAMPLE_EVERY}s to {SAMPLE_FILE}.")

    bridge_bad_since = None
    mqtt_bad_since = None
    reset_tried = False
    degraded_since = None
    last_restart = None
    hold_logged = False
    last_mqtt_restart = None
    mqtt_hold_logged = False
    last_sample = 0.0

    while running:
        now = time.time()

        if now - last_sample >= SAMPLE_EVERY:
            sample()
            last_sample = now

        if bridge_alive():
            if bridge_bad_since:
                log(f"bridge recovered after {now - bridge_bad_since:.0f}s")
            bridge_bad_since, reset_tried = None, False
        else:
            bridge_bad_since = bridge_bad_since or now
            down = now - bridge_bad_since
            if down >= LOOPBACK_FAIL_SECONDS:
                reboot(f"bridge unreachable on loopback for {down:.0f}s")
            elif USB_RESET_AFTER > 0 and down >= USB_RESET_AFTER and not reset_tried:
                log(f"bridge unreachable for {down:.0f}s -- capturing, then trying a USB reset")
                capture(f"bridge unreachable on loopback for {down:.0f}s")
                usb_reset()
                reset_tried = True

        # The bridge answers, but can it still produce a full reading? Only asked while /fast
        # works; if it does not, the branch above owns the fault.
        problem = None
        if bridge_bad_since is None:
            problem = bridge_problem()
            if problem is None:
                if degraded_since:
                    via = " (after a restart)" if last_restart and last_restart >= degraded_since else ""
                    log(f"bridge readings recovered after {now - degraded_since:.0f}s{via}")
                degraded_since, hold_logged = None, False
            else:
                degraded_since = degraded_since or now
                bad = now - degraded_since
                if bad >= FAIL_SECONDS:
                    if last_restart is not None and now - last_restart < RESTART_GAP:
                        if not hold_logged:
                            log(f"bridge readings failing for {bad:.0f}s ({problem}) but "
                                f"{SPLINK_UNIT} was restarted {(now - last_restart) / 60:.0f} min ago "
                                f"(minimum gap {RESTART_GAP / 60:.0f} min) -- holding")
                            hold_logged = True
                    else:
                        restart_bridge(f"/fast answers but full readings have failed for "
                                       f"{bad:.0f}s: {problem}")
                        last_restart, hold_logged = now, False
                        # degraded_since is kept, so recovery is logged against the fault's start;
                        # RESTART_GAP alone stops a second restart before this one has had time.
        else:
            degraded_since = None

        # Only meaningful while the bridge itself is fine; otherwise the branch above owns it.
        if bridge_bad_since is None:
            if mqtt_fresh():
                if mqtt_bad_since:
                    via = " (after a restart)" if last_mqtt_restart and last_mqtt_restart >= mqtt_bad_since else ""
                    log(f"MQTT publishing recovered after {now - mqtt_bad_since:.0f}s{via}")
                mqtt_bad_since, mqtt_hold_logged = None, False
            else:
                mqtt_bad_since = mqtt_bad_since or now
                stale = now - mqtt_bad_since
                if stale >= FAIL_SECONDS:
                    if problem:
                        # A bridge-level fault starves the publisher of data. Its own tier owns
                        # the remedy; restarting the publish path here would fix nothing.
                        if not mqtt_hold_logged:
                            log(f"MQTT stale {stale:.0f}s because the bridge's readings are failing "
                                f"({problem}) -- the bridge restart handles that.")
                            mqtt_hold_logged = True
                    elif not gateway_reachable():
                        # Our own network is gone, which is the other half of the USB wedge.
                        # Restarting the tunnel cannot help when the interface is dead.
                        reboot(f"MQTT stale {stale:.0f}s and the default gateway is unreachable")
                    elif last_mqtt_restart is None or now - last_mqtt_restart >= RESTART_GAP:
                        # The gateway answers and the inverter reads fine, so what is broken is
                        # between here and the broker: the tunnel, or the publisher's connection
                        # to it. Restart both rather than waiting for it to cure itself, which is
                        # what cost 9.8 and 12.4 hours in October.
                        restart_units(MQTT_UNITS,
                                      f"MQTT stale {stale:.0f}s; gateway answers and the bridge "
                                      f"reads fine, so the publish path is the fault")
                        last_mqtt_restart, mqtt_hold_logged = now, False
                    elif not mqtt_hold_logged:
                        log(f"MQTT stale {stale:.0f}s but {', '.join(MQTT_UNITS)} were restarted "
                            f"{(now - last_mqtt_restart) / 60:.0f} min ago (minimum gap "
                            f"{RESTART_GAP / 60:.0f} min) -- holding. If this persists the fault "
                            f"is beyond this machine.")
                        mqtt_hold_logged = True

        for _ in range(CHECK_EVERY):
            if not running:
                break
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
