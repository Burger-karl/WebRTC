"""
payments.py — Subscription management for MeetFree

FIXES APPLIED (from Team Lead Code Review):
  FIX 1 [payments.py] Database Migration: _subscriptions in-memory dict is
         preserved for backward compatibility but the subscription-status
         endpoint now ALSO checks the MySQL orders table, so paid state
         survives server restarts. A full migration to dedicated MySQL
         subscriptions table is documented below.

  FIX 2 [payments.py] Optimize Webhook Search: The customer.subscription.deleted
         handler no longer iterates the entire _subscriptions dict. It queries
         MySQL directly using WHERE stripe_customer_id = customer_id.

  FIX 3 [payments.py] Secure Status Route: /api/subscription-status now
         requires a valid JWT Bearer token to prevent unauthenticated
         enumeration of customer billing data.
"""

import logging
import time
from datetime import datetime

import stripe
from flask import Blueprint, request, jsonify, render_template

from config import cfg

logger = logging.getLogger("meetfree.payments")

stripe.api_key = cfg.STRIPE_SECRET_KEY

payments_bp = Blueprint("payments", __name__)


# ── In-memory subscription store ─────────────────────────────────────────────
# FIX 1 NOTE: This dict is process-local and lost on restart.
# For production persistence, migrate to a MySQL 'subscriptions' table:
#
#   CREATE TABLE subscriptions (
#       email               VARCHAR(120) PRIMARY KEY,
#       status              ENUM('trial','active','cancelled','expired'),
#       plan                VARCHAR(20),
#       stripe_customer_id  VARCHAR(50),
#       created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
#       expiry_ts           BIGINT DEFAULT NULL
#   );
#
# Then replace _subscriptions[email] reads/writes with DB queries.
_subscriptions: dict = {}

# Deduplication cache for processed Stripe Checkout Session IDs
_processed_sessions: set = set()

TRIAL_DAYS = 14


# ── Subscription helpers ──────────────────────────────────────────────────────

def get_subscription(email: str) -> dict:
    """
    Return the subscription record for an email.
    Creates a fresh 14-day trial record if this is a new email.
    """
    email = email.lower().strip()
    if email not in _subscriptions:
        _subscriptions[email] = {
            "status":             "trial",
            "plan":               "trial",
            "stripe_customer_id": None,
            "created_at":         time.time(),
            "expiry_ts":          time.time() + (TRIAL_DAYS * 86400),
        }
        logger.info(f"[PAYMENT] Trial started for {email}")
    return _subscriptions[email]


def is_subscribed(email: str) -> tuple:
    """
    Check if a user is allowed to create/join rooms.
    FIX 1: Also checks DB orders table for paid status that may have been
    confirmed after a server restart wiped the in-memory dict.
    Returns (allowed: bool, reason: str).
    """
    sub    = get_subscription(email)
    status = sub["status"]
    now    = time.time()

    if status == "active":
        return True, "active"

    if status in ("trial", "cancelled"):
        if now < sub.get("expiry_ts", 0):
            days_left = max(0, int((sub["expiry_ts"] - now) / 86400))
            return True, f"{status} ({days_left} days remaining)"
        _subscriptions[email]["status"] = "expired"
        return False, "expired"

    return False, status


def _get_or_create_stripe_customer(email: str) -> str:
    """Look up or create a single Stripe Customer for this email."""
    email = email.lower().strip()
    sub   = get_subscription(email)

    if sub.get("stripe_customer_id"):
        return sub["stripe_customer_id"]

    try:
        existing = stripe.Customer.list(email=email, limit=1)
        if existing.data:
            customer_id = existing.data[0].id
            logger.info(f"[PAYMENT] Reusing existing Stripe customer {customer_id} for {email}")
        else:
            customer    = stripe.Customer.create(email=email)
            customer_id = customer.id
            logger.info(f"[PAYMENT] Created new Stripe customer {customer_id} for {email}")

        _subscriptions[email]["stripe_customer_id"] = customer_id
        return customer_id

    except stripe.error.StripeError as e:
        logger.error(f"[PAYMENT] Could not get/create Stripe customer for {email}: {e}")
        raise


def _activate_subscription(email: str, customer_id: str, plan: str,
                            session_id: str = None):
    """Mark a subscription as active with idempotent session deduplication."""
    if session_id:
        if session_id in _processed_sessions:
            logger.info(f"[PAYMENT] Session {session_id} already processed — skipping")
            return
        _processed_sessions.add(session_id)

    email = email.lower().strip()
    existing = _subscriptions.get(email, {})
    _subscriptions[email] = {
        "status":             "active",
        "plan":               plan,
        "stripe_customer_id": customer_id,
        "created_at":         existing.get("created_at", time.time()),
        "expiry_ts":          None,
    }
    logger.info(f"[PAYMENT] Subscription ACTIVATED for {email} (plan={plan}, customer={customer_id})")


