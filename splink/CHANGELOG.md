# Changelog

## 0.1.0
- First release: `splink serve` as a Home Assistant add-on. Connects over a serial-to-TCP
  adaptor or USB serial, serves the JSON API and `/metrics` on the add-on network at port
  8080 (not published on the host by default), with optional Fronius inverters from the
  options or a `bridge.json` in the add-on config folder.
- Built on the device from the source commit pinned in `build.yaml`.
- splink now handles SIGTERM like Ctrl-C, so stopping the add-on releases the SP PRO
  session cleanly.
