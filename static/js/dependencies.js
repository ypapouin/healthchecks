function updateRecoveryTooltip(element) {
    var seconds = Math.max(0, Math.ceil((Date.parse(element.dataset.deadline) - Date.now()) / 1000));
    var text = "Recovery grace: " + Math.floor(seconds / 60).toString().padStart(2, "0") + ":" + (seconds % 60).toString().padStart(2, "0");
    element.title = text;
    element.setAttribute("aria-label", text);
}

function updateDependencyStatus(element, dependency) {
    if (!element) return;
    var label = document.createElement("span");
    if (dependency.state === "waiting") {
        var waitingFor = "Waiting for " + (dependency.blockers.map(b => b.name).join(", ") || "parent verification");
        label.className = "alert-waiting";
        label.setAttribute("role", "img");
        label.setAttribute("aria-label", waitingFor);
        label.title = waitingFor;
    } else if (dependency.state === "resuming") {
        label.className = "alert-resuming ic-timer";
        label.setAttribute("role", "img");
        label.dataset.deadline = dependency.notification_after;
        updateRecoveryTooltip(label);
    } else if (dependency.state === "claimed") {
        label.className = "checks-subline alert-claimed";
        label.setAttribute("role", "img");
        label.setAttribute("aria-label", "Alert claimed for delivery");
        label.title = "Alert claimed for delivery";
    } else if (dependency.state === "cancelled") {
        label.textContent = "Alert cancelled";
    }
    // Avoid live-region announcements when only an unrelated ping changed.
    if (element.innerHTML !== label.outerHTML) element.replaceChildren(label);
}

$(function () {
    document.querySelectorAll("select.dependency-select").forEach(function (el) {
        new TomSelect(el, {create: false, allowEmptyOption: true, plugins: el.multiple ? ["remove_button"] : []});
    });
    var parentSelect = document.getElementById("dependency-parent");
    if (parentSelect) {
        var initialParent = parentSelect.value;
        var saveParent = document.getElementById("dependency-parent-save");
        function updateParentSave() {
            saveParent.hidden = parentSelect.value === initialParent;
        }
        parentSelect.addEventListener("change", updateParentSave);
        updateParentSave();
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
