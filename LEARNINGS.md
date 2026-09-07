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
- **A failed shutdown command must roll back the state it already changed.**
  `_do_safe_shutdown` marks this printer inactive and publishes the "going
  down" trigger *before* actually running the shutdown command (see above -
  the trigger has to leave the host while it's still up). If `Popen` itself
  raises (bad command, missing binary, no sudo), the host never actually
  shuts down, so leaving `_this_printer_active` false and the trigger topic
  "on" is actively dangerous: an automation watching that topic would cut
  mains power to a printer that's still running. Revert both on failure.

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
- **A plugin's `type: "sidebar"` template can't be inserted inside an
  existing core panel** (e.g. between the State panel's progress bar and its
  Print/Pause/Cancel buttons) - plugin sidebar sections are always appended
  below the built-in Connection/State/Files ones, as their own separate
  accordion section. The only way to land content literally inside a core
  panel is `replaces`, which means reimplementing that panel's entire markup
  and behavior yourself and keeping it in sync with OctoPrint core forever.
  Not worth it for one checkbox - a small dedicated sidebar section is the
  safe choice (the instance owner can still drag it wherever they want via
  Settings -> Appearance -> Sidebar).
- **A Knockout `checked` binding on a real `<input type="checkbox">` has
  already flipped by the time a same-element `click` handler runs.** Don't
  compute the new desired state as `!observable()` inside that handler (a
  leftover pattern from a plain non-checkbox click target like the navbar
  icon) - read `event.target.checked` directly instead, and roll the
  observable back in `.fail()` if the API call the click triggered doesn't
  confirm it.
- **`type: github_release` compares against tagged releases, not commits on
  main.** A version bump alone does nothing for the "Update" button unless a
  matching GitHub Release is *also* tagged (bare semver, no `v` prefix -
  checked against [jneilliii/OctoPrint-ActiveFiltersExtended](https://github.com/jneilliii/OctoPrint-ActiveFiltersExtended),
  a real working example: tags `0.1.0`, `0.0.2`, no `v`). Automated by
  `.github/workflows/tag-release-on-version-bump.yml`, which tags+releases
  on every push to `main` that touches `octoprint_printbutler/__init__.py`
  (skipping if a release for that version already exists) - so bumping
  `__plugin_version__` is now sufficient on its own again. Before that
  workflow existed, releases had to be created by hand and were easy to
  forget, which is exactly what silently broke "Update" for a while.
- **The `pip=` URL template's placeholder is `{target_version}`, not
  `{target}`.** OctoPrint's software update checker substitutes
  `{target_version}` with the release tag when constructing the install
  command; a template containing `{target}` instead just breaks silently (or
  errors) since that key is never provided. This was wrong here for multiple
  versions before being caught - always diff `get_update_information()`
  against a known-working plugin (like the one linked above) rather than
  trusting it because it "looks right".
- **A `ko.computed` that short-circuits before reading an observable never
  subscribes to it**, and won't re-evaluate when that observable later
  changes. Read every observable your computed depends on unconditionally
  (or guard the whole thing so the short-circuit only ever happens once,
  before any real data exists) rather than early-returning past one.
  This bit `autoShutdownFeatureEnabled` for real: it guarded with
  `self.settings && self.settings.shutdown_enabled()`, where `self.settings`
  is only assigned later in `onBeforeBinding` - so on the computed's first,
  eager evaluation (during construction, `self.settings` still `null`) the
  `&&` short-circuited before `shutdown_enabled()` was ever called, and the
  computed locked onto `false` forever, regardless of the actual setting.
- **Don't "fix" that by reading `settingsViewModel.settings.plugins.<id>`
  directly at construction time either** - that was the first attempt here,
  and it made things much worse. A `ko.computed` evaluates its function
  *immediately*, synchronously, during construction, to discover its
  dependencies - and `settingsViewModel.settings.plugins.<id>` isn't
  guaranteed populated yet at that point (it's filled in later, from an
  async request). Reading into it throws, and since the whole viewmodel
  constructor runs as one synchronous block, that exception aborts building
  the *entire* viewmodel - every template this plugin has (settings,
  navbar, sidebar) is left with `data-bind` attributes that never got
  wired to anything. From the user's side this looked exactly like "all my
  settings got reset": checkboxes in the settings dialog still render
  (raw, unbound HTML defaults to unchecked) and *look* interactive, but
  toggling them does nothing to the real observable, so whatever was saved
  before keeps getting sent back unchanged on every Save. The actual fix:
  create the computed inside `onBeforeBinding` instead of in the
  constructor - OctoPrint guarantees that hook only runs once the settings
  data genuinely exists (that's the entire reason `self.settings` itself
  gets assigned there rather than at construction), so by then reading
  `self.settings.shutdown_enabled()` unconditionally is both safe and
  correctly reactive.
- **Don't bind a sidebar checkbox two-way onto `settings.<key>` plus a
  `click` handler calling `OctoPrint.settings.save(...)` to persist it
  immediately.** This looked like the obvious way to let a sidebar control
  edit a real plugin setting without waiting for the Settings dialog's Save
  button - reuse the same shared observable `settingsViewModel` already
  owns, then push just that one key via a partial `OctoPrint.settings.save`
  patch. In practice it was unreliable: clicking the checkbox worked once,
  then further clicks stopped taking effect, and the Settings dialog could
  end up showing a different value than what was actually persisted.
  `OctoPrint.settings.save()`'s response causes OctoPrint core to remap the
  *entire* settings tree back onto the shared observables
  (`ko.mapping.fromJS(response, self.settings)`), so a save triggered from
  outside the normal Settings-dialog flow can race with or clobber the very
  value it just set, and everything bound to that shared observable (both
  the sidebar and the Settings dialog) inherits whatever confusion results.
  The reliable fix: give the sidebar control its own plugin-owned
  observable (`ko.observable`, not `settings.<key>`) and a dedicated
  `SimpleApiPlugin` command that explicitly does
  `self._settings.set_boolean([...], value); self._settings.save()`
  server-side - the exact same pattern this plugin already used for `armed`
  before this setting existed. Sync the Settings-dialog's own observable
  from that command's response (and from a `send_plugin_message` push, for
  other open tabs) only on a confirmed, explicit change - never from the
  periodic status-refresh poll, which would otherwise stomp an unsaved
  in-progress edit in the Settings dialog every few seconds.
