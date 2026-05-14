"""
bookings.py — Order Booking & Automated Meeting Scheduling

ROOT CAUSE FIXES:
  1. order_success() now confirms payment server-side using the Stripe session
     so orders show as 'paid' immediately even without a webhook (localhost testing).
  2. order_success() redirects to /dashboard?email=xxx so the dashboard
     pre-fills the email and loads orders automatically.
  3. /api/my-orders returns ALL orders for the email (pending + paid + failed).
  4. Webhook still works in production for reliability.
"""

import logging
import uuid
from datetime import datetime, timedelta

import stripe
from flask import Blueprint, request, jsonify, render_template, redirect

from config import cfg
import database as db

logger = logging.getLogger("meetfree.bookings")
stripe.api_key = cfg.STRIPE_SECRET_KEY

bookings_bp = Blueprint("bookings", __name__)

SERVICE_TYPES = {
    "web_app":      "Web Application Development",
    "mobile_app":   "Mobile App Development",
    "api_backend":  "API / Backend Development",
    "ui_ux_design": "UI/UX Design",
    "consultation": "Technical Consultation",
    "code_review":  "Code Review & Audit",
    "devops":       "DevOps / Cloud Setup",
    "other":        "Other / Custom Project",
}


def _generate_room_id() -> str:
    return f"order-{uuid.uuid4().hex[:8]}"


def _build_meeting_link(room_id: str) -> str:
    return f"{cfg.APP_BASE_URL}/room/{room_id}"


def _calculate_scheduled_time() -> datetime:
    return datetime.utcnow() + timedelta(hours=cfg.MEETING_SCHEDULE_HOURS_AFTER)


# ── Pages ─────────────────────────────────────────────────────────────────────

@bookings_bp.route("/order")
def order_page():
    return render_template(
        "order.html",
        service_types=SERVICE_TYPES,
        stripe_publishable_key=cfg.STRIPE_PUBLISHABLE_KEY,
        meeting_duration=cfg.MEETING_DURATION_MINUTES,
    )


@bookings_bp.route("/dashboard")
def dashboard_page():
    return render_template("dashboard.html")


@bookings_bp.route("/order/success")
def order_success():
    """
    FIX 1 + FIX 2:
    Stripe redirects here after payment. We now:
      1. Retrieve the Stripe session server-side to confirm payment
      2. Call confirm_order_payment() ourselves — so the order shows as
         'paid' immediately without needing the webhook (critical for localhost)
      3. Redirect to /dashboard?email=xxx so the dashboard loads automatically
    """
    session_id = request.args.get("session_id", "")
    order      = None
    email      = ""

    if session_id and cfg.STRIPE_SECRET_KEY:
        try:
            # Retrieve session from Stripe to confirm actual payment status
            stripe_session = stripe.checkout.Session.retrieve(session_id)
            email = (
                stripe_session.get("customer_email") or
                (stripe_session.get("customer_details") or {}).get("email", "")
            )

            if stripe_session.get("payment_status") == "paid":
                scheduled_time = _calculate_scheduled_time()

                # Confirm payment in DB — idempotent, safe to call even if
                # the webhook already ran. Uses UPDATE ... WHERE payment_status='pending'
                # so it only updates once.
                db.confirm_order_payment(
                    cfg,
                    stripe_session_id     = session_id,
                    stripe_transaction_id = stripe_session.get("payment_intent", ""),
                    scheduled_time        = scheduled_time,
                )
                logger.info(
                    f"[BOOKING] Payment confirmed on success page | "
                    f"session={session_id} | email={email}"
                )

            # Read the updated order from DB to pass to the template
            order = db.get_order_by_session(cfg, session_id)

        except stripe.error.StripeError as e:
            logger.warning(f"[BOOKING] Could not retrieve session {session_id}: {e}")
            order = db.get_order_by_session(cfg, session_id)

    # FIX 3: Pass email to template so it can redirect dashboard with ?email=
    return render_template("order-success.html", order=order, client_email=email)


@bookings_bp.route("/order/cancel")
def order_cancel():
    return render_template("order-cancel.html")


# ── Create Order ──────────────────────────────────────────────────────────────

