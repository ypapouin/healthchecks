function setDependencyTooltip(element, text) {
    if (element.getAttribute("aria-label") === text) return;
    element.setAttribute("aria-label", text);
    var tooltip = $(element).data("bs.tooltip");
    if (tooltip && tooltip.$tip && tooltip.$tip.hasClass("in")) {
        // Refresh the content and position of an already visible bubble.
        tooltip.show();
    }
}

function updateRecoveryTooltip(element) {
    var seconds = Math.max(0, Math.ceil((Date.parse(element.dataset.deadline) - Date.now()) / 1000));
    var text = "Recovery grace: " + Math.floor(seconds / 60).toString().padStart(2, "0") + ":" + (seconds % 60).toString().padStart(2, "0");
    setDependencyTooltip(element, text);
}

function updateDependencyStatus(element, dependency) {
    if (!element) return;
    var label = document.createElement("span");
    if (dependency.state === "waiting") {
        var waitingFor = "Waiting for " + (dependency.blockers.map(b => b.label || b.name).join(", ") || "parent verification");
        label.className = "alert-waiting";
        label.setAttribute("role", "img");
        label.setAttribute("aria-label", waitingFor);
    } else if (dependency.state === "resuming") {
        label.className = "alert-resuming ic-timer";
        label.setAttribute("role", "img");
        label.dataset.deadline = dependency.notification_after;
        updateRecoveryTooltip(label);
    } else if (dependency.state === "claimed") {
        label.className = "checks-subline alert-claimed";
        label.setAttribute("role", "img");
        label.setAttribute("aria-label", "Alert claimed for delivery");
    } else if (dependency.state === "cancelled") {
        label.textContent = "Alert cancelled";
    }
    if (label.getAttribute("role") === "img") label.tabIndex = 0;

    // Keep the hovered/focused icon and its bubble across status refreshes.
    var current = element.firstElementChild;
    if (current && current.className === label.className && current.getAttribute("role") === label.getAttribute("role")) {
        if (label.dataset.deadline) current.dataset.deadline = label.dataset.deadline;
        if (label.getAttribute("role") === "img") {
            setDependencyTooltip(current, label.getAttribute("aria-label"));
        } else if (current.textContent !== label.textContent) {
            current.textContent = label.textContent;
        }
        return;
    }
    if (current && $(current).data("bs.tooltip")) $(current).tooltip("destroy");
    element.replaceChildren(label);
}

$(function () {
    $(".dependency-status, #dependency-status").tooltip({
        container: "body",
        selector: '[role="img"]',
        animation: false,
        html: false,
        title: function () {
            return this.getAttribute("aria-label");
        }
    });

    document.querySelectorAll("select.dependency-select").forEach(function (el) {
        new TomSelect(el, {create: false, allowEmptyOption: true, plugins: el.multiple ? ["remove_button"] : []});
    });
    var parentSelect = document.getElementById("dependency-parent");
    if (parentSelect) {
        var initialParent = parentSelect.value;
        var saveParent = document.getElementById("dependency-parent-save");
        function updateParentSave() {
            saveParent.hidden = parentSelect.value === initialParent;
            document.getElementById("dependency-parent-sharing-help").hidden = !parentSelect.value.startsWith("shared:");
        }
        parentSelect.addEventListener("change", updateParentSave);
        updateParentSave();
        $(document).on("dependencies:updated", function (event, data) {
            var parent = data.dependency.parent;
            var value = parent?.id || "";
            if (parentSelect.value === initialParent) {
                if (parent?.id && !parentSelect.tomselect.options[value]) {
                    parentSelect.tomselect.addOption({value: value, text: parent.label});
                }
                parentSelect.tomselect.setValue(value, true);
            }
            initialParent = value;
            updateParentSave();
        });
    }
    var shared = document.getElementById("dependency-shared");
    if (shared) {
        var initialShared = shared.checked;
        var saveSharing = document.getElementById("dependency-sharing-save");
        function updateSharingSave() {
            saveSharing.hidden = shared.disabled || shared.checked === initialShared;
        }
        shared.addEventListener("change", updateSharingSave);
        updateSharingSave();
        $(document).on("dependencies:updated", function (event, data) {
            if (shared.checked === initialShared) shared.checked = data.shared;
            initialShared = data.shared;
            shared.disabled = !data.can_share;
            saveSharing.disabled = !data.can_share;
            updateSharingSave();
        });
    }
    var addParent = document.getElementById("add-check-parent");
    if (addParent) {
        addParent.addEventListener("change", function () {
            document.getElementById("add-check-parent-sharing-help").hidden = !this.value.startsWith("shared:");
        });
    }
    $("#dependency-children").on("change", function () {
        var replaced = Array.from(this.selectedOptions).filter(o => o.textContent.includes("replaces parent:"));
        $("#dependency-replacements").text(replaced.map(o => o.textContent).join("; "));
    }).trigger("change");

    function countdown() {
        document.querySelectorAll(".alert-resuming[data-deadline]").forEach(updateRecoveryTooltip);
    }
    countdown();
    setInterval(countdown, 1000);
});
