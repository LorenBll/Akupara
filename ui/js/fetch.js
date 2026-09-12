/* fetch.js — centralised authenticated fetch and health polling */
function fetchWithHandshake(url, options) {
    options = options || {};
    options.credentials = 'same-origin';
    return fetch(url, options).then(function (r) {
        if (r.status === 401) {
            window.location.href = '/login';
        }
        return r;
    });
}

function populatePill() {
    fetch('/api/health')
        .then(function (r) { return r.json(); })
        .then(function (data) {
            ['hostname', 'bind_address', 'port', 'pid', 'status'].forEach(function (key) {
                if (data[key] == null) return;
                var els = document.querySelectorAll('.pill-value[data-field="' + key + '"]');
                Array.prototype.forEach.call(els, function (el) {
                    if (key === 'status') {
                        var value = String(data[key]).toUpperCase();
                        el.textContent = value;
                        el.classList.remove('pill-value-ok', 'pill-value-bad');
                        el.classList.add(value === 'OK' ? 'pill-value-ok' : 'pill-value-bad');
                    } else {
                        el.textContent = data[key];
                    }
                });
            });
        })
        .catch(function () {});
}
