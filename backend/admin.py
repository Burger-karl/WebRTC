"""
admin.py — SaaS Admin Dashboard Blueprint

Fixes applied in this version:
  Bug 1: require_admin returned an HTML redirect for API routes.
    fetch() followed the redirect, received <!DOCTYPE html>, and
    res.json() threw "Unexpected token '<'".
    Fix: detect API routes by URL path. For /admin/api/* routes,
    return 401 JSON instead of an HTML redirect.

  Bug 2: API fetch calls did not check res.ok before res.json().
    Fix applied in admin-dashboard.html: all three fetch calls now
    check res.status === 401 and show a login prompt instead of
    trying to parse HTML as JSON.
"""

import logging
import time
import functools
from datetime import datetime

from flask import (
    Blueprint, request, jsonify, render_template,
    redirect, url_for, session
)

from config import cfg
import database as db

logger = logging.getLogger("meetfree.admin")

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

_login_attempts: dict = {}


def _check_login_rate_limit(ip: str) -> bool:
    now    = time.time()
    bucket = [t for t in _login_attempts.get(ip, []) if now - t < 60]
    if len(bucket) >= 5:
        _login_attempts[ip] = bucket
        return False
    bucket.append(now)
    _login_attempts[ip] = bucket
    return True


def _verify_admin_password(password: str) -> bool:
    try:
        import bcrypt
        return bcrypt.checkpw(
            password.encode('utf-8'),
            cfg.ADMIN_PASSWORD_HASH.encode('utf-8')
        )
    except Exception as e:
        logger.error(f"[ADMIN] Password verification error: {e}")
        return False


def _is_api_request() -> bool:
    """
    Returns True if the current request is an API call (/admin/api/*).
    API requests must receive JSON errors — never HTML redirects.
    An HTML redirect causes fetch() to follow it, receive <!DOCTYPE html>,
    and crash when res.json() tries to parse it.
    """
    return request.path.startswith('/admin/api/')


def require_admin(f):
    """
    Protects admin routes.

    FIX: For API routes (/admin/api/*), returns 401 JSON instead of an
    HTML redirect. This prevents the dashboard JavaScript from receiving
    <!DOCTYPE html> when the session expires and trying to parse it as JSON.

    Page routes (/admin/dashboard, etc.) still get the HTML redirect to
    the login page as expected.
    """
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('admin_logged_in'):
            if _is_api_request():
                # FIX: return 401 JSON for API routes — never an HTML redirect
                return jsonify({
                    "error":       "Not authenticated",
                    "redirectTo":  "/admin/login"
                }), 401
            return redirect(url_for('admin.login'))

        login_time = session.get('admin_login_time', 0)
        if time.time() - login_time > cfg.ADMIN_SESSION_TIMEOUT:
            session.clear()
            if _is_api_request():
                # FIX: return 401 JSON for API routes — session expired
                return jsonify({
                    "error":      "Session expired",
                    "redirectTo": "/admin/login?expired=1"
                }), 401
            return redirect(url_for('admin.login') + '?expired=1')

        return f(*args, **kwargs)
    return wrapper


# ── HTTP Routes ───────────────────────────────────────────────────────────────

@admin_bp.route('/login', methods=['GET', 'POST'])
def login():
    expired = request.args.get('expired') == '1'

    if request.method == 'POST':
        ip       = request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        if not _check_login_rate_limit(ip):
            return render_template('admin-login.html',
                                   error='Too many login attempts. Wait 1 minute.',
                                   expired=False), 429

        if username == cfg.ADMIN_USERNAME and _verify_admin_password(password):
            session['admin_logged_in'] = True
            session['admin_login_time'] = time.time()
            session['admin_username']   = username
            session.permanent = False
            logger.info(f"[ADMIN] Login: {username} from {ip}")
            return redirect(url_for('admin.dashboard'))
        else:
            logger.warning(f"[ADMIN] Failed login: username='{username}' ip={ip}")
            return render_template('admin-login.html',
                                   error='Invalid username or password.',
                                   expired=False), 401

    return render_template('admin-login.html', error=None, expired=expired)


@admin_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('admin.login'))


@admin_bp.route('/dashboard')
@require_admin
def dashboard():
    return render_template('admin-dashboard.html',
                           admin_username=session.get('admin_username', 'Admin'))


# ── Admin JSON APIs ───────────────────────────────────────────────────────────

@admin_bp.route('/api/stats')
@require_admin
def admin_stats():
    """
    Live server stats polled every 5 seconds by the dashboard JS.
    FIX: require_admin now returns 401 JSON when session is missing/expired
    so the dashboard JS receives a proper error it can handle — not HTML.
    """
    try:
        from server import rooms
        active_rooms = len(rooms)
        active_peers = sum(len(v) for v in rooms.values())
    except Exception:
        active_rooms = 0
        active_peers = 0

    cpu_percent   = 0.0
    mem_percent   = 0.0
    mem_used_mb   = 0
    mem_total_mb  = 0
    disk_percent  = 0.0
    disk_used_gb  = 0
    disk_total_gb = 0

    try:
        import psutil
        cpu_percent   = psutil.cpu_percent(interval=0.1)
        mem           = psutil.virtual_memory()
        mem_percent   = mem.percent
        mem_used_mb   = round(mem.used  / 1024 / 1024)
        mem_total_mb  = round(mem.total / 1024 / 1024)
        disk          = psutil.disk_usage('/')
        disk_percent  = disk.percent
        disk_used_gb  = round(disk.used  / 1024 / 1024 / 1024, 1)
        disk_total_gb = round(disk.total / 1024 / 1024 / 1024, 1)
    except ImportError:
        logger.warning("[ADMIN] psutil not installed — resource stats unavailable")
    except Exception as e:
        logger.warning(f"[ADMIN] psutil error: {e}")

    total_users  = db.get_user_count(cfg)
    total_orders = len(db.get_all_orders(cfg))
    paid_orders  = len(db.get_all_orders(cfg, status_filter='paid'))

    return jsonify({
        "active_rooms": active_rooms,
        "active_peers": active_peers,
        "total_users":  total_users,
        "total_orders": total_orders,
        "paid_orders":  paid_orders,
        "mysql":        db._db_available,
        "timestamp":    datetime.utcnow().strftime('%H:%M:%S UTC'),
        "resources": {
            "cpu_percent":  cpu_percent,
            "mem_percent":  mem_percent,
            "mem_used_mb":  mem_used_mb,
            "mem_total_mb": mem_total_mb,
            "disk_percent": disk_percent,
            "disk_used_gb": disk_used_gb,
            "disk_total_gb": disk_total_gb,
        }
    })


@admin_bp.route('/api/users')
@require_admin
def admin_users():
    users = db.get_all_users(cfg)
    return jsonify({"users": users, "total": len(users)})


@admin_bp.route('/api/orders')
@require_admin
def admin_orders():
    status = request.args.get('status')
    orders = db.get_all_orders(cfg, status_filter=status)
    result = []
    for o in orders:
        result.append({
            "id":                    o["id"],
            "client_name":           o["client_name"],
            "client_email":          o["client_email"],
            "service_type":          o["service_type"],
            "payment_status":        o["payment_status"],
            "stripe_transaction_id": o.get("stripe_transaction_id") or "—",
            "room_id":               o.get("room_id") or "—",
            "meeting_link":          o.get("meeting_link") or "",
            "scheduled_time":        str(o.get("scheduled_time") or ""),
            "created_at":            str(o.get("created_at") or ""),
        })
    return jsonify({"orders": result, "total": len(result)})