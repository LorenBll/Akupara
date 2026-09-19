"""Flask app factory and shared decorators — extracted from main.py."""

from __future__ import annotations

import functools

import state
from logginglib import log_debug


def _options_response(allowed_methods: list[str]) -> tuple:
    from flask import jsonify  # lazy to avoid circular
    response = jsonify({})
    response.headers["Allow"] = ", ".join(allowed_methods)
    response.headers["Access-Control-Allow-Methods"] = ", ".join(allowed_methods)
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response, 200


def _head_response() -> tuple:
    from flask import jsonify
    response = jsonify({})
    return response, 200


def set_connection_header(response):
    from flask import request
    content_type = response.headers.get("Content-Type", "")
    if content_type.startswith("text/html"):
        response.headers["Connection"] = "keep-alive"
        log_debug("Connection set to keep-alive", {"path": request.path})
    else:
        response.headers["Connection"] = "close"
        log_debug("Connection set to close", {"path": request.path})
    return response


def standard_endpoint(*methods: str):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            from flask import request
            if request.method == "OPTIONS":
                log_debug("OPTIONS request handled", {"path": request.path})
                return _options_response(list(methods))
            if request.method == "HEAD":
                log_debug("HEAD request handled", {"path": request.path})
                return _head_response()
            return func(*args, **kwargs)
        return wrapper
    return decorator


def _register_ui_routes(app_instance=None):
    """Register the UI routes on the Flask app."""
    if app_instance is None:
        app_instance = state.app
    if app_instance is None:
        return
    # View functions are defined in main (the web layer); import lazily at call
    # time, once main is fully loaded.
    import main as _main
    login_page = _main.login_page
    ui_argon2_script = _main.ui_argon2_script
    ui_login_icon = _main.ui_login_icon
    index = _main.index
    ui_settings_page = _main.ui_settings_page
    ui_settings_card = _main.ui_settings_card
    ui_plugins_page = _main.ui_plugins_page
    ui_css = _main.ui_css
    ui_font = _main.ui_font
    ui_icon = _main.ui_icon
    ui_page = _main.ui_page
    ui_js = _main.ui_js
    if not state.GUI_ENABLED:
        return
    app_instance.add_url_rule("/login", methods=["GET", "HEAD", "OPTIONS"], view_func=login_page)
    app_instance.add_url_rule(
        "/ui/js/argon2/argon2-bundled.min.js",
        methods=["GET", "HEAD", "OPTIONS"],
        view_func=ui_argon2_script,
    )
    app_instance.add_url_rule(
        "/ui/icons/akupara.svg",
        methods=["GET", "HEAD", "OPTIONS"],
        view_func=ui_login_icon,
    )
    app_instance.add_url_rule("/", methods=["GET", "HEAD", "OPTIONS"], view_func=index)
    app_instance.add_url_rule(
        "/ui/pages/settings.html",
        methods=["GET", "HEAD", "OPTIONS"],
        view_func=ui_settings_page,
    )
    app_instance.add_url_rule(
        "/ui/cards/settings/<string:card_id>",
        methods=["GET", "HEAD", "OPTIONS"],
        view_func=ui_settings_card,
    )
    app_instance.add_url_rule(
        "/ui/pages/plugins.html",
        methods=["GET", "HEAD", "OPTIONS"],
        view_func=ui_plugins_page,
    )
    app_instance.add_url_rule(
        "/ui/css/<path:filename>",
        methods=["GET", "HEAD", "OPTIONS"],
        view_func=ui_css,
    )
    app_instance.add_url_rule(
        "/ui/fonts/<path:filename>",
        methods=["GET", "HEAD", "OPTIONS"],
        view_func=ui_font,
    )
    app_instance.add_url_rule(
        "/ui/icons/<path:filename>",
        methods=["GET", "HEAD", "OPTIONS"],
        view_func=ui_icon,
    )
    app_instance.add_url_rule(
        "/ui/pages/<path:filename>",
        methods=["GET", "HEAD", "OPTIONS"],
        view_func=ui_page,
    )
    app_instance.add_url_rule(
        "/ui/js/<path:filename>",
        methods=["GET", "HEAD", "OPTIONS"],
        view_func=ui_js,
    )
