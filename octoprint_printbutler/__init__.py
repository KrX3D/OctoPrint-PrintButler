# coding=utf-8
"""
OctoPrint-PrintButler  -  __init__.py
"""
from __future__ import absolute_import, unicode_literals

import datetime
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

        self._plug_state = None          # True/False/None (unknown)
        self._peer_states = {}           # topic -> True/False
        self._shared_light_desired = None

        self._finish_revert_timer = None
        self._finish_light_off_timer = None
        self._shutdown_lock = threading.Lock()
        self._shutdown_running = False

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

            # This printer's own power plug/switch
            plug_enabled=False,
            plug_state_topic="",
            plug_state_payload_on="ON",
            plug_set_topic="",
            plug_set_payload_on="ON",
            plug_set_payload_off="OFF",
            plug_qos=0,
            plug_retain=False,

            # Shared work light (stays on while this OR any peer printer is on)
            shared_light_enabled=False,
            shared_light_set_topic="",
            shared_light_payload_on="ON",
            shared_light_payload_off="OFF",
            shared_light_qos=0,
            shared_light_retain=False,
            shared_light_state_topic="",
            shared_light_peer_topics="",
            shared_light_peer_payload_on="ON",

            # Safe shutdown
            shutdown_enabled=False,
            shutdown_command="sudo shutdown -h now",
            shutdown_plug_off_delay=30,
        )

    def on_settings_save(self, data):
        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
        self._plugin_log("Settings saved.")
        self._rewire_mqtt()

    # -- TemplatePlugin ------------------------------------------------------

    def get_template_configs(self):
        return [dict(
            type="settings",
            name="PrintButler",
            template="printbutler_settings.jinja2",
            custom_bindings=True,
        )]

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
                "MQTT helper not found. Install/enable OctoPrint's built-in 'MQTT' "
                "plugin and configure a broker connection - PrintButler reuses it.",
                "WARNING",
            )
        else:
            self._log("MQTT helper found, wiring up subscriptions.")
            self._rewire_mqtt()

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

    def _publish_finish_notify(self):
        topic = self._settings.get(["finish_topic"])
        if not topic:
            self._log("Finish notify enabled but no topic configured.", "WARNING")
            return

        qos = int(self._settings.get(["finish_qos"]) or 0)
        retain = self._get_bool("finish_retain")
        payload_on = self._settings.get(["finish_payload_on"]) or "true"
        payload_off = self._settings.get(["finish_payload_off"]) or "false"

        self._cancel_timer("_finish_revert_timer")
        self._mqtt_publish(topic, payload_on, qos=qos, retain=retain)

        revert_after = int(self._settings.get(["finish_revert_after"]) or 0)
        if revert_after > 0:
            self._finish_revert_timer = threading.Timer(
                revert_after,
                lambda: self._mqtt_publish(topic, payload_off, qos=qos, retain=retain),
            )
            self._finish_revert_timer.daemon = True
            self._finish_revert_timer.start()

    def _set_finish_light(self, on):
        topic = self._settings.get(["finish_light_topic"])
        if not topic:
            return
        qos = int(self._settings.get(["finish_light_qos"]) or 0)
        retain = self._get_bool("finish_light_retain")
        payload = (
            self._settings.get(["finish_light_payload_on"])
            if on
            else self._settings.get(["finish_light_payload_off"])
        )
        self._mqtt_publish(topic, payload, qos=qos, retain=retain)

    # -- MQTT wiring ---------------------------------------------------------

    def _rewire_mqtt(self):
        if not self._mqtt_helpers or "mqtt_subscribe" not in self._mqtt_helpers:
            return

        unsub = self._mqtt_helpers.get("mqtt_unsubscribe")
        if unsub:
            unsub(self._on_plug_state_message)
            unsub(self._on_peer_state_message)
            unsub(self._on_shared_light_state_message)

        self._peer_states = {}
        sub = self._mqtt_helpers["mqtt_subscribe"]

        if self._get_bool("plug_enabled"):
            topic = self._settings.get(["plug_state_topic"])
            if topic:
                sub(topic, self._on_plug_state_message)
                self._log("Subscribed to plug state topic: {}".format(topic))

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

    def _on_plug_state_message(self, topic, payload, retained=None, qos=None, **kwargs):
        val = self._payload_is_on(payload, self._settings.get(["plug_state_payload_on"]))
        self._plug_state = val
        self._log("Plug state -> {} ({})".format(val, topic))
        if self._get_bool("shared_light_enabled"):
            self._recompute_shared_light(reason="plug_state")

    def _on_peer_state_message(self, topic, payload, retained=None, qos=None, **kwargs):
        val = self._payload_is_on(payload, self._settings.get(["shared_light_peer_payload_on"]))
        self._peer_states[topic] = val
        self._log("Peer state -> {} = {}".format(topic, val))
        self._recompute_shared_light(reason="peer_state")

    def _on_shared_light_state_message(self, topic, payload, retained=None, qos=None, **kwargs):
        val = self._payload_is_on(payload, self._settings.get(["shared_light_payload_on"]))
        if self._shared_light_desired is True and val is False:
            self._log("Shared light dropped out unexpectedly, re-asserting ON.", "WARNING")
            self._set_shared_light(True)

    def _recompute_shared_light(self, reason=""):
        if not self._get_bool("shared_light_enabled"):
            return
        active = bool(self._plug_state) or any(self._peer_states.values())
        self._log(
            "Recompute shared light ({}): plug={} peers={} -> active={}".format(
                reason, self._plug_state, self._peer_states, active
            )
        )
        self._shared_light_desired = active
        self._set_shared_light(active)

    def _set_shared_light(self, on):
        topic = self._settings.get(["shared_light_set_topic"])
        if not topic:
            return
        qos = int(self._settings.get(["shared_light_qos"]) or 0)
        retain = self._get_bool("shared_light_retain")
        payload = (
            self._settings.get(["shared_light_payload_on"])
            if on
            else self._settings.get(["shared_light_payload_off"])
        )
        self._mqtt_publish(topic, payload, qos=qos, retain=retain)

    @staticmethod
    def _payload_is_on(raw_payload, match_str):
        try:
            if isinstance(raw_payload, (bytes, bytearray)):
                text = raw_payload.decode("utf-8", errors="replace")
            else:
                text = str(raw_payload)
        except Exception:
            text = str(raw_payload)
        match = (match_str or "ON").strip()
        return match.lower() in text.lower()

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
        acquired = self._shutdown_lock.acquire(blocking=False)
        if not acquired:
            self._log("Safe shutdown already in progress.", "WARNING")
            return
        self._shutdown_running = True
        try:
            self._log("=" * 60)
            self._log("Safe shutdown requested.")

            cmd = self._settings.get(["shutdown_command"]) or "sudo shutdown -h now"
            self._log("Running shutdown command: {}".format(cmd))
            try:
                subprocess.Popen(shlex.split(cmd))
            except Exception as exc:
                self._log("Shutdown command failed: {}".format(exc), "ERROR")
                self._log(traceback.format_exc(), "DEBUG")

            if self._get_bool("plug_enabled") and self._settings.get(["plug_set_topic"]):
                delay = int(self._settings.get(["shutdown_plug_off_delay"]) or 30)
                self._log(
                    "Waiting {}s for the host to power down before cutting mains "
                    "power via MQTT.".format(delay)
                )
                time.sleep(delay)
                self._set_plug(False)
            else:
                self._log("No power plug configured - skipping MQTT power cut.")

            self._log("Safe shutdown sequence complete.")
            self._log("=" * 60)
        finally:
            self._shutdown_running = False
            self._shutdown_lock.release()

    def _set_plug(self, on):
        topic = self._settings.get(["plug_set_topic"])
        if not topic:
            return
        qos = int(self._settings.get(["plug_qos"]) or 0)
        retain = self._get_bool("plug_retain")
        payload = (
            self._settings.get(["plug_set_payload_on"])
            if on
            else self._settings.get(["plug_set_payload_off"])
        )
        self._mqtt_publish(topic, payload, qos=qos, retain=retain)

    # -- SimpleApiPlugin -------------------------------------------------------

    def get_api_commands(self):
        return dict(
            shutdown_now=[],
            clear_logs=[],
        )

    def on_api_command(self, command, data):
        self._plugin_log("API command received: {}".format(command))

        if command == "shutdown_now":
            if not self._get_bool("shutdown_enabled"):
                return flask.jsonify({
                    "success": False,
                    "message": "Safe shutdown is disabled in settings.",
                }), 400
            if self._shutdown_running:
                return flask.jsonify({
                    "success": False,
                    "message": "A shutdown is already in progress.",
                }), 409
            t = threading.Thread(
                target=self._do_safe_shutdown, name="printbutler-shutdown", daemon=True
            )
            t.start()
            return flask.jsonify({"success": True, "message": "Shutdown initiated."})

        elif command == "clear_logs":
            self._log_entries.clear()
            return flask.jsonify({"success": True})

        return flask.abort(400)

    def on_api_get(self, request):
        return flask.jsonify(dict(
            plugin_version=self._plugin_version,
            mqtt_available=bool(
                self._mqtt_helpers and "mqtt_publish" in self._mqtt_helpers
            ),
            plug_state=self._plug_state,
            shared_light_desired=self._shared_light_desired,
            peer_states=self._peer_states,
            quiet_hours_active=self._in_quiet_hours(),
            shutdown_running=self._shutdown_running,
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