- **Only delete local files, never SD-card ones, from a "delete after
  print" style feature.** `self._file_manager.remove_file()` only handles
  local storage; removing an SD-card file needs a different call
  (`self._printer.delete_sd_file()`) with different failure modes (printer
  must be connected, etc.). Not worth the extra complexity/risk for a
  cleanup convenience feature - skip and log if the finished print's
  `origin` isn't `FileDestinations.LOCAL`.
- **`self._settings.set_xxx(path, value)` silently deletes the key instead
  of storing it whenever `value` equals that key's default** (that's what
  `force=False`, the default, does - it's how OctoPrint keeps `config.yaml`
  free of redundant "explicitly set to the default" entries). For a toggle
  whose default is `False`, this means every "turn it off" call takes a
  structurally different code path through the settings layer than every
  "turn it on" call. `armed` never hit this because it's a plain instance
  attribute, never routed through settings at all - the very first plugin
  setting that's both defaulted to `False` *and* toggled from outside the
  normal Settings-dialog Save flow (`delete_finished_file_enabled`, via the
  sidebar) hit it immediately, looking like "the checkbox can't be
  unchecked." Pass `force=True` on a settings write you want stored
  unconditionally regardless of the default.
- **Any sidebar/navbar control that fires an API command on click needs a
  busyObservable guard, the same way the settings-page Test buttons already
  had one.** Without it, the PrintButler log showed a single intended click
  producing a *burst* of several identical `set_armed`/
  `set_delete_finished_file_enabled` calls in a row - whatever the deeper
  cause (this wasn't fully root-caused), the effect was a pile of pending
  requests that made the whole OctoPrint page feel frozen (unrelated
  buttons like Connect included) while they were all in flight. A
  `busyObservable` checked at the top of the handler, set for the duration
  of the request, and bound to the checkbox's own `enable` in the template
  closes this off unconditionally: a control that's disabled while a
  request is in flight physically cannot fire a second one, regardless of
  what would otherwise have triggered the repeat.
