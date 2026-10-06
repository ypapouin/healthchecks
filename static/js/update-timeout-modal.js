$(function () {
    var base = document.getElementById("base-url").getAttribute("href").slice(0, -1);
    var period = document.getElementById("period-value");
    var periodUnit = document.getElementById("period-unit");
    var grace = document.getElementById("grace-value");
    var graceUnit = document.getElementById("grace-unit");
    var graceCron = document.getElementById("update-timeout-grace-cron");
    var graceCronUnit = document.getElementById("update-timeout-grace-cron-unit");
    var graceOncalendar = document.getElementById("update-timeout-grace-oncalendar");
    var graceOncalendarUnit = document.getElementById("update-timeout-grace-oncalendar-unit");


    $(".rw .timeout-grace").click(function() {
        var code = $(this).closest("tr.checks-row").attr("id");
        if (!code) {
            code = this.dataset.code;
        }

        var url = base + "/checks/" + code + "/timeout/";

        $("#update-timeout-form").attr("action", url);
        $("#update-cron-form").attr("action", url);
        $("#update-oncalendar-form").attr("action", url);

        // Simple, period
        setPeriod(this.dataset.timeout);
        periodSlider.noUiSlider.set(this.dataset.timeout);

        // Simple, grace
        setGrace(this.dataset.grace);
        graceSlider.noUiSlider.set(this.dataset.grace);

        // Cron
        cronPreviewHash = "";
        $("#cron-preview").html("<p>Updating...</p>");
        $("#schedule").val(this.dataset.kind == "cron" ? this.dataset.schedule: "* * * * *");
        $("#tz")[0].tomselect.setValue(this.dataset.tz, true);
        setDurationInput(graceCron, graceCronUnit, this.dataset.grace);
        $("#update-cron-grace").val(this.dataset.grace);
        updateCronPreview();

        // OnCalendar
        onCalendarPreviewHash = "";
        $("#oncalendar-preview").html("<p>Updating...</p>");
        $("#schedule-oncalendar").val(this.dataset.kind == "oncalendar" ? this.dataset.schedule: "*-*-* *:*:*");
        $("#tz-oncalendar")[0].tomselect.setValue(this.dataset.tz, true);
        setDurationInput(graceOncalendar, graceOncalendarUnit, this.dataset.grace);
        $("#update-oncalendar-grace").val(this.dataset.grace);
        updateOnCalendarPreview();

        showPanel(this.dataset.kind);
        $('#update-timeout-modal').modal({"show":true, "backdrop":"static"});
        return false;
    });

    var pipLabels = {
        10: "10 seconds",
        60: "1 minute",
        1800: "30 minutes",
        3600: "1 hour",
        43200: "12 hours",
        86400: "1 day",
        604800: "1 week",
        2592000: "30 days",
        31536000: "365 days"
    }

    var periodSlider = document.getElementById("period-slider");
    noUiSlider.create(periodSlider, {
        start: [60],
        connect: "lower",
        range: {
            'min': [10, 10],
            '15%': [60, 60],
            '35%': [3600, 3600],
            '60%': [86400, 86400],
            '75%': [604800, 86400],
            '90%': [2592000, 2592000],
            'max': 31536000
        },
        pips: {
            mode: 'values',
            values: [10, 60, 1800, 3600, 43200, 86400, 604800, 2592000, 31536000],
            density: 4,
            format: {
                to: function(v) { return pipLabels[v] },
                from: function() {}
            }
        }
    });

    function setPeriod(secs) {
        // Set the hidden form field
        $("#update-timeout-timeout").val(secs);
        // Set the visible value+units form fields
        setDurationInput(period, periodUnit, secs);
    }

    // Update inputs and the hidden field when user slides the period slider
    periodSlider.noUiSlider.on("slide", function(a, b, value) {
        setPeriod(Math.round(value));
    });

    // Update slider, inputs and the hidden field when user clicks slider labels
    $("#period-slider .noUi-value").on("click", function() {
        periodSlider.noUiSlider.set(this.dataset.value);
        setPeriod(this.dataset.value);
    })

    // Update the slider and the hidden field when user changes period inputs
    $("#update-timeout-modal .period-input").on("keyup change", function() {
        var secs = readDurationInput(period, periodUnit);
        if (secs !== null) {
            periodSlider.noUiSlider.set(secs);
            $("#update-timeout-timeout").val(secs);
        }
    })

    var graceSlider = document.getElementById("grace-slider");
    noUiSlider.create(graceSlider, {
        start: [60],
        connect: "lower",
        range: {
            'min': [10, 10],
            '15%': [60, 60],
            '35%': [3600, 3600],
            '60%': [86400, 86400],
            '75%': [604800, 86400],
            '90%': [2592000, 2592000],
            'max': 31536000
        },
        pips: {
            mode: 'values',
            values: [10, 60, 1800, 3600, 43200, 86400, 604800, 2592000, 31536000],
            density: 4,
            format: {
                to: function(v) { return pipLabels[v] },
                from: function() {}
            }
        }
    });

    function setGrace(secs) {
        // Set the hidden form field
        $("#update-timeout-grace").val(secs);
        // Set the visible value+units form fields
        setDurationInput(grace, graceUnit, secs);
    }

    // Update inputs and the hidden field when user slides the grace slider
    graceSlider.noUiSlider.on("slide", function(a, b, value) {
        setGrace(Math.round(value));
    });

    // Update slider, inputs and the hidden field when user clicks slider labels
    $("#grace-slider .noUi-value").on("click", function() {
        graceSlider.noUiSlider.set(this.dataset.value);
        setGrace(this.dataset.value);
    })

    // Update the slider and the hidden field when user changes grace inputs
    $("#update-timeout-modal .grace-input").on("keyup change", function() {
        var secs = readDurationInput(grace, graceUnit);
        if (secs !== null) {
            graceSlider.noUiSlider.set(secs);
            $("#update-timeout-grace").val(secs);
        }
    });

    function showPanel(kind) {
        $("#update-timeout-form").toggle(kind == "simple");
        $("#update-cron-form").toggle(kind == "cron");
        $("#update-oncalendar-form").toggle(kind == "oncalendar");
    }

    var cronPreviewHash = "";
    function updateCronPreview() {
        var schedule = $("#schedule").val();
        var tz = $("#tz").val();
        var hash = schedule + tz;

        // Don't try preview with empty values, or if values have not changed
        if (!schedule || !tz || hash == cronPreviewHash)
            return;

        // OK, we're good
        cronPreviewHash = hash;
        $("#cron-preview-title").text("Updating...");

        var token = $('input[name=csrfmiddlewaretoken]').val();
        $.ajax({
            url: base + "/checks/cron_preview/",
            type: "post",
            headers: {"X-CSRFToken": token},
            data: {schedule: schedule, tz: tz},
            success: function(data) {
                if (hash != cronPreviewHash) {
                    return;  // ignore stale results
                }

                $("#cron-preview" ).html(data);
                var haveError = $("#invalid-arguments").length > 0;
                $("#update-cron-submit").prop("disabled", haveError);
            }
        });
    }

    var onCalendarPreviewHash = "";
    function updateOnCalendarPreview() {
        var schedule = $("#schedule-oncalendar").val();
        var tz = $("#tz-oncalendar").val();
        var hash = schedule + tz;

        // Don't try preview with empty values, or if values have not changed
        if (!schedule || !tz || hash == onCalendarPreviewHash)
            return;

        // OK, we're good
        onCalendarPreviewHash = hash;
        $("#oncalendar-preview-title").text("Updating...");

        var token = $('input[name=csrfmiddlewaretoken]').val();
        $.ajax({
            url: base + "/checks/oncalendar_preview/",
            type: "post",
            headers: {"X-CSRFToken": token},
            data: {schedule: schedule, tz: tz},
            success: function(data) {
                if (hash != onCalendarPreviewHash) {
                    return;  // ignore stale results
                }

                $("#oncalendar-preview" ).html(data);
                var haveError = $("#invalid-oncalendar-arguments").length > 0;
                $("#update-oncalendar-submit").prop("disabled", haveError);
            }
        });
    }

    $("#update-timeout-modal .update-timeout-grace-cron-input").on("keyup change", function() {
        var secs = readDurationInput(graceCron, graceCronUnit);
        if (secs !== null) {
            $("#update-cron-grace").val(secs);
        }
    });

    $("#update-timeout-modal .update-timeout-grace-oncalendar-input").on("keyup change", function() {
        var secs = readDurationInput(graceOncalendar, graceOncalendarUnit);
        if (secs !== null) {
            $("#update-oncalendar-grace").val(secs);
        }
    });

    // Wire up events for Timeout/Cron forms
    $(".kind-simple").click(() => showPanel("simple"));
    $(".kind-cron").click(() => showPanel("cron"));
    $(".kind-oncalendar").click(() => showPanel("oncalendar"));

    $("#schedule").on("keyup", updateCronPreview);
    $("#schedule-oncalendar").on("keyup", updateOnCalendarPreview);
    $("#tz").on("change", updateCronPreview);
    $("#tz-oncalendar").on("change", updateOnCalendarPreview);

});
