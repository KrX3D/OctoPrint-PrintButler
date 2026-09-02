# OctoPrint-PrintButler

Print-finished notifications, light/plug automation, and safe shutdown - driven by OctoPrint's own events over MQTT, fully configurable from the settings UI. No more Home Assistant automations polling `print_progress` sensors.

## Why

Originally this logic lived in a handful of Home Assistant automations and scripts: a "print finished" flash/notification, keeping a work light in sync with which printer is powered on, and a safe-shutdown script that pressed OctoPrint's shutdown button and then cut mains power. PrintButler moves all of that into OctoPrint itself:

- Print-finished detection uses OctoPrint's native `PrintDone` event instead of polling a progress sensor and comparing `printTimeLeft`.
- Safe shutdown runs the shutdown command directly on the OctoPrint host instead of pressing a virtual button and polling for the host to go offline.
- Everything else (which switches to flip, which topics to use) is MQTT-based and fully configurable, since your relays/plugs/WLED are managed elsewhere (Zigbee2MQTT, Tasmota, etc.) and already have topics.

## Requirements

PrintButler does **not** manage its own MQTT broker connection - it reuses OctoPrint's own **[MQTT](https://plugins.octoprint.org/plugins/mqtt/)** plugin (`OctoPrint/OctoPrint-MQTT`). Install and configure that plugin first (broker host, credentials); PrintButler picks up its connection automatically via the plugin helper API. The Status tab shows whether the connection was found.

Each PrintButler instance controls **one printer** (the OctoPrint instance it's installed on). If you run multiple printers/OctoPrint instances, install PrintButler on each and point the "peer" fields at each other's topics - see the Shared Light tab below.

## Features

- **Print Finished Notification** - publishes a configurable MQTT payload (e.g. to trigger a WLED effect) when a print completes, auto-reverting after a delay
- **Quiet Hours** - independent windows for the finish notification and the finish light (both can cross midnight)
- **Finish Indicator Light** - a per-printer light/relay switched on when that printer finishes
- **Shared Work Light** - a light/relay shared across printers, kept on while this printer (i.e. this OctoPrint instance is running) or any configured peer is powered, with optional self-heal if something else switches it off
- **Safe Shutdown** - watches bed/nozzle temperature and, once cooled down after having been hot, publishes an MQTT trigger topic and runs a shutdown command on the host - actually cutting mains power is left to an external automation (e.g. Home Assistant) watching that topic, since a host can't reliably cut its own power
- **Live log viewer** and status panel in the settings UI
- **Enable / disable** switch without uninstalling

---

## Installation

Install via OctoPrint's Plugin Manager using this URL:

```
https://github.com/KrX3D/OctoPrint-PrintButler/archive/refs/heads/main.zip
```

Or clone and install manually:

```bash
cd ~/
git clone https://github.com/KrX3D/OctoPrint-PrintButler.git
~/oprint/bin/pip install -e OctoPrint-PrintButler
```

**To update:** every merge that bumps `__plugin_version__` automatically gets
a matching tagged GitHub Release (see `.github/workflows/tag-release-on-version-bump.yml`),
so OctoPrint's Plugin Manager **Update** button should just work - check
Settings -> Software Update, or use Plugin Manager -> PrintButler -> Update.
**Reinstall** also still works as a fallback (always re-fetches the archive
URL above regardless of version). Either way, fully restart OctoPrint (not
just the browser) and hard-refresh the page (Ctrl+Shift+R) afterward - the
"Version:" label at the top of PrintButler's settings should match the
version in this repo's `octoprint_printbutler/__init__.py`.

---

## Settings

Open OctoPrint -> Settings -> **PrintButler**.

| Tab | What it controls |
|-----|-------------------|
| **Print Finished** | Notification topic/payload on print completion, plus its own quiet hours |
| **Finish Light** | Per-printer indicator light triggered on finish, plus its own quiet hours |
| **Shared Light** | Cross-printer work light, peer topics, optional self-heal |
| **Safe Shutdown** | Bed/nozzle thresholds, shutdown command, MQTT trigger topic - fires automatically once cooled down after being hot, no manual trigger |
| **Status / Log** | MQTT connection status, live values, action log |

All MQTT topics/payloads are freeform text fields - point them at whatever your Zigbee2MQTT/Tasmota/WLED setup already uses.

See [LEARNINGS.md](LEARNINGS.md) for the reasoning behind some of the less obvious design decisions (MQTT chattiness pitfalls, why safe shutdown can't cut its own power, etc.).

## License

MIT
