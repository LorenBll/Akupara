"""External-access worker that serves Akupara on the device's IP."""

from __future__ import annotations

import threading

from werkzeug.exceptions import NotFound
from werkzeug.serving import make_server

from logginglib import log_debug, log_info

_MARKER = "_network_worker_callable"


def network_worker_callable(func):
    """Mark an endpoint or method as callable by the network worker.

    The endpoints and methods marked with this decorator are, together, the only
    ones the network worker is allowed to serve: any other request reaching the
    network worker is answered with a 404 (byte-identical to a non-existent
    endpoint), regardless of whether external access is enabled. Methods marked
    this way act as a signal for callers that may invoke them on behalf of the
    network worker.
    """
    setattr(func, _MARKER, True)
    return func


def _guarded_app(app, ip_policy=None):
    """Wrap ``app`` so only marked endpoints are served by the network worker.

    When ``ip_policy`` is given, it is a callable taking the remote address and
    returning whether the request may be served at all; refusals are answered
    with a 404 byte-identical to a non-existent endpoint.
    """

    def wrapper(environ, start_response):
        if ip_policy is not None:
            remote = environ.get("REMOTE_ADDR") or ""
            if not ip_policy(remote):
                return NotFound().get_response(environ)(environ, start_response)
        endpoint = None
        try:
            endpoint, _values = app.url_map.bind_to_environ(environ).match()
        except Exception:  # noqa: BLE001
            endpoint = None
        view = app.view_functions.get(endpoint) if endpoint else None
        if view is None or not getattr(view, _MARKER, False):
            return NotFound().get_response(environ)(environ, start_response)
        return app(environ, start_response)

    return wrapper


class ExternalAccessWorker:
    """Serves the Akupara app on the device's IP, in parallel with the loopback worker.

    This worker has no sub-workers: it runs a single server in a single thread.
    """

    def __init__(self, app, host: str, port: int, ip_policy=None) -> None:
        self._app = app
        self._host = host
        self._port = int(port)
        self._ip_policy = ip_policy
        self._server = None
        self._thread = None

    def start(self) -> None:
        """Start serving the app on the device's IP and port (idempotent)."""
        if self._server is not None:
            return
        server = make_server(self._host, self._port, _guarded_app(self._app, self._ip_policy), threaded=True)
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever,
            name="external-access-worker",
            daemon=True,
        )
        self._thread.start()
        log_info("External access worker started", {"host": self._host, "port": self._port})

    def stop(self) -> None:
        """Stop serving the app and release the socket (idempotent)."""
        server = self._server
        self._server = None
        thread = self._thread
        self._thread = None
        if server is not None:
            try:
                server.shutdown()
            except Exception as exc:  # noqa: BLE001
                log_debug("External access worker shutdown error", {"error": str(exc)})
        if thread is not None:
            thread.join(timeout=5)
        log_info("External access worker stopped")