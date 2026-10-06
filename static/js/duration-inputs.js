function readDurationInput(input, unit) {
    var seconds = Math.round(Number(input.value) * Number(unit.value));
    input.min = unit.value === "1" ? "10" : "1";
    var error = "";
    if (!Number.isFinite(seconds) || seconds < 10) {
        error = "Must be at least 10 seconds";
    } else if (seconds > 31536000) {
        error = "Must not exceed 365 days";
    }
    input.setCustomValidity(error);
    return input.validity.valid ? seconds : null;
}

function setDurationInput(input, unit, seconds) {
    seconds = Number(seconds);
    var units = [86400, 3600, 60, 1];
    var divisor = units.find(value => seconds % value === 0);
    input.value = seconds / divisor;
    unit.value = divisor;
    readDurationInput(input, unit);
}