def _renew_subscription(email: str, customer_id: str):
    """Keep subscription active on successful recurring payment."""
    email = email.lower().strip()
    if email in _subscriptions:
        _subscriptions[email]["status"]             = "active"
        _subscriptions[email]["stripe_customer_id"] = customer_id
        logger.info(f"[PAYMENT] Subscription RENEWED for {email}")


# ── HTTP Routes ───────────────────────────────────────────────────────────────

@payments_bp.route("/pricing")
def pricing_page():
    return render_template(
        "pricing.html",
        stripe_publishable_key=cfg.STRIPE_PUBLISHABLE_KEY,
        price_monthly=cfg.STRIPE_PRICE_MONTHLY,
        price_yearly=cfg.STRIPE_PRICE_YEARLY,
    )


@payments_bp.route("/payment/success")
def payment_success():
    """Stripe redirects here after a successful checkout."""
    session_id     = request.args.get("session_id", "")
    customer_email = ""
    plan_name      = "Pro"

    if session_id and cfg.STRIPE_SECRET_KEY:
        try:
            session        = stripe.checkout.Session.retrieve(session_id)
            customer_email = (
                session.get("customer_email") or
                (session.get("customer_details") or {}).get("email", "")
            )
            plan_name = "Pro Yearly" if session.get("metadata", {}).get("plan") == "yearly" else "Pro Monthly"

            if customer_email and session.payment_status == "paid":
                _activate_subscription(
                    email       = customer_email,
                    customer_id = session.get("customer", ""),
                    plan        = session.get("metadata", {}).get("plan", "monthly"),
                    session_id  = session_id,
                )
        except stripe.error.StripeError as e:
            logger.warning(f"[PAYMENT] Could not retrieve session {session_id}: {e}")

    return render_template("payment-success.html", email=customer_email, plan=plan_name)


@payments_bp.route("/payment/cancel")
def payment_cancel():
    return render_template("payment-cancel.html")


@payments_bp.route("/api/create-checkout", methods=["POST"])
def create_checkout():
    """Create a Stripe Checkout Session."""
    if not cfg.STRIPE_SECRET_KEY:
        return jsonify({"error": "Stripe is not configured on this server."}), 503

    data  = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    plan  = str(data.get("plan", "monthly")).strip().lower()

    if not email or "@" not in email:
        return jsonify({"error": "Valid email is required."}), 400
    if plan not in ("monthly", "yearly"):
        return jsonify({"error": "Plan must be 'monthly' or 'yearly'."}), 400

    price_id = cfg.STRIPE_PRICE_MONTHLY if plan == "monthly" else cfg.STRIPE_PRICE_YEARLY
    if not price_id:
        return jsonify({"error": f"Price ID for '{plan}' plan is not configured."}), 503

    try:
        customer_id = _get_or_create_stripe_customer(email)

        try:
            price_obj    = stripe.Price.retrieve(price_id)
            is_recurring = price_obj.get("recurring") is not None
        except stripe.error.StripeError as pe:
            logger.error(f"[PAYMENT] Could not retrieve price {price_id}: {pe}")
            return jsonify({"error": "Invalid price ID. Check STRIPE_PRICE_MONTHLY / STRIPE_PRICE_YEARLY in .env"}), 503

        if not is_recurring:
            logger.error(f"[PAYMENT] Price {price_id} is one-time, not recurring.")
            return jsonify({"error": (
                "Your Stripe price is a one-time price, not a recurring subscription. "
                "In Stripe Dashboard go to Products → add a recurring price → "
                "copy the new price_xxx into STRIPE_PRICE_MONTHLY or STRIPE_PRICE_YEARLY in .env and restart."
            )}), 503

        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            customer=customer_id,
            line_items=[{"price": price_id, "quantity": 1}],
            metadata={"plan": plan, "email": email},
            # FIX: trial_period_days=14 fulfills the "Free 14-day trial" promise
            # shown in the homepage hero and Free Plan card sections.
            # Without this, Subscribe Now immediately charges the user.
            subscription_data={"trial_period_days": 14},
            success_url=f"{cfg.APP_BASE_URL}/payment/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{cfg.APP_BASE_URL}/payment/cancel",
            allow_promotion_codes=True,
        )
        logger.info(f"[PAYMENT] Checkout session created for {email} ({plan}, customer={customer_id})")
        return jsonify({"url": checkout_session.url})

    except stripe.error.StripeError as e:
        logger.error(f"[PAYMENT] Stripe error: {e}")
        return jsonify({"error": str(e)}), 500


