/*
 * OctoPrint-PrintButler - printbutler.js
 */
$(function () {
    var tr = function (text) {
        try {
            if (typeof gettext === "function") { return gettext(text); }
        } catch (e) {}
        return text;
    };

    function boolText(val, yes, no, unknown) {
        if (val === true) { return yes; }
        if (val === false) { return no; }
        return unknown;
    }

    function PrintButlerViewModel(parameters) {
        var self = this;

        self.settingsViewModel   = parameters[0];
        self.loginStateViewModel = parameters[1];

        self.pluginVersion   = ko.observable("?");
        self.mqttHelperPresent = ko.observable(null);
        self.mqttConnected     = ko.observable(null);
        self.thisPrinterActive = ko.observable(true);
        self.sharedLightDesired = ko.observable(null);
        self.quietHoursActive   = ko.observable(null);
        self.armed                = ko.observable(true);
        self.armedBusy             = ko.observable(false);
        self.deleteFinishedFileEnabled = ko.observable(false);
        self.deleteFinishedFileBusy    = ko.observable(false);
        // Last version number actually applied for each field - the server
        // bumps its own counter on every change, from any path (this tab's
        // toggle, another tab's toggle, the Settings dialog, the auto-
        // disarm-on-trigger path), and tags every poll response and push
        // message with it. -1 so the very first update (server version 0)
        // is always accepted. See _applyIfNewer / LEARNINGS.md.
        self._armedVersion              = -1;
        self._deleteFinishedFileVersion = -1;

        // Belt-and-suspenders: force the checkbox DOM nodes to match the
        // observable directly, bypassing Knockout's own checked binding
        // entirely for the actual repaint. Querying and setting .checked
        // directly can't have any binding-context/reactivity ambiguity -
        // it's a plain DOM write run every time the observable's value
        // actually changes, regardless of what caused that change.
        self._syncCheckboxDom = function (fieldName, value) {
            document
                .querySelectorAll('input[type="checkbox"][data-printbutler-field="' + fieldName + '"]')
                .forEach(function (el) {
                    if (el.checked !== value) { el.checked = value; }
                });
        };
        self.armed.subscribe(function (value) { self._syncCheckboxDom("armed", value); });
        self.deleteFinishedFileEnabled.subscribe(function (value) { self._syncCheckboxDom("delete-finished-file", value); });

        // Programmatic updates (poll, push message, a toggle's own success
        // response) go through these, NOT straight through self.armed(...)
        // / self.deleteFinishedFileEnabled(...) - the _xSyncing flag tells
        // the subscribe()-based send-to-server handlers below "this change
        // didn't come from the user checking the box, don't re-send it".
        self._armedSyncing = false;
        self._setArmedFromServer = function (value) {
            self._armedSyncing = true;
            self.armed(value);
            self._armedSyncing = false;
        };
        self._deleteFinishedFileSyncing = false;
        self._setDeleteFinishedFileFromServer = function (value) {
            self._deleteFinishedFileSyncing = true;
            self.deleteFinishedFileEnabled(value);
            self._deleteFinishedFileSyncing = false;
        };

        self.cooldownCounting          = ko.observable(false);
        self.cooldownSecondsRemaining  = ko.observable(null);
        self.logs                = ko.observableArray([]);
        self.statusPolling       = null;

        self.testFinishNotifyBusy   = ko.observable(false);
        self.testFinishLightBusy    = ko.observable(false);
        self.testSharedLightBusy    = ko.observable(false);
        self.testShutdownTriggerBusy = ko.observable(false);

        // settings is set in onBeforeBinding - null until then.
        self.settings = null;

        // -- Computed display helpers -------------------------------------

        self.mqttBadgeClass = ko.computed(function () {
            if (self.mqttHelperPresent() === false) { return "printbutler-status-failed"; }
            if (self.mqttConnected() === true)      { return "printbutler-status-success"; }
            if (self.mqttConnected() === false)     { return "printbutler-status-failed"; }
            return "printbutler-status-never";
        });

        self.mqttBadgeText = ko.computed(function () {
            if (self.mqttHelperPresent() === false) { return tr("MQTT plugin not found"); }
            if (self.mqttConnected() === true)      { return tr("MQTT connected"); }
            if (self.mqttConnected() === false)     { return tr("MQTT not connected"); }
            return tr("Unknown");
        });

        self.thisPrinterActiveText = ko.computed(function () {
            return boolText(self.thisPrinterActive(), tr("Active"), tr("Shutting down"), tr("Unknown"));
        });

        self.sharedLightText = ko.computed(function () {
            return boolText(self.sharedLightDesired(), tr("On"), tr("Off"), tr("Unknown"));
        });

        self.quietHoursText = ko.computed(function () {
            return boolText(self.quietHoursActive(), tr("Yes"), tr("No"), tr("Unknown"));
        });

        // autoShutdownFeatureEnabled itself is created in onBeforeBinding,
        // below - not here. settingsViewModel.settings.plugins.printbutler
        // isn't guaranteed populated yet at construction time (it's filled
        // in later, asynchronously) and a ko.computed evaluates its
        // function immediately to find its dependencies - reading it here
        // would throw and abort building this entire viewmodel, silently
        // leaving every binding on this plugin's settings/sidebar templates
        // unwired. onBeforeBinding is guaranteed to run only once that data
        // actually exists (see self.settings below).

        self.cooldownSecondsText = ko.computed(function () {
            var s = self.cooldownSecondsRemaining();
            return s === null ? "" : tr("{seconds}s").replace("{seconds}", s);
        });

        self.armedTooltip = ko.computed(function () {
            return self.armed()
                ? tr("PrintButler: auto-shutdown-when-cool is armed (click to disarm)")
                : tr("PrintButler: auto-shutdown-when-cool is DISARMED (click to arm)");
        });

        // Applies applyFn() only if incomingVersion is as new or newer than
        // what's already been applied for versionKey (e.g. "_armedVersion"),
        // updating that tracked version first. Used everywhere armed/
        // deleteFinishedFileEnabled can be updated from the server (poll,
        // push message, or a toggle's own success response) so a response
        // that happens to arrive out of order can never revert a value a
        // more recent change already applied. A missing/non-numeric version
        // (shouldn't happen, but be defensive) always applies.
        self._applyIfNewer = function (versionKey, incomingVersion, applyFn) {
            if (typeof incomingVersion !== "number" || incomingVersion >= self[versionKey]) {
                if (typeof incomingVersion === "number") { self[versionKey] = incomingVersion; }
                applyFn();
            }
        };

        // -- Lifecycle -------------------------------------------------

        self.onBeforeBinding = function () {
            self.settings = self.settingsViewModel.settings.plugins.printbutler;

            // Created here, not at construction time: self.settings is
            // real by now (unlike at construction), so this reads
            // shutdown_enabled() unconditionally on every evaluation and
            // correctly subscribes to it - no short-circuit, no crash.
            self.autoShutdownFeatureEnabled = ko.computed(function () {
                var shutdownOn = self.settings.shutdown_enabled();
                return shutdownOn === true || shutdownOn === "true";
            });
        };

        self.onSettingsShown = function () {
            self.refreshStatus();
        };

        self.onStartupComplete = function () {
            self.refreshStatus();

            // Runs for as long as this page is open, not just while the
            // Settings dialog is shown - the sidebar panel now also needs
            // a live "armed"/cooldown-countdown display on the main GUI.
            if (!self.statusPolling) {
                self.statusPolling = setInterval(function () {
                    self.refreshStatus();
                }, 5000);
            }
        };

        // This push is a *separate* channel from any REST request/response
        // (including a toggle's own .done() below) - it has no guaranteed
        // delivery order relative to them. Two rapid toggles can easily have
        // the earlier one's push arrive after the later one's REST response,
        // which would silently revert the checkbox back to the older value
        // without the version guard (this was the actual remaining cause of
        // "doesn't update until F5" - see LEARNINGS.md).
        self.onDataUpdaterPluginMessage = function (plugin, data) {
            if (plugin !== "printbutler" || !data || !data.event) { return; }
            if (data.event === "armed_changed") {
                self._applyIfNewer("_armedVersion", data.version, function () {
                    self._setArmedFromServer(data.armed === true);
                });
            } else if (data.event === "delete_finished_file_enabled_changed") {
                self._applyIfNewer("_deleteFinishedFileVersion", data.version, function () {
                    self._setDeleteFinishedFileFromServer(data.enabled === true);
                    if (self.settings) { self.settings.delete_finished_file_enabled(data.enabled === true); }
                });
            }
        };

        // -- API -------------------------------------------------------

        self.refreshStatus = function () {
            OctoPrint.get("api/plugin/printbutler")
                .done(function (data) {
                    self.pluginVersion(data.plugin_version || "?");
                    self.mqttHelperPresent(data.mqtt_helper_present === true);
                    self.mqttConnected(data.mqtt_connected === true);
                    self.thisPrinterActive(data.this_printer_active !== false);
                    self.sharedLightDesired(data.shared_light_desired);
                    self.quietHoursActive(data.quiet_hours_active === true);
                    self._applyIfNewer("_armedVersion", data.armed_version, function () {
                        self._setArmedFromServer(data.auto_shutdown_armed !== false);
                    });
                    // Deliberately NOT also writing this into
                    // self.settings.delete_finished_file_enabled here: this
                    // poll runs every 5s regardless of what else is
                    // happening, and even with the version guard above, "no
                    // newer version yet" still re-applies the *same* value
                    // on every tick - which would keep stomping a Settings-
                    // dialog edit the user has made but not saved yet. Only
                    // sync that shared observable on an explicit, confirmed
                    // change (the armed/delete-file subscribe handlers'
                    // success callback and the delete_finished_file_enabled_
                    // changed plugin message above) - never from a routine
                    // status refresh.
                    self._applyIfNewer("_deleteFinishedFileVersion", data.delete_finished_file_version, function () {
                        self._setDeleteFinishedFileFromServer(data.delete_finished_file_enabled === true);
                    });
                    self.cooldownCounting(data.cooldown_counting === true);
                    self.cooldownSecondsRemaining(
                        typeof data.cooldown_seconds_remaining === "number"
                            ? data.cooldown_seconds_remaining
                            : null
                    );
                    if (Array.isArray(data.logs)) {
                        self.logs(data.logs);
                        var el = document.getElementById("printbutler_log_area");
                        if (el) { el.scrollTop = el.scrollHeight; }
                    }
                });
        };

        // OctoPrint core's own checkboxes (e.g. "Verbindungseinstellungen
        // speichern" on the Connection panel) bind `checked` alone - never
        // `checked` plus a `click` handler on the same element. This plugin
        // combined the two for a long time, and every "checkbox doesn't
        // visibly update" report traced back to it (see LEARNINGS.md) -
        // switched to the same checked-only pattern core uses, with the
        // actual API call triggered by subscribing to the observable
        // instead. _armedSyncing (set by _setArmedFromServer) distinguishes
        // "the user just checked/unchecked this box" from "we just set this
        // to reflect the server's own state" - without it, every poll/push
        // update would loop right back into sending another API request.
        // Guarded by armedBusy too, and the template disables the checkbox
        // itself while busy (`enable: !armedBusy()`) so a disabled checkbox
        // can't fire a change at all - belt and suspenders against the
        // request pile-up seen before this was added.
        self.armed.subscribe(function (value) {
            if (self._armedSyncing || self.armedBusy()) { return; }
            self.armedBusy(true);
            OctoPrint.simpleApiCommand("printbutler", "set_armed", {armed: value})
                .done(function (data) {
                    self._applyIfNewer("_armedVersion", data.version, function () {
                        self._setArmedFromServer(data.armed === true);
                    });
                })
                .fail(function () {
                    self._setArmedFromServer(!value);
                    new PNotify({title: tr("PrintButler"), text: tr("Request failed."), type: "error"});
                })
                .always(function () { self.armedBusy(false); });
        });

        // delete_finished_file_enabled is a real persisted setting (unlike
        // armed), but toggled from the sidebar the same dedicated-observable
        // + custom-API-command way as armed above, deliberately NOT via a
        // two-way binding straight onto settings.delete_finished_file_enabled
        // plus OctoPrint.settings.save() - that seemed simpler, but tying
        // the sidebar checkbox directly to the same shared, globally-remapped
        // settings observable OctoPrint's core Settings dialog also owns
        // made it unreliable in practice (see LEARNINGS.md). A plugin-owned
        // observable, explicitly persisted server-side, is the same
        // approach that already works correctly for armed.
        self.deleteFinishedFileEnabled.subscribe(function (value) {
            if (self._deleteFinishedFileSyncing || self.deleteFinishedFileBusy()) { return; }
            self.deleteFinishedFileBusy(true);
            OctoPrint.simpleApiCommand("printbutler", "set_delete_finished_file_enabled", {enabled: value})
                .done(function (data) {
                    self._applyIfNewer("_deleteFinishedFileVersion", data.version, function () {
                        var enabled = data.delete_finished_file_enabled === true;
                        self._setDeleteFinishedFileFromServer(enabled);
                        if (self.settings) { self.settings.delete_finished_file_enabled(enabled); }
                    });
                })
                .fail(function () {
                    self._setDeleteFinishedFileFromServer(!value);
                    new PNotify({title: tr("PrintButler"), text: tr("Request failed."), type: "error"});
                })
                .always(function () { self.deleteFinishedFileBusy(false); });
        });

        self._runTest = function (command, busyObservable, extraData) {
            if (busyObservable()) { return; }
            busyObservable(true);
            OctoPrint.simpleApiCommand("printbutler", command, extraData || {})
                .done(function (data) {
                    new PNotify({
                        title: tr("PrintButler"),
                        text: data.message || (data.success ? tr("Done.") : tr("Failed.")),
                        type: data.success ? "success" : "error",
                        hide: true
                    });
                })
                .fail(function () {
                    new PNotify({title: tr("PrintButler"), text: tr("Request failed."), type: "error"});
                })
                .always(function () { busyObservable(false); });
        };

        // Test buttons send the form's current (possibly unsaved) values as
        // overrides, so you can try a topic/payload before hitting Save.

        self.testFinishNotify = function () {
            self._runTest("test_finish_notify", self.testFinishNotifyBusy, {
                topic: self.settings.finish_topic(),
                payload_on: self.settings.finish_payload_on(),
                payload_off: self.settings.finish_payload_off(),
                qos: parseInt(self.settings.finish_qos(), 10) || 0,
                retain: self.settings.finish_retain() === true,
                revert_after: parseInt(self.settings.finish_revert_after(), 10) || 0
            });
        };
        self.testFinishLight = function () {
            self._runTest("test_finish_light", self.testFinishLightBusy, {
                topic: self.settings.finish_light_topic(),
                payload_on: self.settings.finish_light_payload_on(),
                payload_off: self.settings.finish_light_payload_off(),
                qos: parseInt(self.settings.finish_light_qos(), 10) || 0,
                retain: self.settings.finish_light_retain() === true
            });
        };

        self.testSharedLight = function () {
            self._runTest("test_shared_light", self.testSharedLightBusy, {
                topic: self.settings.shared_light_set_topic(),
                payload_on: self.settings.shared_light_payload_on(),
                payload_off: self.settings.shared_light_payload_off(),
                qos: parseInt(self.settings.shared_light_qos(), 10) || 0,
                retain: self.settings.shared_light_retain() === true
            });
        };

        self.testShutdownTrigger = function () {
            self._runTest("test_shutdown_trigger", self.testShutdownTriggerBusy, {
                topic: self.settings.shutdown_trigger_topic(),
                payload_on: self.settings.shutdown_trigger_payload_on(),
                qos: parseInt(self.settings.shutdown_trigger_qos(), 10) || 0,
                retain: self.settings.shutdown_trigger_retain() === true,
                json: self.settings.shutdown_trigger_json() === true
            });
        };

        self.clearLogs = function () {
            OctoPrint.simpleApiCommand("printbutler", "clear_logs", {})
                .done(function () { self.logs([]); });
        };

        self.logLineClass = function (line) {
            if (line.indexOf("[ERROR]")   !== -1) { return "log-error"; }
            if (line.indexOf("[WARNING]") !== -1) { return "log-warning"; }
            if (line.indexOf("[DEBUG]")   !== -1) { return "log-debug"; }
            return "";
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct:    PrintButlerViewModel,
        dependencies: ["settingsViewModel", "loginStateViewModel"],
        elements:     ["#settings_plugin_printbutler", "#sidebar_plugin_printbutler"]
    });
});
