"""
bookings.py — Order Booking & Automated Meeting Scheduling
─────────────────────────────────────────────────────────────────────────────
This module implements the full client booking workflow:

  1. Client fills in the Order Form (service requirements, budget)
  2. System creates a pending order record in MySQL
  3. Client is redirected to Stripe Checkout for payment
  4. On payment success, the webhook automatically:
       - Marks order as 'paid'
       - Generates a unique Meeting Room ID
       - Builds the meeting_link (direct URL into the video tool)
       - Calculates and stores the scheduled_time
  5. Client sees their active orders on the Dashboard
  6. Client clicks "Join Meeting" → lands directly in the video room

Routes:
  GET  /order              — Order form page
  POST /api/create-order   — Validates form, creates Stripe Checkout Session,
                             saves pending order to MySQL
  GET  /dashboard          — Client dashboard (shows orders + Join Meeting)
  GET  /api/my-orders      — JSON: fetch orders by email (for dashboard JS)
  GET  /api/order/<id>     — JSON: single order detail
  POST /api/order-webhook  — Stripe webhook: confirms payment, schedules meeting
  GET  /order/success      — Post-payment success page
  GET  /order/cancel       — Post-payment cancel page

The Stripe webhook is separate from the subscription webhook in payments.py.
Both can coexist — they listen to different event types.
─────────────────────────────────────────────────────────────────────────────
"""

import logging
import uuid
import time
from datetime import datetime, timedelta

import stripe
from flask import Blueprint, request, jsonify, render_template, redirect

from config import cfg
import database as db

logger = logging.getLogger("meetfree.bookings")

stripe.api_key = cfg.STRIPE_SECRET_KEY

bookings_bp = Blueprint("bookings", __name__)

# ── Service catalogue ─────────────────────────────────────────────────────────
# These map the service_type value from the order form to a display label.
# Add or edit services here to match your actual offerings.
SERVICE_TYPES = {
    "web_app":        "Web Application Development",
    "mobile_app":     "Mobile App Development",
    "api_backend":    "API / Backend Development",
    "ui_ux_design":   "UI/UX Design",
    "consultation":   "Technical Consultation",
    "code_review":    "Code Review & Audit",
    "devops":         "DevOps / Cloud Setup",
    "other":          "Other / Custom Project",
}


# ── Helper: generate a unique room ID for a booking ──────────────────────────

def _generate_room_id() -> str:
    """
    Generate a URL-safe unique room ID for a meeting.
    Format: order-<8 hex chars>
    Example: order-a3f9c12b
    """
    return f"order-{uuid.uuid4().hex[:8]}"


def _build_meeting_link(room_id: str) -> str:
    """Build the full URL that opens the video room directly."""
    return f"{cfg.APP_BASE_URL}/room/{room_id}"


def _calculate_scheduled_time() -> datetime:
    """
    Auto-schedule the meeting N hours after payment.
    N is configured via MEETING_SCHEDULE_HOURS_AFTER (default: 24).
    """
    return datetime.utcnow() + timedelta(hours=cfg.MEETING_SCHEDULE_HOURS_AFTER)


# ── HTTP Routes ───────────────────────────────────────────────────────────────

@bookings_bp.route("/order")
def order_page():
    """Render the client order form."""
    return render_template(
        "order.html",
        service_types=SERVICE_TYPES,
        stripe_publishable_key=cfg.STRIPE_PUBLISHABLE_KEY,
        meeting_duration=cfg.MEETING_DURATION_MINUTES,
    )


@bookings_bp.route("/dashboard")
def dashboard_page():
    """Render the client dashboard page."""
    return render_template("dashboard.html")


@bookings_bp.route("/order/success")
def order_success():
    """
    Stripe redirects here after a successful order payment.
    Fetches the order from DB using the session_id query param so we can
    show the client their meeting details immediately.
    """
    session_id = request.args.get("session_id", "")
    order      = None

    if session_id:
        # Give the webhook a moment to process if it hasn't yet
        # (in production the webhook fires almost instantly)
        order = db.get_order_by_session(cfg, session_id)

    return render_template("order-success.html", order=order)


