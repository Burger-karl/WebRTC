"""
bookings.py — Order Booking & Automated Meeting Scheduling

Fix 3 applied here: room_id is now generated BEFORE the Stripe Checkout
Session is created. It is:
  1. Embedded in Stripe metadata (room_id, order_id fields)
  2. Stored in MySQL immediately via create_order()
  3. Used by the webhook to confirm payment — no new room_id generated post-payment

This guarantees the "Join Meeting" button link in the client dashboard always
matches the room_id stored in Stripe and MySQL.
"""

import logging
import uuid
import time
from datetime import datetime, timedelta

import stripe
from flask import Blueprint, request, jsonify, render_template

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


# ── Routes ────────────────────────────────────────────────────────────────────

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
    session_id = request.args.get("session_id", "")
    order = db.get_order_by_session(cfg, session_id) if session_id else None
    return render_template("order-success.html", order=order)


@bookings_bp.route("/order/cancel")
def order_cancel():
    return render_template("order-cancel.html")


@bookings_bp.route("/api/create-order", methods=["POST"])
def create_order():
    """
    FIX 3: Pre-generate room_id and meeting_link BEFORE creating the
    Stripe session so they are embedded in Stripe metadata and stored
    in MySQL atomically. The webhook receives them directly from metadata
    and only needs to update payment_status and scheduled_time.
    """
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
        errors.append(f"Invalid service type.")
    if not requirements or len(requirements) < 20:
        errors.append("Please describe your requirements (min 20 characters).")
    if errors:
        return jsonify({"error": " | ".join(errors)}), 400

    price_id = cfg.STRIPE_SERVICE_PRICE_ID
    if not price_id:
        return jsonify({"error": "STRIPE_SERVICE_PRICE_ID is not configured."}), 503

    # FIX 3: Generate room_id NOW — before anything else
    room_id      = _generate_room_id()
    meeting_link = _build_meeting_link(room_id)

    try:
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
                # FIX 3: embed pre-generated room_id in Stripe metadata
                "room_id":      room_id,
                "meeting_link": meeting_link,
            },
            success_url=f"{cfg.APP_BASE_URL}/order/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{cfg.APP_BASE_URL}/order/cancel",
        )

        # FIX 3: Store room_id and meeting_link in DB immediately
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
            f"[BOOKING] Order {order_id} created | "
            f"client={client_email} | room={room_id} | session={checkout_session.id}"
        )
        return jsonify({"url": checkout_session.url})

    except stripe.error.StripeError as e:
        logger.error(f"[BOOKING] Stripe error: {e}")
        return jsonify({"error": str(e)}), 500


@bookings_bp.route("/api/my-orders", methods=["GET"])
def my_orders():
    email = request.args.get("email", "").strip().lower()
    if not email or "@" not in email:
        return jsonify({"error": "Valid email is required."}), 400

    orders = db.get_orders_by_email(cfg, email)
    result = []
    for o in orders:
        result.append({
            "id":             o["id"],
            "service_type":   SERVICE_TYPES.get(o["service_type"], o["service_type"]),
            "payment_status": o["payment_status"],
            "meeting_link":   o.get("meeting_link"),
            "room_id":        o.get("room_id"),
            "scheduled_time": o.get("scheduled_time_fmt") or str(o.get("scheduled_time", "")),
            "created_at":     o.get("created_at_fmt") or str(o.get("created_at", "")),
            "can_join":       o["payment_status"] == "paid" and bool(o.get("meeting_link")),
        })
    return jsonify({"orders": result, "total": len(result)})


@bookings_bp.route("/api/order/<int:order_id>", methods=["GET"])
def get_order(order_id):
    order = db.get_order_by_id(cfg, order_id)
    if not order:
        return jsonify({"error": "Order not found."}), 404
    return jsonify({
        "id":             order["id"],
        "service_type":   SERVICE_TYPES.get(order["service_type"], order["service_type"]),
        "payment_status": order["payment_status"],
        "room_id":        order.get("room_id"),
        "meeting_link":   order.get("meeting_link"),
        "scheduled_time": str(order.get("scheduled_time", "")),
        "can_join":       order["payment_status"] == "paid" and bool(order.get("meeting_link")),
    })


@bookings_bp.route("/api/order-webhook", methods=["POST"])
def order_webhook():
    """
    FIX 3: Webhook no longer generates room_id — it reads it from
    Stripe metadata (where we stored it before checkout was created).
    Only updates payment_status and scheduled_time.
    """
    payload    = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    if not cfg.STRIPE_WEBHOOK_SECRET:
        event = stripe.Event.construct_from(request.get_json(force=True), stripe.api_key)
    else:
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, cfg.STRIPE_WEBHOOK_SECRET)
        except stripe.error.SignatureVerificationError as e:
            logger.warning(f"[BOOKING] Webhook signature invalid: {e}")
            return jsonify({"error": "Invalid signature"}), 400

    event_type = event["type"]
    data_obj   = event["data"]["object"]
    metadata   = data_obj.get("metadata", {})

    if metadata.get("order_type") != "service_booking":
        return jsonify({"status": "ignored"})

    if event_type == "checkout.session.completed":
        session_id     = data_obj.get("id", "")
        payment_intent = data_obj.get("payment_intent", "")

        if data_obj.get("payment_status") != "paid":
            return jsonify({"status": "ok", "reason": "not yet paid"})

        scheduled_time = _calculate_scheduled_time()

        success = db.confirm_order_payment(
            cfg,
            stripe_session_id     = session_id,
            stripe_transaction_id = payment_intent,
            scheduled_time        = scheduled_time,
        )

        if success:
            # Read back the room_id from metadata for the log
            room_id = metadata.get("room_id", "unknown")
            logger.info(
                f"[BOOKING] Payment confirmed | session={session_id} | "
                f"room={room_id} | scheduled={scheduled_time.strftime('%Y-%m-%d %H:%M UTC')}"
            )

    return jsonify({"status": "ok"})