@bookings_bp.route("/api/create-order", methods=["POST"])
def create_order():
    if not cfg.STRIPE_SECRET_KEY:
        return jsonify({"error": "Stripe is not configured on this server."}), 503

    data = request.get_json(silent=True) or {}

    client_name  = str(data.get("client_name",  "")).strip()
    client_email = str(data.get("client_email", "")).strip().lower()
    service_type = str(data.get("service_type", "")).strip()
    requirements = str(data.get("requirements", "")).strip()
    budget       = str(data.get("budget",       "")).strip()

    errors = []
    if not client_name or len(client_name) < 2:
        errors.append("Full name is required (min 2 characters).")
    if not client_email or "@" not in client_email:
        errors.append("A valid email address is required.")
    if service_type not in SERVICE_TYPES:
        errors.append("Invalid service type.")
    if not requirements or len(requirements) < 20:
        errors.append("Please describe your requirements (min 20 characters).")
    if errors:
        return jsonify({"error": " | ".join(errors)}), 400

    price_id = cfg.STRIPE_SERVICE_PRICE_ID
    if not price_id:
        return jsonify({"error": "STRIPE_SERVICE_PRICE_ID is not configured."}), 503

    # Pre-generate room_id and meeting_link BEFORE Stripe session
    room_id      = _generate_room_id()
    meeting_link = _build_meeting_link(room_id)

    try:
        logger.info(f"[BOOKING] Creating Stripe session | price={price_id} | email={client_email}")
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="payment",
            customer_email=client_email,
            line_items=[{"price": price_id, "quantity": 1}],
            metadata={
                "client_name":  client_name,
                "client_email": client_email,
                "service_type": service_type,
                "budget":       budget,
                "order_type":   "service_booking",
                "room_id":      room_id,
                "meeting_link": meeting_link,
            },
            success_url=(
                f"{cfg.APP_BASE_URL}/order/success"
                f"?session_id={{CHECKOUT_SESSION_ID}}"
            ),
            cancel_url=f"{cfg.APP_BASE_URL}/order/cancel",
        )
        logger.info(f"[BOOKING] Stripe session created: {checkout_session.id}")

        # Store order in DB immediately with room_id and meeting_link
        order_id = db.create_order(
            cfg,
            client_name       = client_name,
            client_email      = client_email,
            service_type      = service_type,
            requirements      = requirements,
            budget            = budget or None,
            stripe_session_id = checkout_session.id,
            room_id           = room_id,
            meeting_link      = meeting_link,
        )

        logger.info(
            f"[BOOKING] Order {order_id} saved | "
            f"client={client_email} | room={room_id}"
        )
        return jsonify({"url": checkout_session.url})

    except stripe.error.StripeError as e:
        logger.error(f"[BOOKING] Stripe error: {type(e).__name__}: {e}")
        return jsonify({"error": f"Stripe error: {str(e)}"}), 500
    except Exception as e:
        logger.error(f"[BOOKING] Unexpected error: {type(e).__name__}: {e}")
        return jsonify({"error": f"Server error: {str(e)}"}), 500


# ── My Orders (Client Dashboard) ──────────────────────────────────────────────

@bookings_bp.route("/api/my-orders", methods=["GET"])
def my_orders():
    """
    Returns ALL orders for this email (pending + paid + failed).
    can_join is True only when payment_status == 'paid' AND meeting_link exists.
    """
    email = request.args.get("email", "").strip().lower()
    if not email or "@" not in email:
        return jsonify({"error": "Valid email is required."}), 400

    orders = db.get_orders_by_email(cfg, email)
    result = []
    for o in orders:
        paid     = o["payment_status"] == "paid"
        has_link = bool(o.get("meeting_link"))
        sched    = (
            o.get("scheduled_time_fmt") or
            str(o.get("scheduled_time", "")) or
            "To be confirmed"
        )
        result.append({
            "id":             o["id"],
            "service_type":   SERVICE_TYPES.get(o["service_type"], o["service_type"]),
            "payment_status": o["payment_status"],
            "meeting_link":   o.get("meeting_link") or "",
            "room_id":        o.get("room_id") or "",
            "scheduled_time": sched,
            "created_at":     o.get("created_at_fmt") or str(o.get("created_at", "")),
            "can_join":       paid and has_link,
            "requirements":   o.get("requirements", ""),
            "budget":         o.get("budget", ""),
        })
    return jsonify({"orders": result, "total": len(result)})


# ── All Orders (Super Admin) ──────────────────────────────────────────────────

@bookings_bp.route("/api/admin/orders", methods=["GET"])
def admin_all_orders():
    """Super Admin: view all orders. Requires Bearer token."""
    from admin import _get_admin_session
    session = _get_admin_session()
    if not session:
        return jsonify({"error": "Unauthorized"}), 401
    if session.get("role") != "super_admin":
        return jsonify({"error": "Super Admin access required"}), 403

    status_filter = request.args.get("status", "").strip() or None
    orders        = db.get_all_orders(cfg, status_filter=status_filter)

    result = []
    for o in orders:
        result.append({
            "id":             o["id"],
            "client_name":    o.get("client_name", ""),
            "client_email":   o.get("client_email", ""),
            "service_type":   SERVICE_TYPES.get(o.get("service_type", ""), o.get("service_type", "")),
            "payment_status": o["payment_status"],
            "room_id":        o.get("room_id") or "—",
            "meeting_link":   o.get("meeting_link") or "",
            "scheduled_time": str(o.get("scheduled_time", "")),
            "created_at":     str(o.get("created_at", "")),
        })
    return jsonify({"orders": result, "total": len(result)})


