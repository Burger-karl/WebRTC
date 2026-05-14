import logging
import time
from datetime import datetime

import stripe
from flask import Blueprint, request, jsonify, render_template

from config import cfg

logger = logging.getLogger("meetfree.payments")

stripe.api_key = cfg.STRIPE_SECRET_KEY

payments_bp = Blueprint("payments", __name__)


# ── Subscription store ────────────────────────────────────────────────────────
# { email: { status, plan, stripe_customer_id, created_at, expiry_ts } }
#
# status: "trial" | "active" | "cancelled" | "expired"
#
# Replace with a real database in production.
_subscriptions: dict = {}

# Deduplication cache for processed Stripe Checkout Session IDs.
# Prevents double-activation when both the success-page redirect AND the
# checkout.session.completed webhook call _activate_subscription for the
# same payment.
# { session_id: True }
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
        # Mark as expired
        _subscriptions[email]["status"] = "expired"
        return False, "expired"

    return False, status


def _get_or_create_stripe_customer(email: str) -> str:
    """
    FIX 1 — Duplicate customer prevention.

    Look up whether a Stripe Customer already exists for this email.
    If yes, return the existing customer ID.
    If no, create one and store the ID locally.

    This replaces passing customer_email= to checkout sessions (which always
    creates a new Customer object) with passing customer=<id> (which reuses
    the existing one).
    """
    email = email.lower().strip()
    sub   = get_subscription(email)

    # If we already have a Stripe customer ID stored, use it
    if sub.get("stripe_customer_id"):
        return sub["stripe_customer_id"]

    # Search Stripe for an existing customer with this email
    try:
        existing = stripe.Customer.list(email=email, limit=1)
        if existing.data:
            customer_id = existing.data[0].id
            logger.info(f"[PAYMENT] Reusing existing Stripe customer {customer_id} for {email}")
        else:
            # Create a new Stripe customer
            customer    = stripe.Customer.create(email=email)
            customer_id = customer.id
            logger.info(f"[PAYMENT] Created new Stripe customer {customer_id} for {email}")

        # Store the customer ID locally so we don't need to search again
        _subscriptions[email]["stripe_customer_id"] = customer_id
        return customer_id

    except stripe.error.StripeError as e:
        logger.error(f"[PAYMENT] Could not get/create Stripe customer for {email}: {e}")
        raise


def _activate_subscription(email: str, customer_id: str, plan: str,
                            session_id: str = None):
    """
    Mark a subscription as active.

    FIX 2 — Idempotent activation with session deduplication.
    If session_id is provided and has already been processed, this call
    is a no-op. This prevents double-activation when both the success-page
    redirect and the webhook fire for the same checkout session.
    """
    if session_id:
        if session_id in _processed_sessions:
            logger.info(f"[PAYMENT] Session {session_id} already processed — skipping duplicate activation")
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
    """
    Keep subscription active on successful recurring payment.
    """
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
    """
    Stripe redirects here after a successful checkout.
    We retrieve the session server-side to confirm payment status and
    activate the subscription — but only if the webhook hasn't already done it.
    """
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
                # Pass session_id so _activate_subscription can deduplicate
                # against the webhook that will also fire for this session.
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
    """
    Create a Stripe Checkout Session.

    FIX 1 applied here: we look up or create a single Stripe Customer for
    this email before creating the session, then pass customer=<id> instead
    of customer_email=. This ensures Stripe never creates duplicate Customer
    objects for the same email address.

    Request body: { "email": "alice@example.com", "plan": "monthly"|"yearly" }
    Response:     { "url": "https://checkout.stripe.com/..." }
    """
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
        # Get or create a single Stripe Customer for this email
        customer_id = _get_or_create_stripe_customer(email)

        # Validate price is RECURRING before using subscription mode.
        # A one-time price in subscription mode causes Stripe 400 error.
        try:
            price_obj   = stripe.Price.retrieve(price_id)
            is_recurring = price_obj.get("recurring") is not None
        except stripe.error.StripeError as pe:
            logger.error(f"[PAYMENT] Could not retrieve price {price_id}: {pe}")
            return jsonify({"error": f"Invalid price ID. Check STRIPE_PRICE_MONTHLY / STRIPE_PRICE_YEARLY in .env"}), 503

        if not is_recurring:
            logger.error(f"[PAYMENT] Price {price_id} is one-time, not recurring. Subscription mode requires recurring price.")
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

    Events handled:
      checkout.session.completed  — payment succeeded, activate subscription
      invoice.payment_succeeded   — recurring renewal succeeded
      invoice.payment_failed      — payment failed
      customer.subscription.deleted — subscription cancelled

    FIX 2: checkout.session.completed passes session_id to _activate_subscription
            so it deduplicates against the success-page call.

    FIX 3: invoice.payment_succeeded skips billing_reason == "subscription_create"
            because that invoice is the first one fired right after checkout
            completion, which is already handled by checkout.session.completed.
            Processing it again would cause a redundant second write.
    """
    payload    = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    if not cfg.STRIPE_WEBHOOK_SECRET:
        logger.warning("[PAYMENT] STRIPE_WEBHOOK_SECRET not set — skipping signature verification (dev only)")
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

    # ── checkout.session.completed ─────────────────────────────────────────
    if event_type == "checkout.session.completed":
        email       = (data_obj.get("customer_email") or
                       (data_obj.get("customer_details") or {}).get("email", ""))
        customer_id = data_obj.get("customer", "")
        plan        = data_obj.get("metadata", {}).get("plan", "monthly")
        session_id  = data_obj.get("id", "")

        if email:
            # session_id deduplicates against the success-page call (FIX 2)
            _activate_subscription(email, customer_id, plan, session_id=session_id)

    # ── invoice.payment_succeeded ──────────────────────────────────────────
    elif event_type == "invoice.payment_succeeded":
        # FIX 3: skip the very first invoice on a new subscription.
        # billing_reason == "subscription_create" means this is the initial
        # invoice fired right alongside checkout.session.completed.
        # We already handled that event above — processing this one too would
        # cause a redundant duplicate write.
        billing_reason = data_obj.get("billing_reason", "")
        if billing_reason == "subscription_create":
            logger.info("[PAYMENT] Skipping invoice.payment_succeeded with billing_reason=subscription_create (already handled by checkout.session.completed)")
        else:
            customer_id = data_obj.get("customer", "")
            email       = data_obj.get("customer_email", "")
            if email:
                _renew_subscription(email, customer_id)

    # ── invoice.payment_failed ─────────────────────────────────────────────
    elif event_type == "invoice.payment_failed":
        email = data_obj.get("customer_email", "")
        if email:
            logger.warning(f"[PAYMENT] Payment failed for {email} — subscription at risk")
            # Production: trigger a "payment failed" email to the user here

    # ── customer.subscription.deleted ─────────────────────────────────────
    elif event_type == "customer.subscription.deleted":
        customer_id = data_obj.get("customer", "")
        period_end  = data_obj.get("current_period_end", time.time())
        for email, sub in _subscriptions.items():
            if sub.get("stripe_customer_id") == customer_id:
                _subscriptions[email]["status"]    = "cancelled"
                _subscriptions[email]["expiry_ts"] = period_end
                logger.info(
                    f"[PAYMENT] Subscription cancelled for {email}, "
                    f"access until {datetime.fromtimestamp(period_end).strftime('%Y-%m-%d')}"
                )
                break

    return jsonify({"status": "ok"})


@payments_bp.route("/api/subscription-status", methods=["GET"])
def subscription_status():
    """
    Check subscription status for an email.
    Query param: ?email=alice@example.com
    """
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