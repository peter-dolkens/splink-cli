# splink add-on

Home Assistant add-on for [splink-cli](https://github.com/peter-dolkens/splink-cli): a
read-only bridge for Selectronic SP PRO inverters that serves a JSON API and Prometheus
`/metrics` to other add-ons, over USB serial or a serial-to-TCP adaptor, with optional
Fronius inverters.

## Install

1. Settings → Add-ons → Add-on Store → ⋮ → **Repositories** → add
   `https://github.com/peter-dolkens/splink-cli`.
2. Install **splink**, set the connection on the Configuration tab, and start it.
3. Other add-ons read it at `http://<hostname>:8080/` (the hostname is on the Info page).

See the Documentation tab for the options, and for moving from a standalone splink.

Not affiliated with Selectronic or Fronius. "SP PRO", "SP LINK" and "Fronius" are the
trademarks of their respective owners.
