function updateDependencyStatus(element, dependency) {
    if (!element) return;
    var label = document.createElement("span");
    if (dependency.state === "waiting") {
        label.className = "label label-warning";
        label.textContent = "Waiting for " + (dependency.blockers.map(b => b.name).join(", ") || "parent verification");
    } else if (dependency.state === "resuming") {
        label.className = "label label-info";
        label.dataset.deadline = dependency.notification_after;
        label.textContent = "Recovery grace until " + dependency.notification_after;
    } else if (dependency.state === "claimed") {
        label.className = "checks-subline";
        label.textContent = "Alert claimed for delivery";
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
    $("#dependency-children").on("change", function () {
        var replaced = Array.from(this.selectedOptions).filter(o => o.textContent.includes("replaces parent:"));
        $("#dependency-replacements").text(replaced.map(o => o.textContent).join("; "));
    }).trigger("change");

    function countdown() {
        document.querySelectorAll("[data-deadline]").forEach(function (el) {
            var seconds = Math.max(0, Math.ceil((Date.parse(el.dataset.deadline) - Date.now()) / 1000));
            el.textContent = "Recovery grace: " + Math.floor(seconds / 60).toString().padStart(2, "0") + ":" + (seconds % 60).toString().padStart(2, "0");
        });
    }
    countdown();
    setInterval(countdown, 1000);
});
