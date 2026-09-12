/* validation.js — required-input validation helpers (Required Input Validation Standard) */
function showErrorTooltip(target, message, side) {
    var tooltip = document.getElementById('errorTooltip');
    if (!tooltip || !target || !message) return;
    tooltip.textContent = message;
    placeTooltip(registerTooltipAnchor(tooltip, target, 'error', side));
}

function hideErrorTooltip() {
    var tooltip = document.getElementById('errorTooltip');
    if (tooltip) {
        tooltip.hidden = true;
        unregisterTooltipAnchor(tooltip);
    }
}

var transientErrorTimer = null;

function showTransientError(input, invalidClass, message, side) {
    if (transientErrorTimer) {
        clearTimeout(transientErrorTimer);
        transientErrorTimer = null;
    }
    input.classList.add(invalidClass);
    showErrorTooltip(input, message, side);
    transientErrorTimer = setTimeout(function () {
        transientErrorTimer = null;
        input.classList.remove(invalidClass);
        hideErrorTooltip();
    }, 3000);
}
