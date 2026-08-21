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
        self.plugState       = ko.observable(null);
        self.sharedLightDesired = ko.observable(null);
        self.quietHoursActive   = ko.observable(null);
        self.shutdownRunning    = ko.observable(false);
        self.shutdownBusy       = ko.observable(false);
        self.logs                = ko.observableArray([]);
        self.statusPolling       = null;

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

        self.plugStateText = ko.computed(function () {
            return boolText(self.plugState(), tr("On"), tr("Off"), tr("Unknown"));
        });

        self.sharedLightText = ko.computed(function () {
            return boolText(self.sharedLightDesired(), tr("On"), tr("Off"), tr("Unknown"));
        });

        self.quietHoursText = ko.computed(function () {
            return boolText(self.quietHoursActive(), tr("Yes"), tr("No"), tr("Unknown"));
        });

        // -- Lifecycle -------------------------------------------------

        self.onBeforeBinding = function () {
            self.settings = self.settingsViewModel.settings.plugins.printbutler;
        };

        self.onSettingsShown = function () {
            self.shutdownBusy(false);
            self.refreshStatus();

            self.statusPolling = setInterval(function () {
                self.refreshStatus();
            }, 5000);
        };

        self.onSettingsHidden = function () {
            if (self.statusPolling) {
                clearInterval(self.statusPolling);
                self.statusPolling = null;
            }
        };

        // -- API -------------------------------------------------------

        self.refreshStatus = function () {
            OctoPrint.get("api/plugin/printbutler")
                .done(function (data) {
                    self.pluginVersion(data.plugin_version || "?");
                    self.mqttHelperPresent(data.mqtt_helper_present === true);
                    self.mqttConnected(data.mqtt_connected === true);
                    self.plugState(data.plug_state);
                    self.sharedLightDesired(data.shared_light_desired);
                    self.quietHoursActive(data.quiet_hours_active === true);
                    self.shutdownRunning(data.shutdown_running === true);
                    self.shutdownBusy(data.shutdown_running === true);
                    if (Array.isArray(data.logs)) {
                        self.logs(data.logs);
                        var el = document.getElementById("printbutler_log_area");
                        if (el) { el.scrollTop = el.scrollHeight; }
                    }
                });
        };

        self.shutdownNow = function () {
            if (self.shutdownBusy()) { return; }
            if (!confirm(tr("Shut down this printer now? Mains power will be cut after the configured delay."))) {
                return;
            }
            self.shutdownBusy(true);
            OctoPrint.simpleApiCommand("printbutler", "shutdown_now", {})
                .done(function (data) {
                    if (data.success) {
                        new PNotify({
                            title: tr("PrintButler"), text: tr("Shutdown initiated."),
                            type: "success", hide: true
                        });
                    } else {
                        new PNotify({
                            title: tr("PrintButler"),
                            text: data.message || tr("Could not start shutdown."),
                            type: "error"
                        });
                        self.shutdownBusy(false);
                    }
                })
                .fail(function () {
                    new PNotify({title: tr("PrintButler"), text: tr("Request failed."), type: "error"});
                    self.shutdownBusy(false);
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
        elements:     ["#settings_plugin_printbutler"]
    });
});
