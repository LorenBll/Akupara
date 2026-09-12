/* tooltips.js — tooltip anchoring and repositioning (hover vs validation) */
var tooltipAnchors = [];

function registerTooltipAnchor(el, target, kind, side) {
    tooltipAnchors = tooltipAnchors.filter(function (a) { return a.el !== el; });
    var anchor = { el: el, target: target, kind: kind, side: side };
    tooltipAnchors.push(anchor);
    return anchor;
}

function unregisterTooltipAnchor(el) {
    tooltipAnchors = tooltipAnchors.filter(function (a) { return a.el !== el; });
}

function getTooltipPosition(a) {
    var r = a.target.getBoundingClientRect();
    var tr = a.el.getBoundingClientRect();
    if (a.kind === 'error') {
        if (a.side === 'right') {
            return { left: r.right - tr.width, top: r.top - tr.height - 8 };
        }
        return { left: r.left + r.width / 2 - tr.width / 2, top: r.top - tr.height - 8 };
    }
    if (a.kind === 'table') {
        var row = a.target.closest('tr');
        var rowRect = row ? row.getBoundingClientRect() : r;
        var top = rowRect.top + rowRect.height / 2 - tr.height / 2;
        var left;
        if (a.side === 'left') {
            left = r.left - tr.width - 8;
            if (left < 8) left = r.right + 8;
        } else {
            left = r.right + 8;
        }
        return { left: left, top: top };
    }
    if (a.kind === 'plugin-search') {
        return { left: r.left - a.el.offsetWidth - 8, top: r.top + r.height / 2 - a.el.offsetHeight / 2 };
    }
    return { left: r.right + 8, top: r.top + r.height / 2 - tr.height / 2 };
}

function setTooltipVisible(el, visible, kind) {
    if (kind === 'plugin-search') {
        el.style.visibility = visible ? 'visible' : 'hidden';
        el.style.opacity = visible ? '1' : '0';
    } else {
        el.hidden = !visible;
    }
}

function placeTooltip(a) {
    if (!a.el || a.el.isConnected === false) return;
    if (!a.target || a.target.isConnected === false) return;
    var viewEl = document.getElementById('view');
    var confined = a.kind === 'error';
    if (viewEl && confined) {
        var targetRect = a.target.getBoundingClientRect();
        var v = viewEl.getBoundingClientRect();
        var inView = targetRect.right > v.left && targetRect.left < v.right && targetRect.bottom > v.top && targetRect.top < v.bottom;
        if (!inView) {
            setTooltipVisible(a.el, false, a.kind);
            return;
        }
    }
    setTooltipVisible(a.el, true, a.kind);
    var pos = getTooltipPosition(a);
    if (viewEl && confined) {
        var v2 = viewEl.getBoundingClientRect();
        var tooltipRect = a.el.getBoundingClientRect();
        pos.left = Math.max(v2.left, Math.min(pos.left, v2.right - tooltipRect.width));
        pos.top = Math.max(v2.top, Math.min(pos.top, v2.bottom - tooltipRect.height));
    }
    a.el.style.left = Math.round(pos.left) + 'px';
    a.el.style.top = Math.round(pos.top) + 'px';
}

function repositionTooltips() {
    var kept = [];
    for (var i = 0; i < tooltipAnchors.length; i++) {
        var a = tooltipAnchors[i];
        if (!a.el || a.el.isConnected === false || !a.target || a.target.isConnected === false) continue;
        placeTooltip(a);
        kept.push(a);
    }
    tooltipAnchors = kept;
}

var repositionScheduled = false;

function scheduleReposition() {
    if (repositionScheduled) return;
    repositionScheduled = true;
    requestAnimationFrame(function () {
        repositionScheduled = false;
        repositionTooltips();
    });
}

(function () {
    var viewEl = document.getElementById('view');
    if (viewEl) viewEl.addEventListener('scroll', scheduleReposition);
    window.addEventListener('resize', scheduleReposition);
})();