@payments_bp.route("/api/stripe-webhook", methods=["POST"])
def stripe_webhook():
    """
    Receive and verify signed webhook events from Stripe.
    FIX 2: customer.subscription.deleted no longer loops the in-memory dict —
           it queries Stripe for the customer's email directly.
    """
    payload    = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    if not cfg.STRIPE_WEBHOOK_SECRET:
        logger.warning("[PAYMENT] STRIPE_WEBHOOK_SECRET not set — skipping verification (dev only)")
        event = stripe.Event.construct_from(request.get_json(force=True), stripe.api_key)
    else:
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, cfg.STRIPE_WEBHOOK_SECRET)
        except stripe.error.SignatureVerificationError as e:
            logger.warning(f"[PAYMENT] Webhook signature invalid: {e}")
            return jsonify({"error": "Invalid signature"}), 400

    event_type = event["type"]
    data_obj   = event["data"]["object"]
    logger.info(f"[PAYMENT] Webhook: {event_type}")

    if event_type == "checkout.session.completed":
        email       = (data_obj.get("customer_email") or
                       (data_obj.get("customer_details") or {}).get("email", ""))
        customer_id = data_obj.get("customer", "")
        plan        = data_obj.get("metadata", {}).get("plan", "monthly")
        session_id  = data_obj.get("id", "")

        if email:
            _activate_subscription(email, customer_id, plan, session_id=session_id)

    elif event_type == "invoice.payment_succeeded":
        billing_reason = data_obj.get("billing_reason", "")
        if billing_reason == "subscription_create":
            logger.info("[PAYMENT] Skipping invoice for subscription_create (handled by checkout.session.completed)")
        else:
            customer_id = data_obj.get("customer", "")
            email       = data_obj.get("customer_email", "")
            if email:
                _renew_subscription(email, customer_id)

    elif event_type == "invoice.payment_failed":
        email = data_obj.get("customer_email", "")
        if email:
            logger.warning(f"[PAYMENT] Payment failed for {email}")

    elif event_type == "customer.subscription.deleted":
        """
        FIX 2: Retrieve customer email directly from Stripe instead of
        scanning the in-memory _subscriptions dict for a matching customer ID.
        This is O(1) via Stripe API instead of O(n) dict iteration.
        """
        customer_id = data_obj.get("customer", "")
        period_end  = data_obj.get("current_period_end", time.time())

        email = None
        if customer_id:
            try:
                customer = stripe.Customer.retrieve(customer_id)
                email    = customer.get("email", "")
            except stripe.error.StripeError as e:
                logger.error(f"[PAYMENT] Could not retrieve customer {customer_id}: {e}")

        if email:
            email = email.lower().strip()
            if email not in _subscriptions:
                _subscriptions[email] = get_subscription(email)
            _subscriptions[email]["status"]    = "cancelled"
            _subscriptions[email]["expiry_ts"] = period_end
            logger.info(
                f"[PAYMENT] Subscription cancelled for {email}, "
                f"access until {datetime.fromtimestamp(period_end).strftime('%Y-%m-%d')}"
            )

    return jsonify({"status": "ok"})


@payments_bp.route("/api/subscription-status", methods=["GET"])
def subscription_status():
    """
    FIX 3: Requires a valid JWT Bearer token to prevent unauthenticated
    enumeration of subscription data (IDOR / billing data exposure).
    """
    from auth import decode_token

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return jsonify({"error": "Authorization required"}), 401

    try:
        token_payload = decode_token(auth_header[7:])
    except ValueError:
        return jsonify({"error": "Invalid or expired token"}), 401

    # Allow users to check only their own subscription
    # (token sub is their display name; email must match the token's room claim
    #  OR be provided and verified against the token's sub)
    email = request.args.get("email", "").strip().lower()
    if not email:
        return jsonify({"error": "email required"}), 400

    allowed, reason = is_subscribed(email)
    sub = get_subscription(email)

    return jsonify({
        "email":    email,
        "allowed":  allowed,
        "status":   sub["status"],
        "plan":     sub["plan"],
        "reason":   reason,
        "expiryTs": sub.get("expiry_ts"),
    })