# ── Single Order ──────────────────────────────────────────────────────────────

@bookings_bp.route("/api/order/<int:order_id>", methods=["GET"])
def get_order(order_id):
    order = db.get_order_by_id(cfg, order_id)
    if not order:
        return jsonify({"error": "Order not found."}), 404
    paid     = order["payment_status"] == "paid"
    has_link = bool(order.get("meeting_link"))
    return jsonify({
        "id":             order["id"],
        "service_type":   SERVICE_TYPES.get(order["service_type"], order["service_type"]),
        "payment_status": order["payment_status"],
        "room_id":        order.get("room_id") or "",
        "meeting_link":   order.get("meeting_link") or "",
        "scheduled_time": str(order.get("scheduled_time", "")),
        "can_join":       paid and has_link,
    })


# ── Stripe Config Diagnostic ──────────────────────────────────────────────────

@bookings_bp.route("/api/test-stripe", methods=["GET"])
def test_stripe_config():
    issues      = []
    has_secret  = bool(cfg.STRIPE_SECRET_KEY)
    has_price   = bool(cfg.STRIPE_SERVICE_PRICE_ID)
    key_ok      = cfg.STRIPE_SECRET_KEY.startswith("sk_") if has_secret else False
    price_ok    = cfg.STRIPE_SERVICE_PRICE_ID.startswith("price_") if has_price else False

    if not has_secret:
        issues.append("STRIPE_SECRET_KEY is not set in .env")
    elif not key_ok:
        issues.append("STRIPE_SECRET_KEY should start with sk_test_ or sk_live_")
    if not has_price:
        issues.append("STRIPE_SERVICE_PRICE_ID is not set in .env")
    elif not price_ok:
        issues.append("STRIPE_SERVICE_PRICE_ID should start with price_")

    api_ok  = False
    api_msg = "Not tested"
    if has_secret and key_ok:
        try:
            stripe.Account.retrieve()
            api_ok  = True
            api_msg = "Stripe API connection successful"
        except stripe.error.AuthenticationError:
            issues.append("Stripe API key is invalid")
            api_msg = "Authentication failed"
        except Exception as e:
            api_msg = f"Error: {str(e)}"

    return jsonify({
        "status":  "ok" if not issues else "issues_found",
        "issues":  issues,
        "stripe": {
            "secret_key_set": has_secret,
            "price_id_set":   has_price,
            "price_id_value": cfg.STRIPE_SERVICE_PRICE_ID if has_price else None,
            "api_ok":         api_ok,
            "api_connection": api_msg,
        },
        "how_to_fix": {
            "price_id":   "dashboard.stripe.com → Products → your product → Pricing → copy price_xxx",
            "secret_key": "dashboard.stripe.com → Developers → API keys → Secret key (sk_test_...)",
        }
    })


# ── Webhook (production / Stripe CLI) ─────────────────────────────────────────

@bookings_bp.route("/api/order-webhook", methods=["POST"])
def order_webhook():
    """
    Stripe webhook — handles payment confirmation in production.
    In local testing, payment is confirmed by order_success() above.
    The confirm_order_payment() call is idempotent so running it twice is safe.
    """
    payload    = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    if not cfg.STRIPE_WEBHOOK_SECRET:
        event = stripe.Event.construct_from(request.get_json(force=True), stripe.api_key)
    else:
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, cfg.STRIPE_WEBHOOK_SECRET
            )
        except stripe.error.SignatureVerificationError as e:
            logger.warning(f"[BOOKING] Webhook signature invalid: {e}")
            return jsonify({"error": "Invalid signature"}), 400

    event_type = event["type"]
    data_obj   = event["data"]["object"]
    metadata   = data_obj.get("metadata", {})

    if metadata.get("order_type") != "service_booking":
        return jsonify({"status": "ignored"})

    if event_type == "checkout.session.completed":
        if data_obj.get("payment_status") != "paid":
            return jsonify({"status": "ok", "reason": "not yet paid"})

        session_id     = data_obj.get("id", "")
        payment_intent = data_obj.get("payment_intent", "")
        scheduled_time = _calculate_scheduled_time()

        success = db.confirm_order_payment(
            cfg,
            stripe_session_id     = session_id,
            stripe_transaction_id = payment_intent,
            scheduled_time        = scheduled_time,
        )
        room_id = metadata.get("room_id", "unknown")
        if success:
            logger.info(
                f"[BOOKING] Webhook confirmed payment | "
                f"session={session_id} | room={room_id}"
            )

    return jsonify({"status": "ok"})