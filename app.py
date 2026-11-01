import functools
import os
from typing import Any

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import database
import monitor

database.init_db()

app = Flask(__name__)
app.secret_key = os.getenv(
    "SECRET_KEY", "venator-default-secret-key-change-in-production"
)
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")

# Start the event-driven Telethon listener in a background daemon thread
monitor.start_monitor_background()


def login_required(view_func):
    """Decorator enforcing password authentication for dashboard & API routes."""

    @functools.wraps(view_func)
    def wrapped(*args: Any, **kwargs: Any):
        if not session.get("authenticated"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)

    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == ADMIN_PASSWORD:
            session["authenticated"] = True
            session.permanent = True
            return redirect(url_for("dashboard"))
        error = "Invalid administrator password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    settings = database.get_settings()
    masked_settings = dict(settings)
    raw_key = masked_settings.get("llm_api_key", "")
    masked_settings["has_api_key"] = bool(raw_key)
    masked_settings["api_key_preview"] = (
        f"{raw_key[:6]}...{raw_key[-4:]}" if len(raw_key) > 12 else ("Set" if raw_key else "Not Set")
    )
    masked_settings["llm_api_key"] = ""

    return render_template(
        "dashboard.html",
        settings=masked_settings,
        presets=database.LLM_PRESETS,
        stats=database.get_stats(),
        monitor_status=monitor.get_monitor_status(),
        orders=database.get_orders(limit=50),
        bans=database.get_ban_events(limit=20),
    )


@app.route("/api/state", methods=["GET"])
@login_required
def api_state():
    """Lightweight JSON state endpoint called only when the Dashboard tab is visible."""
    filter_type = request.args.get("filter", "all")
    settings = database.get_settings()
    return jsonify({
        "stats": database.get_stats(),
        "monitor": monitor.get_monitor_status(),
        "is_paused": settings.get("is_paused", False),
        "active_model": settings.get("llm_model", ""),
        "active_provider": settings.get("llm_provider", ""),
        "orders": database.get_orders(filter_type=filter_type, limit=50),
        "bans": database.get_ban_events(limit=20),
    })


@app.route("/api/settings", methods=["POST"])
@login_required
def api_update_settings():
    """Update AI settings with strict live verification ping."""
    data = request.get_json(silent=True) or {}
    current = database.get_settings()

    updates: dict[str, Any] = {}
    for field in (
        "is_paused",
        "target_folder",
        "folder_invite_link",
        "llm_provider",
        "llm_api_url",
        "llm_model",
        "min_score",
        "target_keywords",
        "stop_keywords",
        "system_prompt",
    ):
        if field in data:
            updates[field] = data[field]

    new_key = str(data.get("llm_api_key", "")).strip()
    test_key = new_key if new_key else current.get("llm_api_key", "")
    updates["llm_api_key"] = test_key

    # Live verification ping before saving
    test_cfg = dict(current)
    test_cfg.update(updates)
    test_res = monitor.analyze_post_with_llm(
        "Ping test: verify model availability and JSON format", test_cfg
    )

    if not test_res or "_error" in test_res:
        err_msg = test_res.get("_error") if test_res else "Empty response from provider"
        return jsonify({
            "ok": False,
            "error": f"LLM Verification Failed: {err_msg}"
        }), 400

    updated = database.update_settings(updates)

    if monitor.get_monitor_status().get("status") == "ONLINE":
        monitor.sync_target_folder()

    raw_key = updated.get("llm_api_key", "")
    key_preview = (
        f"{raw_key[:6]}...{raw_key[-4:]}" if len(raw_key) > 12 else ("Set" if raw_key else "Not Set")
    )

    return jsonify({
        "ok": True,
        "active_model": updated["llm_model"],
        "active_provider": updated["llm_provider"],
        "is_paused": updated["is_paused"],
        "api_key_preview": key_preview,
    })


@app.route("/api/test-llm", methods=["POST"])
@login_required
def api_test_llm():
    """Run a live dry-run evaluation against the active AI model from the Dashboard."""
    data = request.get_json(silent=True) or {}
    sample_text = str(data.get("text", "")).strip()
    send_tg = bool(data.get("send_telegram", False))

    if not sample_text:
        return jsonify({"ok": False, "error": "Sample text cannot be empty."}), 400

    result = monitor.analyze_post_with_llm(sample_text)
    if not result:
        return jsonify({"ok": False, "error": "Empty response from LLM."}), 500
    if "_error" in result:
        return jsonify({"ok": False, "error": result["_error"]}), 502

    tg_sent = False
    if send_tg:
        score = result.get("match_score", 0)
        summary = result.get("summary", "Test summary")
        reason = result.get("reason", "Test reason")
        pitch = result.get("recommended_pitch", "A")
        model_used = result.get("_model_used", "unknown")
        tg_sent = monitor.send_bot_notification(
            f"🧪 <b>Venator AI Sandbox Test ({score}%)</b>\n"
            f"📝 <b>Summary:</b> {summary}\n"
            f"✅ <b>Verdict:</b> {reason}\n"
            f"📨 <b>Pitch:</b> <code>{pitch}</code>\n"
            f"🧠 <b>Model:</b> <code>{model_used}</code>"
        )

    return jsonify({"ok": True, "result": result, "telegram_sent": tg_sent})


@app.route("/api/clear-orders", methods=["POST"])
@login_required
def api_clear_orders():
    """Delete all stored order evaluation records."""
    database.clear_orders()
    return jsonify({"ok": True})


@app.route("/api/auth/qr", methods=["POST"])
@login_required
def api_auth_qr():
    """Generate a Telegram login QR code directly in the Web Dashboard."""
    res = monitor.request_qr_login()
    return jsonify(res)


@app.route("/api/auth/2fa", methods=["POST"])
@login_required
def api_auth_2fa():
    """Submit Telegram 2FA Cloud Password from the Web Dashboard."""
    data = request.get_json(silent=True) or {}
    password = str(data.get("password", "")).strip()
    if not password:
        return jsonify({"ok": False, "error": "Password cannot be empty."}), 400
    res = monitor.submit_2fa_password(password)
    return jsonify(res)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5006"))
    app.run(host="127.0.0.1", port=port, debug=False)