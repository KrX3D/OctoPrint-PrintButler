# Design notes / lessons learned

Working notes from building PrintButler, kept so the reasoning behind
non-obvious decisions doesn't have to be rediscovered later. Not a changelog -
see the PR history for that.

## MQTT

- **Reuse OctoPrint's own MQTT plugin, don't manage a broker connection.**
  `octoprint.plugin.manager.get_helpers("mqtt", "mqtt_publish", "mqtt_subscribe",
  "mqtt_unsubscribe")` gives you the already-configured connection from
  `OctoPrint/OctoPrint-MQTT`. No separate credentials, no second connection to
  the same broker.
- **There's no public "am I connected" helper.** `mqtt_publish(..., allow_queueing=False)`
  returns `False` immediately if the broker link is down, so a throwaway
  non-retained publish doubles as a connectivity probe without reaching into
  the other plugin's private state.
- **Zigbee2MQTT publishes a full JSON state object, not just the field that
  changed.** A naive substring search for `"ON"` will false-positive on
  unrelated fields (`"indicator_mode":"off/on"` contains `"on"`). Parse JSON
  and compare a specific key (`state` by default, configurable) instead.
- **Zigbee2MQTT devices republish their whole state on every minor sensor
  tick** (power/voltage/energy readings drift constantly on metered plugs),
  not just on real on/off changes. Any handler reacting to a state topic
  needs to check whether the *decoded value* actually changed before doing
  anything - otherwise you get a publish storm from a device that's just
  idling. Same applies to log lines: log on change, not on every message.
- **Zigbee2MQTT publishes an optimistic state update immediately on
  receiving a `/set` command, then a second confirmed update once the
  physical device responds.** Mesh jitter between those two can make a
  "did my command actually stick" check see a transient stale reading and
  retrigger itself. Debounce any self-heal/re-assert logic (a few seconds is
  enough) rather than reacting to every single message.
- **Don't retain `/set` (command) topics** - only state topics should be
  retained. A retained command gets replayed to the device on every broker
  reconnect/Zigbee2MQTT restart, which is rarely what you want.
- **A retained trigger/command topic needs an explicit reset, or it gets
  stuck.** If you publish "on" once and never publish "off" again, a fresh
  subscriber (or a restart of whatever's watching it) sees a permanently
  stale "on". Reset it back to "off" on your own next startup if nothing
  else will.

## Safe shutdown

- **A host cannot reliably cut its own mains power.** Once a shutdown
  command runs, the process issuing it (and its MQTT connection) can die at
  any point during the sequence - there's no way to guarantee an in-process
  "wait N seconds then cut power" step actually completes. The only robust
  design: publish a trigger topic *before* shutting down, and let something
  that stays alive independently of this host (e.g. a Home Assistant
  automation) confirm the host is actually down before cutting power.
- **"Is this printer powered on" doesn't need MQTT if the plugin runs on the
  same host the plug powers.** OctoPrint being alive already answers that
  question locally and instantly - subscribing to the plug's own state topic
  to ask a question you can answer for free is pointless indirection, and
  couples a purely-local fact to network/broker reliability. This only
  applies to *this* printer, though - a peer printer's state genuinely can't
  be known without MQTT.
- **A temperature-based auto-trigger must distinguish "already cold" from
  "just cooled down".** Watching for "temperature below X while idle" and
  firing as soon as that's true will false-trigger on every routine restart
  where the printer happens to already be idle and cold (e.g. a reboot for a
  plugin update, no print involved) - not just after a real print. Require
  having observed a hot reading since the watch last armed before a cool
  reading is allowed to start any countdown.

## OctoPrint plugin mechanics

- **jQuery treats any non-2xx HTTP response as a failure**, routing it to
  `.fail()` instead of `.done()` - even if the response body is a
  well-formed `{"success": false, "message": "..."}`. If you want the
  frontend to show the real reason for an "expected" failure (missing
  config, already running, etc.), return a plain 200 with the message in the
  body. Reserve non-2xx for genuinely unexpected/invalid requests.
- **Navbar plugin templates must NOT be wrapped in `<li>`.** OctoPrint core's
  own bundled navbar plugins (`announcements`, `health_check`) use a bare
  `<a class="pull-right">`. Wrapping in `<li>` breaks the DOM structure
  enough to interfere with both layout and Knockout bindings.
- **No tagged GitHub releases means the Plugin Manager's "Update" button
  never finds anything**, since `get_update_information()` (`type:
  github_release`) compares against release tags, not commits. Until this
  repo starts tagging releases, updates only actually land via **Reinstall**
  (which re-fetches the archive URL regardless of version) - "Update" will
  silently do nothing. Bump `__plugin_version__` on every PR anyway so the
  settings page's "Version:" label gives an unambiguous way to confirm a
  reinstall actually picked up new code.
- **A `ko.computed` that short-circuits before reading an observable never
  subscribes to it**, and won't re-evaluate when that observable later
  changes. Read every observable your computed depends on unconditionally
  (or guard the whole thing so the short-circuit only ever happens once,
  before any real data exists) rather than early-returning past one.