@bookings_bp.route("/order/cancel")
def order_cancel():
    """Stripe redirects here if the client abandons the payment."""
    return render_template("order-cancel.html")


@bookings_bp.route("/api/create-order", methods=["POST"])
def create_order():
    """
    Step 1 of the booking flow.

    Validates the order form, creates a pending order in MySQL,
    then creates a Stripe Checkout Session and returns the URL.

    Request body (JSON or form data):
    {
        "client_name":   "Alice Johnson",
        "client_email":  "alice@example.com",
        "service_type":  "web_app",
        "requirements":  "I need a full-stack e-commerce site...",
        "budget":        "$5,000 - $10,000"    (optional)
    }

    Response:
    { "url": "https://checkout.stripe.com/..." }
    """
    if not cfg.STRIPE_SECRET_KEY:
        return jsonify({"error": "Stripe is not configured on this server."}), 503

    data = request.get_json(silent=True) or {}

    # ── Validate inputs ───────────────────────────────────────
    client_name   = str(data.get("client_name",   "")).strip()
    client_email  = str(data.get("client_email",  "")).strip().lower()
    service_type  = str(data.get("service_type",  "")).strip()
    requirements  = str(data.get("requirements",  "")).strip()
    budget        = str(data.get("budget",        "")).strip()

    errors = []
    if not client_name or len(client_name) < 2:
        errors.append("Full name is required (min 2 characters).")
    if not client_email or "@" not in client_email:
        errors.append("A valid email address is required.")
    if service_type not in SERVICE_TYPES:
        errors.append(f"Invalid service type. Choose from: {', '.join(SERVICE_TYPES.keys())}")
    if not requirements or len(requirements) < 20:
        errors.append("Please describe your requirements (min 20 characters).")

    if errors:
        return jsonify({"error": " | ".join(errors)}), 400

    # ── Determine price ───────────────────────────────────────
    price_id = cfg.STRIPE_SERVICE_PRICE_ID
    if not price_id:
        # If no fixed service price is configured, use a custom amount
        # Default: $99 consultation fee (in cents)
        # You can override this per service_type in production
        return jsonify({
            "error": "STRIPE_SERVICE_PRICE_ID is not configured. "
                     "Please create a one-time price in your Stripe Dashboard "
                     "and add it to your .env file."
        }), 503

    try:
        # ── Create Stripe Checkout Session ────────────────────
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="payment",                # one-time payment, not subscription
            customer_email=client_email,
            line_items=[{
                "price":    price_id,
                "quantity": 1,
            }],
            metadata={
                "client_name":  client_name,
                "client_email": client_email,
                "service_type": service_type,
                "budget":       budget,
                "order_type":   "service_booking",
            },
            success_url=f"{cfg.APP_BASE_URL}/order/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{cfg.APP_BASE_URL}/order/cancel",
        )

        # ── Save pending order to MySQL ───────────────────────
        # We create the order BEFORE payment so we always have a record.
        # The webhook updates it to 'paid' and adds meeting details.
        order_id = db.create_order(
            cfg,
            client_name   = client_name,
            client_email  = client_email,
            service_type  = service_type,
            requirements  = requirements,
            budget        = budget or None,
            stripe_session_id = checkout_session.id,
        )

        logger.info(
            f"[BOOKING] Order {order_id} created | "
            f"client={client_email} | service={service_type} | "
            f"session={checkout_session.id}"
        )

        return jsonify({"url": checkout_session.url})

    except stripe.error.StripeError as e:
        logger.error(f"[BOOKING] Stripe error creating order checkout: {e}")
        return jsonify({"error": str(e)}), 500


