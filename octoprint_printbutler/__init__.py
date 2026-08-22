# coding=utf-8
"""
OctoPrint-PrintButler  -  __init__.py
"""
from __future__ import absolute_import, unicode_literals

import datetime
import json
import shlex
import subprocess
import threading
import time
import traceback
from collections import deque

import flask
import octoprint.plugin
from octoprint.events import Events

MAX_LOG_ENTRIES = 400


class PrintButlerPlugin(
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.EventHandlerPlugin,
    octoprint.plugin.SimpleApiPlugin,
):

    def __init__(self):
        self._log_entries = deque(maxlen=MAX_LOG_ENTRIES)
        self._mqtt_helpers = None

        # This printer's own presence: OctoPrint runs on the same host the
        # smart plug powers, so "is this printer on" is trivially "is this
        # plugin currently running" - no MQTT round-trip needed. True from
        # startup until _do_safe_shutdown flips it off on its way out.
        self._this_printer_active = True
        self._peer_states = {}           # topic -> True/False
        self._shared_light_desired = None

        self._finish_revert_timer = None
        self._finish_light_off_timer = None
        self._shutdown_lock = threading.Lock()
        self._shutdown_running = False

        self._cooldown_stop = threading.Event()
        self._cooldown_since = None   # timestamp, or None when not currently counting down
        # Auto-shutdown-when-cool arm/disarm - live, in-memory, resets to
        # armed on every OctoPrint start (see the navbar toggle).
        self._auto_shutdown_armed = True

    # -- SettingsPlugin ----------------------------------------------------

    def get_settings_defaults(self):
        return dict(
            enabled=True,

            # Print finished notification (e.g. WLED trigger topic)
            finish_enabled=False,
            finish_topic="",
            finish_payload_on="true",
            finish_payload_off="false",
            finish_qos=2,
            finish_retain=True,
            finish_revert_after=30,

            # Quiet hours - suppresses finish notify, finish light, and
            # the "turn shared light on because a print finished" action.
            # Does NOT suppress the plug-follows-light mirroring below.
            quiet_hours_enabled=True,
            quiet_hours_start="22:00",
            quiet_hours_end="10:00",

            # Per-printer finish indicator light
            finish_light_enabled=False,
            finish_light_topic="",
            finish_light_payload_on="ON",
            finish_light_payload_off="OFF",
            finish_light_qos=0,
            finish_light_retain=False,
            finish_light_turn_off_after=0,  # 0 = leave on until next print starts

            # Shared work light (stays on while this OR any peer printer is on)
            shared_light_enabled=False,
            shared_light_set_topic="",
            shared_light_payload_on="ON",
            shared_light_payload_off="OFF",
            shared_light_qos=0,
            shared_light_retain=False,
            shared_light_state_topic="",
            shared_light_state_json_key="state",
            shared_light_peer_topics="",
            shared_light_peer_payload_on="ON",
            shared_light_peer_json_key="state",

            # Safe shutdown - this host can't reliably cut its own mains power
            # (it dies mid-shutdown), so PrintButler only publishes a trigger
            # topic beforehand; an external automation (e.g. Home Assistant)
            # is responsible for actually switching the plug off once it has
            # confirmed the host is down.
            shutdown_enabled=False,
            shutdown_command="sudo shutdown -h now",
            shutdown_trigger_topic="",
            shutdown_trigger_payload_on="ON",
            shutdown_trigger_payload_off="OFF",
            shutdown_trigger_qos=1,
            shutdown_trigger_retain=False,
            shutdown_trigger_settle_seconds=3,

            # Auto-shutdown when cool: after a print finishes, watch bed/nozzle
            # temps and fire Safe Shutdown once both stay below threshold for
            # a sustained period. Gated by the navbar arm/disarm toggle too.
            auto_shutdown_enabled=False,
            auto_shutdown_bed_threshold=40,
            auto_shutdown_tool_threshold=40,
            auto_shutdown_confirm_seconds=60,
        )

    def on_settings_save(self, data):
        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
        self._plugin_log("Settings saved.")
        self._rewire_mqtt()

    # -- TemplatePlugin ------------------------------------------------------

    def get_template_configs(self):
        return [
            dict(
                type="settings",
                name="PrintButler",
                template="printbutler_settings.jinja2",
                custom_bindings=True,
            ),
            dict(
                type="navbar",
                custom_bindings=True,
                template="printbutler_navbar.jinja2",
            ),
        ]

    # -- AssetPlugin -----------------------------------------------------------

    def get_assets(self):
        return dict(js=["js/printbutler.js"], css=["css/printbutler.css"])

    # -- SoftwareUpdate (hook only) --------------------------------------------

    def get_update_information(self):
        return dict(
            printbutler=dict(
                displayName=__plugin_name__,
                displayVersion=self._plugin_version,
                type="github_release",
                user="KrX3D",
                repo="OctoPrint-PrintButler",
                current=self._plugin_version,
                pip="https://github.com/KrX3D/OctoPrint-PrintButler/archive/{target}.zip",
            )
        )

    # -- StartupPlugin ---------------------------------------------------------

    def on_after_startup(self):
        self._plugin_log("PrintButler plugin started (v{})".format(self._plugin_version))

        self._mqtt_helpers = self._plugin_manager.get_helpers(
            "mqtt", "mqtt_publish", "mqtt_subscribe", "mqtt_unsubscribe"
        )
        if not self._mqtt_helpers or "mqtt_publish" not in self._mqtt_helpers:
            self._log(
                "MQTT plugin not found. Install/enable OctoPrint's built-in 'MQTT' "
                "plugin and configure a broker connection - PrintButler reuses it.",
                "WARNING",
            )
        else:
            self._log(
                "MQTT plugin found, broker connected={}.".format(self._check_mqtt_connected())
            )
            self._rewire_mqtt()

        if self._get_bool("shared_light_enabled"):
            self._recompute_shared_light(reason="startup")

        if self._settings.get(["shutdown_trigger_topic"]):
            self._set_shutdown_trigger(False)

        threading.Thread(
            target=self._cooldown_loop, name="printbutler-cooldown", daemon=True
        ).start()

    # -- EventHandlerPlugin ------------------------------------------------

    def on_event(self, event, payload):
        if not self._get_bool("enabled"):
            return
        if event == Events.PRINT_DONE:
            self._handle_print_done()
        elif event == Events.PRINT_STARTED:
            self._handle_print_started()

    def _handle_print_done(self):
        quiet = self._in_quiet_hours()
        self._log("Print finished (quiet_hours={}).".format(quiet))

        if self._get_bool("finish_enabled"):
            if quiet:
                self._log("Finish notify skipped - quiet hours.")
            else:
                self._publish_finish_notify()

        if self._get_bool("finish_light_enabled"):
            if quiet:
                self._log("Finish light skipped - quiet hours.")
            else:
                self._cancel_timer("_finish_light_off_timer")
                self._set_finish_light(True)
                off_after = int(self._settings.get(["finish_light_turn_off_after"]) or 0)
                if off_after > 0:
                    self._log("Finish light will turn off in {}s.".format(off_after))
                    self._finish_light_off_timer = threading.Timer(
                        off_after, lambda: self._set_finish_light(False)
                    )
                    self._finish_light_off_timer.daemon = True
                    self._finish_light_off_timer.start()

        if self._get_bool("shared_light_enabled") and not quiet:
            self._recompute_shared_light(reason="print_done")

    def _handle_print_started(self):
        if self._get_bool("finish_light_enabled"):
            self._cancel_timer("_finish_light_off_timer")
            self._set_finish_light(False)

    # -- Finish notify / finish light ---------------------------------------

    def _publish_finish_notify(self, overrides=None):
        """
        overrides lets the "Test" button in the UI use whatever is currently
        typed into the settings form, even if it hasn't been saved yet - real
        (event-triggered) calls pass nothing and use the persisted settings.
        """
        overrides = overrides or {}
        topic = overrides.get("topic") or self._settings.get(["finish_topic"])
        if not topic:
            self._log("Finish notify enabled but no topic configured.", "WARNING")
            return False

        qos = int(overrides.get("qos") if overrides.get("qos") is not None
                   else self._settings.get(["finish_qos"]) or 0)
        retain = bool(overrides.get("retain") if "retain" in overrides
                      else self._get_bool("finish_retain"))
        payload_on = overrides.get("payload_on") or self._settings.get(["finish_payload_on"]) or "true"
        payload_off = overrides.get("payload_off") or self._settings.get(["finish_payload_off"]) or "false"

        self._cancel_timer("_finish_revert_timer")
        self._mqtt_publish(topic, payload_on, qos=qos, retain=retain)

        revert_after = int(overrides.get("revert_after") if overrides.get("revert_after") is not None
                            else self._settings.get(["finish_revert_after"]) or 0)
        if revert_after > 0:
            self._finish_revert_timer = threading.Timer(
                revert_after,
                lambda: self._mqtt_publish(topic, payload_off, qos=qos, retain=retain),
            )
            self._finish_revert_timer.daemon = True
            self._finish_revert_timer.start()
        return True

    def _set_finish_light(self, on, overrides=None):
        overrides = overrides or {}
        topic = overrides.get("topic") or self._settings.get(["finish_light_topic"])
        if not topic:
            return False
        qos = int(overrides.get("qos") if overrides.get("qos") is not None
                   else self._settings.get(["finish_light_qos"]) or 0)
        retain = bool(overrides.get("retain") if "retain" in overrides
                      else self._get_bool("finish_light_retain"))
        if on:
            payload = overrides.get("payload_on") or self._settings.get(["finish_light_payload_on"])
        else:
            payload = overrides.get("payload_off") or self._settings.get(["finish_light_payload_off"])
        self._mqtt_publish(topic, payload, qos=qos, retain=retain)
        return True

    # -- MQTT wiring ---------------------------------------------------------

    def _rewire_mqtt(self):
        if not self._mqtt_helpers or "mqtt_subscribe" not in self._mqtt_helpers:
            return

        unsub = self._mqtt_helpers.get("mqtt_unsubscribe")
        if unsub:
            unsub(self._on_peer_state_message)
            unsub(self._on_shared_light_state_message)

        self._peer_states = {}
        sub = self._mqtt_helpers["mqtt_subscribe"]

        if self._get_bool("shared_light_enabled"):
            for peer_topic in self._get_peer_topics():
                sub(peer_topic, self._on_peer_state_message)
                self._log("Subscribed to peer topic: {}".format(peer_topic))

            state_topic = self._settings.get(["shared_light_state_topic"])
            if state_topic:
                sub(state_topic, self._on_shared_light_state_message)
                self._log("Subscribed to shared light state topic: {}".format(state_topic))

    def _get_peer_topics(self):
        raw = self._settings.get(["shared_light_peer_topics"]) or ""
        return [line.strip() for line in raw.splitlines() if line.strip()]

    def _on_peer_state_message(self, topic, payload, retained=None, qos=None, **kwargs):
        val = self._payload_is_on(
            payload,
            self._settings.get(["shared_light_peer_payload_on"]),
            json_key=self._settings.get(["shared_light_peer_json_key"]),
        )
        self._peer_states[topic] = val
        self._log("Peer state -> {} = {}".format(topic, val))
        self._recompute_shared_light(reason="peer_state")

    def _on_shared_light_state_message(self, topic, payload, retained=None, qos=None, **kwargs):
        val = self._payload_is_on(
            payload,
            self._settings.get(["shared_light_payload_on"]),
            json_key=self._settings.get(["shared_light_state_json_key"]),
        )
        if self._shared_light_desired is True and val is False:
            self._log("Shared light dropped out unexpectedly, re-asserting ON.", "WARNING")
            self._set_shared_light(True)

    def _recompute_shared_light(self, reason=""):
        if not self._get_bool("shared_light_enabled"):
            return
        active = self._this_printer_active or any(self._peer_states.values())
        self._log(
            "Recompute shared light ({}): this_printer={} peers={} -> active={}".format(
                reason, self._this_printer_active, self._peer_states, active
            )
        )
        self._shared_light_desired = active
        self._set_shared_light(active)

    def _set_shared_light(self, on, overrides=None):
        overrides = overrides or {}
        topic = overrides.get("topic") or self._settings.get(["shared_light_set_topic"])
        if not topic:
            return False
        qos = int(overrides.get("qos") if overrides.get("qos") is not None
                   else self._settings.get(["shared_light_qos"]) or 0)
        retain = bool(overrides.get("retain") if "retain" in overrides
                      else self._get_bool("shared_light_retain"))
        if on:
            payload = overrides.get("payload_on") or self._settings.get(["shared_light_payload_on"])
        else:
            payload = overrides.get("payload_off") or self._settings.get(["shared_light_payload_off"])
        self._mqtt_publish(topic, payload, qos=qos, retain=retain)
        return True

    def _check_mqtt_connected(self):
        """
        The MQTT plugin exposes no public "am I connected" helper, only
        mqtt_publish(). With allow_queueing=False it returns False immediately
        if the broker connection is down, so a throwaway non-retained publish
        doubles as a live connectivity probe without touching the other
        plugin's private state.
        """
        if not self._mqtt_helpers or "mqtt_publish" not in self._mqtt_helpers:
            return False
        publish = self._mqtt_helpers["mqtt_publish"]
        try:
            return bool(publish(
                "printbutler/ping", "1", retained=False, qos=0, allow_queueing=False
            ))
        except Exception:
            return False

    @staticmethod
    def _payload_is_on(raw_payload, match_str, json_key=""):
        """
        Zigbee2MQTT (and similar) publish a full JSON state object, e.g.
        {"state": "ON", "indicator_mode": "off/on", ...} - a naive substring
        search for "ON" would false-positive on unrelated fields like
        "off/on". When json_key is set, parse the payload as JSON and
        compare that field's value exactly; otherwise (or if parsing fails)
        fall back to comparing the whole payload as plain text.
        """
        try:
            if isinstance(raw_payload, (bytes, bytearray)):
                text = raw_payload.decode("utf-8", errors="replace")
            else:
                text = str(raw_payload)
        except Exception:
            text = str(raw_payload)

        match = (match_str or "ON").strip().strip('"').lower()

        if json_key:
            try:
                data = json.loads(text)
            except Exception:
                data = None
            if isinstance(data, dict) and json_key in data:
                return str(data[json_key]).strip().lower() == match

        return text.strip().strip('"').lower() == match

    def _mqtt_publish(self, topic, payload, qos=0, retain=False):
        if not topic:
            return False
        if not self._mqtt_helpers or "mqtt_publish" not in self._mqtt_helpers:
            self._log("MQTT publish skipped (helper unavailable): {}".format(topic), "WARNING")
            return False
        publish = self._mqtt_helpers["mqtt_publish"]
        ok = publish(topic, payload, retained=retain, qos=qos, allow_queueing=True)
        self._log(
            "MQTT publish -> {} = {} (qos={} retain={} ok={})".format(
                topic, payload, qos, retain, ok
            )
        )
        return ok

    # -- Safe shutdown ---------------------------------------------------------

    def _do_safe_shutdown(self):
        """
        This host cannot reliably cut its own mains power: once the shutdown
        command runs, OctoPrint's own process (and its MQTT connection) can
        die at any point during the shutdown sequence, well before a fixed
        in-process delay would elapse. So PrintButler's job ends at publishing
        a trigger topic before shutting down - actually switching the plug
        off is left to something that stays alive independently of this host
        (e.g. a Home Assistant automation watching that topic, which can
        safely wait for/confirm the host is really down before cutting power).
        """
        acquired = self._shutdown_lock.acquire(blocking=False)
        if not acquired:
            self._log("Safe shutdown already in progress.", "WARNING")
            return
        self._shutdown_running = True
        try:
            self._log("=" * 60)
            self._log("Safe shutdown requested.")
            self._cooldown_since = None

            self._this_printer_active = False
            if self._get_bool("shared_light_enabled"):
                self._recompute_shared_light(reason="shutdown")

            if self._settings.get(["shutdown_trigger_topic"]):
                self._set_shutdown_trigger(True)
                settle = int(self._settings.get(["shutdown_trigger_settle_seconds"]) or 0)
                if settle > 0:
                    self._log("Waiting {}s for the trigger to leave the host...".format(settle))
                    time.sleep(settle)
            else:
                self._log(
                    "No shutdown trigger topic configured - nothing will cut "
                    "mains power once this host goes down.",
                    "WARNING",
                )

            cmd = self._settings.get(["shutdown_command"]) or "sudo shutdown -h now"
            self._log("Running shutdown command: {}".format(cmd))
            try:
                subprocess.Popen(shlex.split(cmd))
            except Exception as exc:
                self._log("Shutdown command failed: {}".format(exc), "ERROR")
                self._log(traceback.format_exc(), "DEBUG")

            self._log("Safe shutdown sequence complete - host is going down.")
            self._log("=" * 60)
        finally:
            self._shutdown_running = False
            self._shutdown_lock.release()

    def _set_shutdown_trigger(self, on, overrides=None):
        """
        Published ON right before Safe Shutdown runs, and reset back to OFF
        here on the next startup - so a retained topic doesn't get stuck
        "ON" forever, and any automation watching it gets a clean signal
        that this host came back up.
        """
        overrides = overrides or {}
        topic = overrides.get("topic") or self._settings.get(["shutdown_trigger_topic"])
        if not topic:
            return False
        qos = int(overrides.get("qos") if overrides.get("qos") is not None
                   else self._settings.get(["shutdown_trigger_qos"]) or 0)
        retain = bool(overrides.get("retain") if "retain" in overrides
                      else self._get_bool("shutdown_trigger_retain"))
        if on:
            payload = overrides.get("payload_on") or self._settings.get(["shutdown_trigger_payload_on"]) or "ON"
        else:
            payload = overrides.get("payload_off") or self._settings.get(["shutdown_trigger_payload_off"]) or "OFF"
        self._log(
            "Publishing shutdown trigger -> {} = {}{}".format(
                topic, payload,
                " (an external automation is expected to cut mains power once "
                "it confirms this host is actually down)." if on
                else " (reset after startup)."
            )
        )
        return self._mqtt_publish(topic, payload, qos=qos, retain=retain)

    # -- Auto-shutdown when cool ------------------------------------------------

    def _cooldown_loop(self):
        """
        Runs for the plugin's whole lifetime, checked every ~10s. Mirrors a
        plain "temperature below X for N minutes, and not printing" trigger -
        it does not require a print to have just finished, matching the
        original Home Assistant automation's actual behavior. This means it
        starts counting immediately whenever armed+enabled+idle+cool, which
        includes right after OctoPrint (re)starts if the printer already is.
        Auto-disarms itself after firing so it does not immediately loop.
        """
        was_active = None
        while not self._cooldown_stop.is_set():
            self._cooldown_stop.wait(10)
            if self._cooldown_stop.is_set():
                break

            try:
                active = (
                    self._get_bool("auto_shutdown_enabled")
                    and self._get_bool("shutdown_enabled")
                    and self._auto_shutdown_armed
                )
                if active != was_active:
                    self._log("Cooldown watch {}.".format("active" if active else "inactive"))
                    was_active = active
                if not active:
                    self._cooldown_since = None
                    continue

                if self._printer.is_printing() or self._printer.is_paused():
                    if self._cooldown_since is not None:
                        self._log("Cooldown watch reset - printer is active.")
                    self._cooldown_since = None
                    continue

                bed_thresh = float(self._settings.get(["auto_shutdown_bed_threshold"]) or 40)
                tool_thresh = float(self._settings.get(["auto_shutdown_tool_threshold"]) or 40)
                confirm_secs = int(self._settings.get(["auto_shutdown_confirm_seconds"]) or 60)

                temps = self._printer.get_current_temperatures() or {}
                bed = (temps.get("bed") or {}).get("actual")
                tool0 = (temps.get("tool0") or {}).get("actual")
                is_cool = (
                    bed is not None and bed <= bed_thresh
                    and tool0 is not None and tool0 <= tool_thresh
                )

                now = time.time()
                if is_cool:
                    if self._cooldown_since is None:
                        self._cooldown_since = now
                        self._log(
                            "Bed/nozzle below threshold (bed={} tool={}), confirming "
                            "for {}s...".format(bed, tool0, confirm_secs)
                        )
                    elif now - self._cooldown_since >= confirm_secs:
                        self._log("Cooldown confirmed - triggering safe shutdown, disarming.")
                        self._cooldown_since = None
                        self._auto_shutdown_armed = False
                        self._plugin_manager.send_plugin_message(
                            self._identifier,
                            {"event": "armed_changed", "armed": False},
                        )
                        threading.Thread(
                            target=self._do_safe_shutdown, name="printbutler-autoshutdown",
                            daemon=True,
                        ).start()
                else:
                    if self._cooldown_since is not None:
                        self._log("Temperature rose again, resetting cooldown timer.")
                    self._cooldown_since = None
            except Exception:
                self._logger.exception("Cooldown watch loop error")

    # -- SimpleApiPlugin -------------------------------------------------------

    def get_api_commands(self):
        return dict(
            shutdown_now=[],
            clear_logs=[],
            set_armed=["armed"],
            test_finish_notify=[],
            test_finish_light=[],
            test_shared_light=[],
            test_shutdown_trigger=[],
        )

    def on_api_command(self, command, data):
        self._plugin_log("API command received: {}".format(command))

        if command == "shutdown_now":
            if not self._get_bool("shutdown_enabled"):
                return flask.jsonify({
                    "success": False,
                    "message": "Safe shutdown is disabled in settings.",
                })
            if self._shutdown_running:
                return flask.jsonify({
                    "success": False,
                    "message": "A shutdown is already in progress.",
                })
            t = threading.Thread(
                target=self._do_safe_shutdown, name="printbutler-shutdown", daemon=True
            )
            t.start()
            return flask.jsonify({"success": True, "message": "Shutdown initiated."})

        elif command == "clear_logs":
            self._log_entries.clear()
            return flask.jsonify({"success": True})

        elif command == "set_armed":
            self._auto_shutdown_armed = bool(data.get("armed"))
            self._log(
                "Auto-shutdown-when-cool {}.".format(
                    "armed" if self._auto_shutdown_armed else "disarmed"
                )
            )
            if not self._auto_shutdown_armed:
                self._cooldown_since = None
            self._plugin_manager.send_plugin_message(
                self._identifier,
                {"event": "armed_changed", "armed": self._auto_shutdown_armed},
            )
            return flask.jsonify({"success": True, "armed": self._auto_shutdown_armed})

        elif command == "test_finish_notify":
            if not self._publish_finish_notify(overrides=data):
                return flask.jsonify({
                    "success": False,
                    "message": "No finish topic configured - fill in the Topic field first.",
                })
            return flask.jsonify({
                "success": True,
                "message": "Finish notification published (using the current, possibly unsaved field values).",
            })

        elif command == "test_finish_light":
            if not self._set_finish_light(True, overrides=data):
                return flask.jsonify({
                    "success": False,
                    "message": "No finish light topic configured - fill in the Topic field first.",
                })
            t = threading.Timer(3, lambda: self._set_finish_light(False, overrides=data))
            t.daemon = True
            t.start()
            return flask.jsonify({
                "success": True,
                "message": "Finish light switched on for 3s (using the current, possibly unsaved field values).",
            })

        elif command == "test_shared_light":
            if not self._set_shared_light(True, overrides=data):
                return flask.jsonify({
                    "success": False,
                    "message": "No shared light set topic configured - fill in the field first.",
                })
            t = threading.Timer(3, lambda: self._set_shared_light(False, overrides=data))
            t.daemon = True
            t.start()
            return flask.jsonify({
                "success": True,
                "message": "Shared light switched on for 3s (using the current, possibly unsaved field values).",
            })

        elif command == "test_shutdown_trigger":
            if not self._set_shutdown_trigger(True, overrides=data):
                return flask.jsonify({
                    "success": False,
                    "message": "No trigger topic configured - fill in the field first.",
                })
            return flask.jsonify({
                "success": True,
                "message": "Trigger topic published (shutdown command was NOT run).",
            })

        return flask.abort(400)

    def on_api_get(self, request):
        mqtt_helper_present = bool(
            self._mqtt_helpers and "mqtt_publish" in self._mqtt_helpers
        )
        cooldown_seconds_remaining = None
        if self._cooldown_since is not None:
            confirm_secs = int(self._settings.get(["auto_shutdown_confirm_seconds"]) or 60)
            cooldown_seconds_remaining = max(
                0, int(confirm_secs - (time.time() - self._cooldown_since))
            )
        return flask.jsonify(dict(
            plugin_version=self._plugin_version,
            mqtt_helper_present=mqtt_helper_present,
            mqtt_connected=self._check_mqtt_connected() if mqtt_helper_present else False,
            this_printer_active=self._this_printer_active,
            shared_light_desired=self._shared_light_desired,
            peer_states=self._peer_states,
            quiet_hours_active=self._in_quiet_hours(),
            shutdown_running=self._shutdown_running,
            auto_shutdown_armed=self._auto_shutdown_armed,
            cooldown_counting=self._cooldown_since is not None,
            cooldown_seconds_remaining=cooldown_seconds_remaining,
            logs=list(self._log_entries),
        ))

    # -- Quiet hours -------------------------------------------------------

    def _in_quiet_hours(self):
        if not self._get_bool("quiet_hours_enabled"):
            return False
        start = self._settings.get(["quiet_hours_start"]) or "22:00"
        end = self._settings.get(["quiet_hours_end"]) or "10:00"
        try:
            sh, sm = (int(x) for x in start.split(":"))
            eh, em = (int(x) for x in end.split(":"))
        except Exception:
            return False

        now = datetime.datetime.now().time()
        start_t = datetime.time(sh, sm)
        end_t = datetime.time(eh, em)

        if start_t <= end_t:
            return start_t <= now <= end_t
        # Window crosses midnight (e.g. 22:00 -> 10:00)
        return now >= start_t or now <= end_t

    # -- Helpers -----------------------------------------------------------

    def _get_bool(self, key):
        """
        Safe boolean getter that handles both Python bool and string "true"/"false"
        (KO checkboxes normally send real booleans, but be defensive).
        """
        val = self._settings.get([key])
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.strip().lower() in ("true", "1", "yes")
        return bool(val)

    def _cancel_timer(self, attr_name):
        timer = getattr(self, attr_name, None)
        if timer is not None:
            timer.cancel()
            setattr(self, attr_name, None)

    # -- Logging -------------------------------------------------------------

    def _log(self, message, level="INFO"):
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = "[{}] [{}] {}".format(ts, level, message)
        self._log_entries.append(line)
        if level == "ERROR":
            self._logger.error(message)
        elif level == "WARNING":
            self._logger.warning(message)
        elif level == "DEBUG":
            self._logger.debug(message)
        else:
            self._logger.info(message)

    def _plugin_log(self, message):
        """Logs to octoprint.log only (not the in-UI log buffer)."""
        self._logger.info(message)


# -- Plugin registration -------------------------------------------------------

__plugin_name__         = "PrintButler"
__plugin_identifier__   = "printbutler"
__plugin_pythoncompat__ = ">=3.7,<4"
__plugin_version__      = "0.1.0"
__plugin_description__  = (
    "Print-finished notifications, light/plug automation, and safe shutdown - "
    "all driven from OctoPrint's own state over MQTT, configurable from the "
    "settings UI."
)
__plugin_author__       = "KrX3D"
__plugin_url__          = "https://github.com/KrX3D/OctoPrint-PrintButler"
__plugin_license__      = "MIT"


def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = PrintButlerPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.plugin.softwareupdate.check_config":
            __plugin_implementation__.get_update_information,
    }
