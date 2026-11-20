#!/bin/sh
# Turns the add-on options (/data/options.json, written by the Supervisor) into a
# `splink serve` command line. No bashio: the image is the plain .NET runtime.
set -eu

OPTIONS=/data/options.json
opt() { jq -r "$1 | if . == null then empty else tostring end" "$OPTIONS"; }
log() { echo "[splink-addon] $*"; }

set -- serve

case "$(opt .connection)" in
  tcp)
    host=$(opt .host)
    if [ -z "$host" ]; then
      log "connection is tcp but host is empty: set the adaptor's host name or IP"
      exit 1
    fi
    set -- "$@" --host "$host" --tcp-port "$(opt .tcp_port)"
    where="$host:$(opt .tcp_port)"
    ;;
  serial)
    port=$(opt .serial_port)
    set -- "$@" --port "${port:-auto}"
    baud=$(opt .baud)
    [ -n "$baud" ] && set -- "$@" --baud "$baud"
    where="serial ${port:-auto}"
    ;;
  *)
    log "unknown connection type"; exit 1 ;;
esac

set -- "$@" --http-port "$(opt .http_port)"

# Fronius: the inline list wins; otherwise a bridge.json in the add-on's config folder.
fronius=$(jq -r '[.fronius[]? | "\(.name)=\(.host)"] | join(",")' "$OPTIONS")
config=$(opt .bridge_config)
if [ -n "$fronius" ]; then
  set -- "$@" --fronius "$fronius"
  [ -n "$config" ] && log "fronius list is set, so bridge_config ($config) is ignored"
elif [ -n "$config" ]; then
  case "$config" in /*) ;; *) config="/config/$config" ;; esac
  if [ ! -r "$config" ]; then
    log "bridge_config $config not found (put it in this add-on's config folder)"
    exit 1
  fi
  set -- "$@" --config "$config"
else
  log "no Fronius inverters configured: serving the SP PRO only"
fi

for key in idle_timeout live_interval today_interval clock_interval sweep_interval log_interval fast_cache cache; do
  value=$(opt ".$key")
  [ -n "$value" ] && set -- "$@" "--$(echo "$key" | tr _ -)" "$value"
done

[ "$(opt .allow_raw)" = "true" ] && set -- "$@" --allow-raw
[ "$(opt .log_level)" = "debug" ] && set -- "$@" --verbose

# The SP PRO password goes by environment, never argv (argv is visible to `ps`).
password=$(opt .password)
if [ -n "$password" ]; then export SPLINK_PASSWORD="$password"; fi
unset password

log "splink $(/opt/splink/splink --version), SP PRO via $where"
log "splink $*"
exec /opt/splink/splink "$@"