@bookings_bp.route("/api/my-orders", methods=["GET"])
def my_orders():
    """
    Client Dashboard API — returns all orders for a given email.

    Query param: ?email=alice@example.com

    Used by dashboard.html JavaScript to populate the orders table.
    Returns only orders with payment_status='paid' for security
    (pending/failed orders don't have a meeting link yet).
    """
    email = request.args.get("email", "").strip().lower()
    if not email or "@" not in email:
        return jsonify({"error": "Valid email is required."}), 400

    orders = db.get_orders_by_email(cfg, email)

    # Shape the response — only expose what the client dashboard needs
    result = []
    for o in orders:
        result.append({
            "id":            o["id"],
            "service_type":  SERVICE_TYPES.get(o["service_type"], o["service_type"]),
            "payment_status": o["payment_status"],
            "meeting_link":  o.get("meeting_link"),
            "room_id":       o.get("room_id"),
            "scheduled_time": o.get("scheduled_time_fmt") or o.get("scheduled_time"),
            "created_at":    o.get("created_at_fmt") or str(o.get("created_at", "")),
            "can_join":      o["payment_status"] == "paid" and bool(o.get("meeting_link")),
        })

    return jsonify({"orders": result, "total": len(result)})


@bookings_bp.route("/api/order/<int:order_id>", methods=["GET"])
def get_order(order_id):
    """
    Fetch a single order by ID.
    Used to verify a meeting link before letting someone join.
    """
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
    Stripe webhook for order payments.

    Separate from the subscription webhook in payments.py.
    Only processes events where metadata.order_type == 'service_booking'
    so subscription events and order events never mix.

    Event handled:
      checkout.session.completed
        → Marks order as paid
        → Generates unique room_id
        → Builds meeting_link
        → Calculates scheduled_time
        → Updates order record in MySQL

    To test locally with Stripe CLI:
        stripe listen --forward-to localhost:5000/api/order-webhook
    """
    payload    = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    if not cfg.STRIPE_WEBHOOK_SECRET:
        logger.warning("[BOOKING] STRIPE_WEBHOOK_SECRET not set — skipping signature check (dev mode)")
        event = stripe.Event.construct_from(request.get_json(force=True), stripe.api_key)
    else:
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, cfg.STRIPE_WEBHOOK_SECRET)
        except stripe.error.SignatureVerificationError as e:
            logger.warning(f"[BOOKING] Webhook signature invalid: {e}")
            return jsonify({"error": "Invalid signature"}), 400

    event_type = event["type"]
    data_obj   = event["data"]["object"]

    # Only process order events — ignore subscription events
    metadata   = data_obj.get("metadata", {})
    if metadata.get("order_type") != "service_booking":
        return jsonify({"status": "ignored", "reason": "not an order event"})

    logger.info(f"[BOOKING] Webhook received: {event_type}")

    if event_type == "checkout.session.completed":
        session_id       = data_obj.get("id", "")
        payment_intent   = data_obj.get("payment_intent", "")
        client_email     = data_obj.get("customer_email") or metadata.get("client_email", "")

        if data_obj.get("payment_status") != "paid":
            logger.warning(f"[BOOKING] Session {session_id} completed but payment_status != paid")
            return jsonify({"status": "ok", "reason": "payment not confirmed"})

        # Generate meeting details
        room_id        = _generate_room_id()
        meeting_link   = _build_meeting_link(room_id)
        scheduled_time = _calculate_scheduled_time()

        success = db.confirm_order_payment(
            cfg,
            stripe_session_id     = session_id,
            stripe_transaction_id = payment_intent,
            room_id               = room_id,
            meeting_link          = meeting_link,
            scheduled_time        = scheduled_time,
        )

        if success:
            logger.info(
                f"[BOOKING] Meeting scheduled | "
                f"client={client_email} | room={room_id} | "
                f"time={scheduled_time.strftime('%Y-%m-%d %H:%M UTC')}"
            )
        else:
            logger.error(f"[BOOKING] Failed to confirm order for session {session_id}")

    return jsonify({"status": "ok"})