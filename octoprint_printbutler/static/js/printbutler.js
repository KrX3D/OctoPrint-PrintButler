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
        self.deleteFinishedFileEnabled = ko.observable(false);
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

        self.onDataUpdaterPluginMessage = function (plugin, data) {
            if (plugin !== "printbutler" || !data || !data.event) { return; }
            if (data.event === "armed_changed") {
                self.armed(data.armed === true);
            } else if (data.event === "delete_finished_file_enabled_changed") {
                self.deleteFinishedFileEnabled(data.enabled === true);
                if (self.settings) { self.settings.delete_finished_file_enabled(data.enabled === true); }
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
                    self.armed(data.auto_shutdown_armed !== false);
                    // Deliberately NOT also writing this into
                    // self.settings.delete_finished_file_enabled here: this
                    // poll runs every 5s regardless of what else is
                    // happening, and stomping that shared, Settings-dialog-
                    // bound observable on every tick could revert an edit
                    // the user just made in the Settings dialog but hasn't
                    // saved yet. Only sync it on an explicit, confirmed
                    // change (toggleDeleteFinishedFile's success handler and
                    // the delete_finished_file_enabled_changed plugin
                    // message below) - never from a routine status refresh.
                    self.deleteFinishedFileEnabled(data.delete_finished_file_enabled === true);
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

        // The sidebar checkbox's native click + KO's checked binding have
        // already flipped event.target.checked to the new value by the
        // time this runs, so read it directly rather than negating armed().
        self.toggleArmed = function (data, event) {
            var next = event.target.checked;
            OctoPrint.simpleApiCommand("printbutler", "set_armed", {armed: next})
                .done(function (data) {
                    self.armed(data.armed === true);
                })
                .fail(function () {
                    self.armed(!next);
                    new PNotify({title: tr("PrintButler"), text: tr("Request failed."), type: "error"});
                });
        };

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
        self.toggleDeleteFinishedFile = function (data, event) {
            var next = event.target.checked;
            OctoPrint.simpleApiCommand("printbutler", "set_delete_finished_file_enabled", {enabled: next})
                .done(function (data) {
                    var enabled = data.delete_finished_file_enabled === true;
                    self.deleteFinishedFileEnabled(enabled);
                    if (self.settings) { self.settings.delete_finished_file_enabled(enabled); }
                })
                .fail(function () {
                    self.deleteFinishedFileEnabled(!next);
                    new PNotify({title: tr("PrintButler"), text: tr("Request failed."), type: "error"});
                });
        };

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
                retain: self.settings.shutdown_trigger_retain() === true
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
