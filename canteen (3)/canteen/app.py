"""
Smart Campus Canteen - Intelligent Pre-Ordering & Queue Management System
Flask + PyMySQL backend.   Run:  python app.py

Payments: Cash on Pickup, and a SIMULATED online payment (demo mode).
No real money is ever transferred and no bank transaction is verified.
"""
import math
import os
import re
import secrets
import socket
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal

import pymysql
import segno
from flask import Flask, jsonify, render_template, request
from pymysql.cursors import DictCursor

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Configuration (all overridable with environment variables)
# ---------------------------------------------------------------------------
def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": env_int("DB_PORT", 3306),
    "user": os.environ.get("DB_USER", "root"),
    "password": os.environ.get("DB_PASSWORD", "system"),
    "database": os.environ.get("DB_NAME", "canteen_db"),
    "charset": "utf8mb4",
    "cursorclass": DictCursor,
    "autocommit": False,
}

# Leave BASE_URL unset to auto-detect (recommended locally). Set it when deployed online.
BASE_URL = os.environ.get("BASE_URL", "").strip().rstrip("/")

# Slot capacity: single source of truth (a slot's own max_capacity column overrides it)
MAX_ORDERS_PER_SLOT = env_int("MAX_ORDERS_PER_SLOT", 15)
# If true, slots whose end time has passed (DB clock) cannot be booked
ONLY_FUTURE_SLOTS = env_bool("ONLY_FUTURE_SLOTS", False)

# ETA model
DEFAULT_PREPARATION_TIME = env_float("DEFAULT_PREPARATION_TIME", 3.0)  # minutes, fallback
MIN_PREPARATION_TIME = env_float("MIN_PREPARATION_TIME", 1.0)          # minutes, floor
MIN_SAMPLES_FOR_HISTORY = env_int("MIN_SAMPLES_FOR_HISTORY", 3)
PREP_HISTORY_DAYS = env_int("PREP_HISTORY_DAYS", 7)

# Orders / inventory
MAX_QTY_PER_ITEM = 20
MAX_NOTE_LENGTH = 150
CANCEL_WINDOW_MINUTES = env_int("CANCEL_WINDOW_MINUTES", 5)
LOW_STOCK_THRESHOLD = 5

# Analytics / forecasting
FORECAST_LOOKBACK_DAYS = 28
TOP_ITEMS_DAYS = 30

QUEUE_STATUSES = ("Pending", "Preparing")
VALID_STATUSES = ("Preparing", "Ready", "Completed")
STATUS_RANK = {"Pending": 0, "Preparing": 1, "Ready": 2, "Completed": 3}

# Payments
PAYMENT_METHODS = ("Cash", "Online")
PAYMENT_LABELS = {"Cash": "Cash on Pickup", "Online": "Online \u2014 Demo"}
PAYMENT_ACTIONS = ("success", "failure", "retry")
CASH_PENDING_MESSAGE = "Order placed successfully. Please pay cash when collecting your order."
ONLINE_PENDING_MESSAGE = "Order placed. Complete the demo payment to continue."


# ---------------------------------------------------------------------------
# Errors + DB helpers
# ---------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


@app.errorhandler(ApiError)
def handle_api_error(err):
    return jsonify({"success": False, "error": err.message}), err.status


@app.errorhandler(pymysql.MySQLError)
def handle_db_error(err):
    app.logger.error("Database error: %s", err)
    return jsonify({
        "success": False,
        "error": "Database error. Make sure MySQL is running, the SQL scripts (including the payment "
                 "migration) were executed, and DB_USER / DB_PASSWORD are correct.",
    }), 500


@app.after_request
def no_cache_for_api(resp):
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def get_db_connection():
    return pymysql.connect(**DB_CONFIG)


@contextmanager
def db_cursor(commit=False):
    """Yield (connection, DictCursor). Rolls back on any error."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            yield conn, cur
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def db_now(cur):
    """Use the database clock everywhere so Python and MySQL never disagree."""
    cur.execute("SELECT NOW() AS now_ts")
    return cur.fetchone()["now_ts"]


# ---------------------------------------------------------------------------
# Time formatting helpers
# ---------------------------------------------------------------------------
def to_minutes(value):
    """MySQL TIME comes back from PyMySQL as timedelta."""
    if isinstance(value, timedelta):
        return int(value.total_seconds() // 60)
    return value.hour * 60 + value.minute


def fmt_minutes(minutes):
    hour24 = (minutes // 60) % 24
    suffix = "AM" if hour24 < 12 else "PM"
    return "%d:%02d %s" % (hour24 % 12 or 12, minutes % 60, suffix)


def fmt_clock(dt):
    return fmt_minutes(dt.hour * 60 + dt.minute)


def fmt_hour(hour):
    hour %= 24
    return "%d %s" % (hour % 12 or 12, "AM" if hour < 12 else "PM")


def slot_label(start, end):
    return "%s - %s" % (fmt_minutes(to_minutes(start)), fmt_minutes(to_minutes(end)))


def fmt_dt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


# ---------------------------------------------------------------------------
# Order / item helpers
# ---------------------------------------------------------------------------
def fetch_order_items(cur, order_ids):
    """Return {order_id: [ {name, quantity, price, options, note}, ... ]}"""
    result = {oid: [] for oid in order_ids}
    if not order_ids:
        return result
    placeholders = ",".join(["%s"] * len(order_ids))
    cur.execute(
        "SELECT oi.order_item_id, oi.order_id, oi.quantity, oi.price, oi.customization_cost, "
        "oi.special_instructions, mi.item_name "
        "FROM order_items oi JOIN menu_items mi ON mi.item_id = oi.item_id "
        "WHERE oi.order_id IN (" + placeholders + ") ORDER BY oi.order_item_id ASC",
        list(order_ids),
    )
    lines = cur.fetchall()
    line_ids = [r["order_item_id"] for r in lines]
    opts = {}
    if line_ids:
        ph2 = ",".join(["%s"] * len(line_ids))
        cur.execute(
            "SELECT order_item_id, option_name FROM order_item_options "
            "WHERE order_item_id IN (" + ph2 + ") ORDER BY option_name ASC",
            line_ids,
        )
        for o in cur.fetchall():
            opts.setdefault(o["order_item_id"], []).append(o["option_name"])
    for row in lines:
        result[row["order_id"]].append({
            "name": row["item_name"],
            "quantity": int(row["quantity"]),
            "price": float(row["price"]) + float(row["customization_cost"] or 0),
            "options": opts.get(row["order_item_id"], []),
            "note": row["special_instructions"] or "",
        })
    return result


def summarize_items(items):
    return ", ".join("%d x %s" % (i["quantity"], i["name"]) for i in items)


def serialize_order(row, items, queue_position=None):
    method = row.get("payment_method") or "Cash"
    return {
        "order_id": row["order_id"],
        "student_name": row["student_name"],
        "items_summary": summarize_items(items),
        "items": items,
        "total_price": float(row["total_price"]),
        "pickup_time": row["pickup_time"],
        "status": row["status"],
        "created_at": fmt_dt(row["created_at"]),
        "queue_position": queue_position,
        "payment_method": method,
        "payment_method_label": PAYMENT_LABELS.get(method, method),
        "payment_status": row.get("payment_status") or "Pending",
    }


# ---------------------------------------------------------------------------
# Queue + ETA
# ---------------------------------------------------------------------------
def get_prep_stats(cur):
    """Average kitchen preparation time (preparing_at -> ready_at) in minutes."""
    cur.execute(
        "SELECT COUNT(*) AS n, AVG(TIMESTAMPDIFF(SECOND, preparing_at, ready_at)) AS avg_sec "
        "FROM orders WHERE preparing_at IS NOT NULL AND ready_at IS NOT NULL "
        "AND ready_at >= preparing_at "
        "AND created_at >= DATE_SUB(CURDATE(), INTERVAL %s DAY)",
        (PREP_HISTORY_DAYS,),
    )
    row = cur.fetchone()
    samples = int(row["n"] or 0)
    if samples >= MIN_SAMPLES_FOR_HISTORY and row["avg_sec"] is not None:
        minutes = max(float(row["avg_sec"]) / 60.0, MIN_PREPARATION_TIME)
        return {"minutes": round(minutes, 2), "samples": samples, "source": "historical"}
    return {"minutes": DEFAULT_PREPARATION_TIME, "samples": samples, "source": "default"}


def get_active_queue(cur):
    """
    Today's orders still waiting for the kitchen (Pending/Preparing).
    Queue order: earliest pickup slot first, then order creation time, then order id.
    """
    cur.execute(
        "SELECT o.order_id, o.student_name, o.total_price, o.pickup_time, o.status, "
        "o.created_at, o.preparing_at, o.ready_at, o.completed_at, "
        "o.payment_method, o.payment_status "
        "FROM orders o JOIN pickup_slots s ON s.slot_id = o.slot_id "
        "WHERE o.status IN (%s, %s) AND o.created_at >= CURDATE() "
        "ORDER BY s.slot_start ASC, o.created_at ASC, o.order_id ASC",
        QUEUE_STATUSES,
    )
    return cur.fetchall()


def build_order_status(cur, order_id, key=None):
    cur.execute(
        "SELECT order_id, student_name, total_price, pickup_time, status, created_at, "
        "preparing_at, ready_at, completed_at, payment_method, payment_status, paid_at, "
        "cancelled_at, cancellation_reason, refund_status, refunded_at, tracking_token "
        "FROM orders WHERE order_id = %s",
        (order_id,),
    )
    order = cur.fetchone()
    if order is None:
        return None

    now = db_now(cur)
    prep = get_prep_stats(cur)
    status = order["status"]
    avg = prep["minutes"]
    queue_position = None
    orders_ahead = 0
    wait_minutes = 0

    if status in QUEUE_STATUSES:
        queue_ids = [q["order_id"] for q in get_active_queue(cur)]
        if order_id in queue_ids:
            orders_ahead = queue_ids.index(order_id)
            queue_position = orders_ahead + 1
        if status == "Preparing" and order["preparing_at"]:
            elapsed = max(0.0, (now - order["preparing_at"]).total_seconds() / 60.0)
            own = max(0.0, avg - elapsed)
        else:
            own = avg
        wait_minutes = max(1, int(math.ceil(orders_ahead * avg + own)))
        expected = now + timedelta(minutes=wait_minutes)
        message = ("The kitchen is preparing your order." if status == "Preparing"
                   else "Your order is in the queue.")
    elif status == "Ready":
        expected = order["ready_at"] or now
        message = "Your order is READY! Please collect it at the counter."
    elif status == "Cancelled":
        expected = order["cancelled_at"] or now
        message = "This order was cancelled."
    else:
        expected = order["completed_at"] or now
        message = "Order completed. Enjoy your meal!"

    items = fetch_order_items(cur, [order_id])[order_id]
    method = order["payment_method"] or "Cash"
    cur.execute(
        "SELECT food_rating, service_rating, wait_rating, comment FROM feedback WHERE order_id = %s",
        (order_id,),
    )
    fb = cur.fetchone()
    authorized = key_matches(order["tracking_token"], key)

    # Cancellation: only while still waiting in the queue, inside the time window
    left = cancel_window_left(order, now)
    if status == "Cancelled":
        can_cancel, cancel_message = False, "This order is already cancelled."
    elif not authorized:
        can_cancel, cancel_message = False, "Open this page from your QR code or order link to cancel."
    elif status != "Pending":
        can_cancel, cancel_message = False, "Cancellation is closed because the kitchen has started your order."
    elif left <= 0:
        can_cancel = False
        cancel_message = "The %d-minute cancellation window has ended." % CANCEL_WINDOW_MINUTES
    else:
        can_cancel, cancel_message = True, "You can cancel while your order is still waiting in the queue."

    refund_status = order["refund_status"] or "None"
    if status != "Cancelled":
        refund_message = ""
    elif refund_status == "Pending":
        refund_message = ("A refund of \u20b9%.2f is due. Please collect it at the canteen counter."
                          % float(order["total_price"]))
    elif refund_status == "Refunded":
        refund_message = "Your refund has been handed back."
    else:
        refund_message = "No payment was taken, so no refund is needed."

    return {
        "order_id": order["order_id"],
        "student_name": order["student_name"],
        "status": status,
        "pickup_time": order["pickup_time"],
        "queue_position": queue_position,
        "orders_ahead": orders_ahead,
        "estimated_wait_minutes": wait_minutes,
        "expected_ready_time": fmt_clock(expected),
        "avg_prep_minutes": avg,
        "eta_source": prep["source"],
        "eta_samples": prep["samples"],
        "total_price": float(order["total_price"]),
        "items": items,
        "items_summary": summarize_items(items),
        "created_at": fmt_dt(order["created_at"]),
        "message": message,
        "payment_method": method,
        "payment_method_label": PAYMENT_LABELS.get(method, method),
        "payment_status": order["payment_status"] or "Pending",
        "authorized": authorized,
        "cancel_window_minutes": CANCEL_WINDOW_MINUTES,
        "can_cancel": can_cancel,
        "cancel_seconds_left": left if can_cancel else 0,
        "cancel_message": cancel_message,
        "cancelled_at": fmt_dt(order["cancelled_at"]),
        "cancellation_reason": order["cancellation_reason"] or "",
        "refund_status": refund_status,
        "refund_message": refund_message,
        "feedback_allowed": authorized and status == "Completed" and fb is None,
        "feedback_submitted": fb is not None,
        "feedback": ({"food_rating": int(fb["food_rating"]), "service_rating": int(fb["service_rating"]),
                      "wait_rating": int(fb["wait_rating"]), "comment": fb["comment"] or ""}
                     if (fb and authorized) else None),
    }


# ---------------------------------------------------------------------------
# Pickup slots
# ---------------------------------------------------------------------------
def get_slots(cur, now):
    """Return (slots, recommended_slot_dict_or_None) using today's real bookings."""
    cur.execute(
        "SELECT s.slot_id, s.slot_start, s.slot_end, "
        "COALESCE(s.max_capacity, %s) AS capacity, COUNT(o.order_id) AS booked "
        "FROM pickup_slots s "
        "LEFT JOIN orders o ON o.slot_id = s.slot_id AND o.created_at >= CURDATE() "
        "AND o.status <> 'Cancelled' "
        "GROUP BY s.slot_id, s.slot_start, s.slot_end, s.max_capacity "
        "ORDER BY s.slot_start ASC",
        (MAX_ORDERS_PER_SLOT,),
    )
    rows = cur.fetchall()
    now_min = now.hour * 60 + now.minute
    slots = []
    for r in rows:
        start = to_minutes(r["slot_start"])
        end = to_minutes(r["slot_end"])
        capacity = int(r["capacity"])
        booked = int(r["booked"])
        remaining = max(0, capacity - booked)
        slots.append({
            "slot_id": r["slot_id"],
            "label": slot_label(r["slot_start"], r["slot_end"]),
            "start_minutes": start,
            "booked": booked,
            "capacity": capacity,
            "remaining": remaining,
            "utilization": int(round(100.0 * booked / capacity)) if capacity else 100,
            "is_full": remaining <= 0,
            "is_closed": bool(ONLY_FUTURE_SLOTS and end <= now_min),
            "is_recommended": False,
        })

    candidates = [s for s in slots if not s["is_full"] and not s["is_closed"]]
    recommended = None
    if candidates:
        best = min(candidates, key=lambda s: (s["booked"] / float(s["capacity"]), s["start_minutes"]))
        best["is_recommended"] = True
        recommended = {k: best[k] for k in ("slot_id", "label", "booked", "remaining", "capacity")}
    return slots, recommended


# ---------------------------------------------------------------------------
# QR code
# ---------------------------------------------------------------------------
def get_lan_ip():
    """Best guess of this PC's address on the local network (no data is actually sent)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def get_base_url():
    """
    1) If BASE_URL is set, use it (deployment).
    2) Otherwise use the address the page was opened with, but swap localhost /
       127.0.0.1 for the PC's LAN IP so a phone can reach it.
    """
    if BASE_URL:
        return BASE_URL
    hostname, _, port = request.host.partition(":")
    if hostname in ("localhost", "127.0.0.1", "[::1]"):
        hostname = get_lan_ip()
    return "%s://%s%s" % (request.scheme, hostname, (":" + port) if port else "")


def track_url(order_id, key=None):
    url = "%s/track/%d" % (get_base_url(), order_id)
    return url + ("?key=" + key) if key else url


def key_matches(stored, supplied):
    """True only if the order has a private key and the caller supplied the same one."""
    if not stored or not supplied:
        return False
    return secrets.compare_digest(str(stored), str(supplied))


def require_order_key(cur, order_id, supplied, lock=False):
    """Fetch the order row (optionally locked) and enforce the private tracking key."""
    cur.execute("SELECT * FROM orders WHERE order_id = %s" + (" FOR UPDATE" if lock else ""), (order_id,))
    row = cur.fetchone()
    if row is None:
        raise ApiError("Order not found.", 404)
    if not key_matches(row["tracking_token"], supplied):
        raise ApiError("Open this order from your QR code or tracking link to do that.", 403)
    return row


def cancel_window_left(order, now):
    """Seconds left to cancel. The clock starts at payment (or at ordering if unpaid/cash)."""
    start = order["paid_at"] or order["created_at"]
    end = start + timedelta(minutes=CANCEL_WINDOW_MINUTES)
    return max(0, int((end - now).total_seconds()))


def make_qr_data_uri(url):
    qr = segno.make(url, error="m")
    try:
        return qr.png_data_uri(scale=8, border=2)
    except Exception:  # pragma: no cover - fallback if PNG writer unavailable
        return qr.svg_data_uri(scale=8, border=2)


# ---------------------------------------------------------------------------
# Student pages / APIs
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/menu")
def api_menu():
    with db_cursor() as (conn, cur):
        cur.execute(
            "SELECT item_id, item_name, price, stock_quantity, is_available "
            "FROM menu_items ORDER BY item_id ASC"
        )
        rows = cur.fetchall()

        # Fetch associated customization options for all menu items
        cur.execute(
            "SELECT mio.item_id, co.option_id, co.option_name, co.extra_price, co.option_group "
            "FROM menu_item_options mio "
            "JOIN customization_options co ON co.option_id = mio.option_id "
            "WHERE co.is_available = 1"
        )
        options_by_item = {}
        for opt in cur.fetchall():
            item_id = opt["item_id"]
            if item_id not in options_by_item:
                options_by_item[item_id] = []
            options_by_item[item_id].append({
                "option_id": opt["option_id"],
                "option_name": opt["option_name"],
                "extra_price": float(opt["extra_price"]),
                "option_group": opt["option_group"]
            })

    items = []
    for r in rows:
        stock = int(r["stock_quantity"])
        sold_out = (not r["is_available"]) or stock <= 0
        items.append({
            "item_id": r["item_id"],
            "item_name": r["item_name"],
            "price": float(r["price"]),
            "stock_quantity": stock,
            "sold_out": sold_out,
            "low_stock": (not sold_out) and stock <= LOW_STOCK_THRESHOLD,
            "options": options_by_item.get(r["item_id"], [])  # Attach the options array here
        })
    return jsonify({"success": True, "items": items, "max_qty_per_item": MAX_QTY_PER_ITEM,
                    "max_note_length": MAX_NOTE_LENGTH})


@app.route("/api/slots")
def api_slots():
    with db_cursor() as (conn, cur):
        now = db_now(cur)
        slots, recommended = get_slots(cur, now)
    return jsonify({
        "success": True,
        "slots": slots,
        "recommended": recommended,
        "max_orders_per_slot": MAX_ORDERS_PER_SLOT,
    })


@app.route("/api/order", methods=["POST"])
def create_order():
    data = request.get_json(silent=True) or {}

    student_name = re.sub(r"[\x00-\x1f\x7f]", "", str(data.get("student_name", ""))).strip()
    if not student_name:
        raise ApiError("Student name is required.")
    if len(student_name) > 100:
        raise ApiError("Student name is too long (max 100 characters).")

    payment_method = str(data.get("payment_method", "")).strip()
    if payment_method not in PAYMENT_METHODS:
        raise ApiError("Please select a payment method (Cash on Pickup or Online Payment - Demo).")

    try:
        slot_id = int(data.get("slot_id"))
    except (TypeError, ValueError):
        raise ApiError("Please select a pickup time slot.")

    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ApiError("Your cart is empty.")
    if len(raw_items) > 50:
        raise ApiError("Too many items in one order.")

    quantities = {}      # item_id -> total qty (for stock)
    lines = []           # each cart line: item_id, qty, option ids, note
    for entry in raw_items:
        if not isinstance(entry, dict):
            raise ApiError("Invalid item data.")
        try:
            item_id = int(entry.get("item_id"))
            qty = int(entry.get("quantity"))
        except (TypeError, ValueError):
            raise ApiError("Invalid item data.")
        if qty < 1:
            raise ApiError("Quantity must be at least 1.")
        raw_opts = entry.get("options") or []
        if not isinstance(raw_opts, list):
            raise ApiError("Invalid customization data.")
        try:
            opt_ids = sorted(set(int(x) for x in raw_opts))
        except (TypeError, ValueError):
            raise ApiError("Invalid customization data.")
        note = re.sub(r"[\x00-\x1f\x7f]", " ", str(entry.get("note") or ""))
        note = re.sub(r"\s+", " ", note).strip()[:MAX_NOTE_LENGTH]
        lines.append({"item_id": item_id, "qty": qty, "opt_ids": opt_ids, "note": note})
        quantities[item_id] = quantities.get(item_id, 0) + qty
    if any(q > MAX_QTY_PER_ITEM for q in quantities.values()):
        raise ApiError("You can order at most %d of any single item." % MAX_QTY_PER_ITEM)

    with db_cursor(commit=True) as (conn, cur):
        # 1) Lock the slot row first. Everyone ordering for this slot is serialized here,
        #    so the capacity count below cannot be raced past.
        cur.execute(
            "SELECT slot_id, slot_start, slot_end, COALESCE(max_capacity, %s) AS capacity "
            "FROM pickup_slots WHERE slot_id = %s FOR UPDATE",
            (MAX_ORDERS_PER_SLOT, slot_id),
        )
        slot = cur.fetchone()
        if slot is None:
            raise ApiError("The selected pickup slot does not exist.")

        now = db_now(cur)
        if ONLY_FUTURE_SLOTS and to_minutes(slot["slot_end"]) <= now.hour * 60 + now.minute:
            raise ApiError("That pickup slot has already passed. Please choose another.", 409)

        # Plain (snapshot) read taken AFTER the lock, so it sees every committed order.
        cur.execute(
            "SELECT COUNT(*) AS booked FROM orders WHERE slot_id = %s AND created_at >= CURDATE() "
            "AND status <> 'Cancelled'",
            (slot_id,),
        )
        if int(cur.fetchone()["booked"]) >= int(slot["capacity"]):
            raise ApiError("That pickup slot is full. Please choose another slot.", 409)

        # 2) Lock the menu rows (always in item_id order to avoid deadlocks) and validate stock.
        ids = sorted(quantities)
        placeholders = ",".join(["%s"] * len(ids))
        cur.execute(
            "SELECT item_id, item_name, price, stock_quantity, is_available FROM menu_items "
            "WHERE item_id IN (" + placeholders + ") ORDER BY item_id ASC FOR UPDATE",
            ids,
        )
        rows = {r["item_id"]: r for r in cur.fetchall()}
        if len(rows) != len(ids):
            raise ApiError("One or more items no longer exist. Please refresh the page.")

        stock_updates = []
        for item_id in ids:
            row = rows[item_id]
            qty = quantities[item_id]
            stock = int(row["stock_quantity"])
            if (not row["is_available"]) or stock <= 0:
                raise ApiError("%s is sold out." % row["item_name"], 409)
            if qty > stock:
                raise ApiError("Only %d x %s left in stock." % (stock, row["item_name"]), 409)
            new_stock = stock - qty
            stock_updates.append((new_stock, 1 if new_stock > 0 else 0, item_id))

        # Allowed customizations per dish (price + group always come from the database)
        cur.execute(
            "SELECT mio.item_id, co.option_id, co.option_name, co.extra_price, co.option_group "
            "FROM menu_item_options mio JOIN customization_options co ON co.option_id = mio.option_id "
            "WHERE co.is_available = 1 AND mio.item_id IN (" + placeholders + ")",
            ids,
        )
        allowed = {}
        for o in cur.fetchall():
            allowed.setdefault(o["item_id"], {})[o["option_id"]] = o

        total = Decimal("0.00")
        priced_lines = []
        for ln in lines:
            row = rows[ln["item_id"]]
            chosen = []
            groups = set()
            for oid in ln["opt_ids"]:
                opt = allowed.get(ln["item_id"], {}).get(oid)
                if opt is None:
                    raise ApiError("A selected customization is not available for %s." % row["item_name"], 409)
                if opt["option_group"]:
                    if opt["option_group"] in groups:
                        raise ApiError("Choose only one '%s' option for %s." % (opt["option_group"], row["item_name"]))
                    groups.add(opt["option_group"])
                chosen.append(opt)
            extra = sum((Decimal(o["extra_price"]) for o in chosen), Decimal("0.00"))
            unit_price = Decimal(row["price"])
            total += (unit_price + extra) * ln["qty"]
            priced_lines.append((ln, unit_price, extra, chosen))

        # 3) Write the order, its lines, and decrement stock - all in one transaction.
        #    Payment always starts as Pending; only the payment endpoints can change it.
        label = slot_label(slot["slot_start"], slot["slot_end"])
        tracking_key = secrets.token_urlsafe(24)
        cur.execute(
            "INSERT INTO orders (student_name, total_price, slot_id, pickup_time, status, "
            "payment_method, payment_status, tracking_token) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (student_name, total, slot_id, label, "Pending", payment_method, "Pending", tracking_key),
        )
        order_id = cur.lastrowid
        for ln, unit_price, extra, chosen in priced_lines:
            cur.execute(
                "INSERT INTO order_items (order_id, item_id, quantity, price, customization_cost, "
                "special_instructions) VALUES (%s, %s, %s, %s, %s, %s)",
                (order_id, ln["item_id"], ln["qty"], unit_price, extra, ln["note"] or None),
            )
            line_id = cur.lastrowid
            for opt in chosen:
                cur.execute(
                    "INSERT INTO order_item_options (order_item_id, option_id, option_name, extra_price) "
                    "VALUES (%s, %s, %s, %s)",
                    (line_id, opt["option_id"], opt["option_name"], opt["extra_price"]),
                )
        for new_stock, available, item_id in stock_updates:
            cur.execute(
                "UPDATE menu_items SET stock_quantity = %s, is_available = %s WHERE item_id = %s",
                (new_stock, available, item_id),
            )

    # Transaction committed - build the response with live ETA data
    with db_cursor() as (conn, cur):
        status = build_order_status(cur, order_id, tracking_key)

    url = track_url(order_id, tracking_key)
    try:
        qr = make_qr_data_uri(url)
    except Exception as exc:  # QR is a bonus; never fail an order because of it
        app.logger.error("QR generation failed: %s", exc)
        qr = None

    is_online = payment_method == "Online"
    return jsonify({
        "success": True,
        "token": order_id,
        "key": tracking_key,
        "track_url": url,
        "qr_data_uri": qr,
        "order": status,
        "payment_method": payment_method,
        "payment_status": "Pending",
        "payment_url": ("/payment/%d?key=%s" % (order_id, tracking_key)) if is_online else None,
        "message": ONLINE_PENDING_MESSAGE if is_online else CASH_PENDING_MESSAGE,
    }), 201


@app.route("/api/order/<int:order_id>/status")
def api_order_status(order_id):
    with db_cursor() as (conn, cur):
        status = build_order_status(cur, order_id, request.args.get("key", ""))
    if status is None:
        raise ApiError("Order not found.", 404)
    return jsonify(dict(status, success=True))


@app.route("/api/order/<int:order_id>/qr")
def api_order_qr(order_id):
    """QR code + private tracking URL for an existing order (used by the payment page)."""
    key = request.args.get("key", "")
    with db_cursor() as (conn, cur):
        require_order_key(cur, order_id, key)
    url = track_url(order_id, key)
    try:
        qr = make_qr_data_uri(url)
    except Exception as exc:
        app.logger.error("QR generation failed: %s", exc)
        qr = None
    return jsonify({"success": True, "track_url": url, "qr_data_uri": qr})


@app.route("/track/<int:order_id>")
def track(order_id):
    return render_template("track.html", order_id=order_id)


# ---------------------------------------------------------------------------
# Payments (cash + SIMULATED online payment)
# ---------------------------------------------------------------------------
@app.route("/payment/<int:order_id>")
def payment_page(order_id):
    return render_template("payment.html", order_id=order_id)


@app.route("/api/payment/<int:order_id>/simulate", methods=["POST"])
def api_payment_simulate(order_id):
    """
    DEMO ONLY. Simulates the outcome of an online payment.
    action = "success"  : Pending/Failed -> Paid
             "failure"  : Pending/Failed -> Failed
             "retry"    : Failed         -> Pending  (same order, no new order, no stock change)
    No real money moves and no bank is contacted. The amount is never taken from the browser;
    the order's stored total_price is the only amount that exists.
    """
    data = request.get_json(silent=True) or {}
    action = str(data.get("action", "")).strip().lower()
    supplied_key = str(data.get("key", ""))
    if action not in PAYMENT_ACTIONS:
        raise ApiError("Invalid payment action.")

    allowed_from = {
        "success": ("Pending", "Failed"),
        "failure": ("Pending", "Failed"),
        "retry": ("Failed",),
    }
    new_status_for = {"success": "Paid", "failure": "Failed", "retry": "Pending"}
    message_for = {
        "success": "Demo payment successful.",
        "failure": "Demo payment failed.",
        "retry": "Payment reset. You can retry the demo payment.",
    }

    with db_cursor(commit=True) as (conn, cur):
        row = require_order_key(cur, order_id, supplied_key, lock=True)
        if row["status"] == "Cancelled":
            raise ApiError("This order was cancelled. No payment is needed.", 409)
        if row["payment_method"] != "Online":
            raise ApiError("This is a cash order. Online payment simulation is not available for it.", 400)

        current = row["payment_status"]
        if current == "Paid":
            raise ApiError("This order is already paid.", 409)
        if current not in allowed_from[action]:
            raise ApiError("This action is not allowed while the payment is %s." % current, 409)

        new_status = new_status_for[action]
        cur.execute(
            "UPDATE orders SET payment_status = %s, paid_at = " + ("NOW()" if new_status == "Paid" else "NULL") +
            " WHERE order_id = %s",
            (new_status, order_id),
        )

    return jsonify({
        "success": True,
        "order_id": order_id,
        "action": action,
        "payment_status": new_status,
        "message": message_for[action],
        "demo": True,
    })


@app.route("/api/payment/<int:order_id>/cash-paid", methods=["POST"])
def api_payment_cash_paid(order_id):
    """Kitchen staff confirm that cash was received for a Cash order."""
    with db_cursor(commit=True) as (conn, cur):
        cur.execute(
            "SELECT order_id, payment_method, payment_status FROM orders "
            "WHERE order_id = %s FOR UPDATE",
            (order_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ApiError("Order not found.", 404)
        if row["payment_method"] != "Cash":
            raise ApiError("Only cash orders can be marked as cash paid.", 400)
        cur.execute("SELECT status FROM orders WHERE order_id = %s", (order_id,))
        if cur.fetchone()["status"] == "Cancelled":
            raise ApiError("This order was cancelled.", 409)
        if row["payment_status"] == "Paid":
            raise ApiError("This order is already marked as paid.", 409)
        if row["payment_status"] != "Pending":
            raise ApiError("Payment status is not Pending.", 409)
        cur.execute(
            "UPDATE orders SET payment_status = %s, paid_at = NOW() WHERE order_id = %s",
            ("Paid", order_id),
        )
    return jsonify({
        "success": True,
        "order_id": order_id,
        "payment_status": "Paid",
        "message": "Cash payment recorded.",
    })


# ---------------------------------------------------------------------------
# Kitchen
# ---------------------------------------------------------------------------
@app.route("/kitchen")
def kitchen():
    return render_template("kitchen.html")


@app.route("/api/orders")
def api_orders():
    with db_cursor() as (conn, cur):
        prep = get_prep_stats(cur)
        queue = get_active_queue(cur)
        cur.execute(
            "SELECT order_id, student_name, total_price, pickup_time, status, created_at, "
            "payment_method, payment_status "
            "FROM orders WHERE status = %s AND created_at >= CURDATE() "
            "ORDER BY ready_at ASC, order_id ASC",
            ("Ready",),
        )
        ready = cur.fetchall()
        ids = [o["order_id"] for o in queue] + [o["order_id"] for o in ready]
        items = fetch_order_items(cur, ids)
        cur.execute(
            "SELECT order_id, student_name, total_price, payment_method, payment_status, "
            "cancelled_at, cancellation_reason, refund_status FROM orders "
            "WHERE status = 'Cancelled' AND cancelled_at >= CURDATE() ORDER BY cancelled_at DESC"
        )
        cancelled = [{
            "order_id": c["order_id"], "student_name": c["student_name"],
            "payment_method_label": PAYMENT_LABELS.get(c["payment_method"], c["payment_method"]),
            "total_price": float(c["total_price"]),
            "cancelled_at": fmt_dt(c["cancelled_at"]),
            "cancellation_reason": c["cancellation_reason"] or "",
            "refund_status": c["refund_status"],
        } for c in cur.fetchall()]

    orders = []
    for pos, o in enumerate(queue, start=1):
        orders.append(serialize_order(o, items[o["order_id"]], pos))
    for o in ready:
        orders.append(serialize_order(o, items[o["order_id"]], None))

    return jsonify({
        "success": True,
        "orders": orders,
        "cancelled": cancelled,
        "meta": {
            "pending": sum(1 for o in queue if o["status"] == "Pending"),
            "preparing": sum(1 for o in queue if o["status"] == "Preparing"),
            "ready": len(ready),
            "avg_prep_minutes": prep["minutes"],
            "prep_source": prep["source"],
        },
    })


@app.route("/api/update_status", methods=["POST"])
def update_status():
    """Food-preparation status only. Never touches payment_status."""
    data = request.get_json(silent=True) or {}
    status = str(data.get("status", "")).strip()
    try:
        order_id = int(data.get("order_id"))
    except (TypeError, ValueError):
        raise ApiError("Invalid order id.")
    if status not in VALID_STATUSES:
        raise ApiError("Invalid status.")

    # Fixed SQL per status (no user input is ever concatenated)
    sql_by_status = {
        "Preparing": "UPDATE orders SET status = %s, preparing_at = COALESCE(preparing_at, NOW()), "
                     "ready_at = NULL, completed_at = NULL WHERE order_id = %s",
        "Ready": "UPDATE orders SET status = %s, ready_at = NOW(), completed_at = NULL "
                 "WHERE order_id = %s",
        "Completed": "UPDATE orders SET status = %s, completed_at = NOW() WHERE order_id = %s",
    }

    with db_cursor(commit=True) as (conn, cur):
        cur.execute("SELECT status FROM orders WHERE order_id = %s FOR UPDATE", (order_id,))
        row = cur.fetchone()
        if row is None:
            raise ApiError("Order not found.", 404)
        if row["status"] == "Completed":
            raise ApiError("This order is already completed.", 409)
        if row["status"] == "Cancelled":
            raise ApiError("This order was cancelled.", 409)
        cur.execute(sql_by_status[status], (status, order_id))

    return jsonify({"success": True, "order_id": order_id, "status": status})


# ---------------------------------------------------------------------------
# Public display board
# ---------------------------------------------------------------------------
def fetch_board_tokens(cur):
    cur.execute(
        "SELECT order_id FROM orders WHERE status = %s AND created_at >= CURDATE() "
        "ORDER BY ready_at ASC, order_id ASC",
        ("Ready",),
    )
    ready = [r["order_id"] for r in cur.fetchall()]
    preparing = [o["order_id"] for o in get_active_queue(cur)]
    return ready, preparing


@app.route("/display")
def display():
    try:
        with db_cursor() as (conn, cur):
            ready, preparing = fetch_board_tokens(cur)
    except pymysql.MySQLError as exc:
        app.logger.error("Display board DB error: %s", exc)
        ready, preparing = [], []
    return render_template("display.html", ready=ready, preparing=preparing)


@app.route("/api/display")
def api_display():
    with db_cursor() as (conn, cur):
        ready, preparing = fetch_board_tokens(cur)
    return jsonify({"success": True, "ready": ready, "preparing": preparing})


# ---------------------------------------------------------------------------
# Analytics + demand forecast
# ---------------------------------------------------------------------------
def forecast_value(series, days, target_weekday):
    """
    Explainable demand model for one item. `series` = units sold on each past operating day
    (oldest first), `days` = matching dates.
      < 3 days : plain average of what we have            (low confidence)
      3-6 days : weighted moving average, newest weighted most (medium)
      >= 7 days: 60% weighted moving average + 40% same-weekday average (high)
    """
    n = len(series)
    if n < 3:
        return sum(series) / float(n)
    recent = series[-7:]
    weights = list(range(1, len(recent) + 1))
    wma = sum(v * w for v, w in zip(recent, weights)) / float(sum(weights))
    if n >= 7:
        same = [v for v, d in zip(series, days) if d.weekday() == target_weekday]
        if len(same) >= 2:
            return 0.6 * wma + 0.4 * (sum(same) / float(len(same)))
    return wma


def build_forecast(cur, now):
    today = now.date()
    target = today + timedelta(days=1)

    cur.execute("SELECT item_id, item_name, stock_quantity FROM menu_items ORDER BY item_id ASC")
    menu = cur.fetchall()

    # Only COMPLETE past days are used for forecasting (today is still in progress)
    cur.execute(
        "SELECT DATE(created_at) AS d FROM orders "
        "WHERE created_at >= DATE_SUB(CURDATE(), INTERVAL %s DAY) AND created_at < CURDATE() "
        "AND status <> 'Cancelled' GROUP BY DATE(created_at) ORDER BY d ASC",
        (FORECAST_LOOKBACK_DAYS,),
    )
    days = [r["d"] for r in cur.fetchall()]

    base = {
        "target_date": target.strftime("%A, %d %b %Y"),
        "history_days": len(days),
        "lookback_days": FORECAST_LOOKBACK_DAYS,
    }

    if not days:
        cur.execute(
            "SELECT oi.item_id, SUM(oi.quantity) AS qty FROM order_items oi "
            "JOIN orders o ON o.order_id = oi.order_id WHERE o.created_at >= CURDATE() AND o.status <> 'Cancelled' "
            "GROUP BY oi.item_id"
        )
        today_sales = {r["item_id"]: int(r["qty"]) for r in cur.fetchall()}
        return dict(base, status="insufficient",
                    message="Not enough historical data for reliable prediction.",
                    detail="The forecast needs completed past days of orders. "
                           "Below is the demand observed today so far.",
                    items=[{"item_name": m["item_name"],
                            "today_so_far": today_sales.get(m["item_id"], 0),
                            "stock": int(m["stock_quantity"])} for m in menu],
                    recommendations=[])

    cur.execute(
        "SELECT DATE(o.created_at) AS d, oi.item_id, SUM(oi.quantity) AS qty "
        "FROM order_items oi JOIN orders o ON o.order_id = oi.order_id "
        "WHERE o.created_at >= DATE_SUB(CURDATE(), INTERVAL %s DAY) AND o.created_at < CURDATE() "
        "AND o.status <> 'Cancelled' GROUP BY DATE(o.created_at), oi.item_id",
        (FORECAST_LOOKBACK_DAYS,),
    )
    sales = {(r["d"], r["item_id"]): int(r["qty"]) for r in cur.fetchall()}

    n = len(days)
    if n < 3:
        confidence, margin = "low", 0.20
        method = "Recent average (very limited history - treat as a rough estimate)"
    elif n < 7:
        confidence, margin = "medium", 0.15
        method = "Weighted moving average (recent days count more)"
    else:
        confidence, margin = "high", 0.10
        method = "Weighted moving average blended with same-weekday average"

    items = []
    recommendations = []
    for m in menu:
        series = [sales.get((d, m["item_id"]), 0) for d in days]
        value = forecast_value(series, days, target.weekday())
        expected = int(round(value))
        low = max(0, int(math.floor(value * (1 - margin))))
        high = expected
        stock = int(m["stock_quantity"])

        surplus = max(0, stock - expected)
        shortfall = max(0, expected - stock)
        if expected == 0:
            risk, note = "No demand data", "No recent sales recorded for this item."
        elif stock < expected:
            risk = "Shortfall risk"
            note = "Stock (%d) is below expected demand (%d). Consider preparing %d more." % (
                stock, expected, shortfall)
        elif surplus <= 0.10 * expected:
            risk, note = "Low", "Stock closely matches expected demand."
        else:
            pct = int(round(surplus * 100.0 / stock)) if stock else 0
            risk = "Medium" if surplus <= 0.30 * expected else "High"
            note = ("Potential surplus: %d units above expected demand. "
                    "Consider reducing preparation by approximately %d%%." % (surplus, pct))

        if expected > 0:
            rng = str(high) if low == high else "%d-%d" % (low, high)
            recommendations.append("Prepare approximately %s %s tomorrow." % (rng, m["item_name"]))

        items.append({
            "item_id": m["item_id"], "item_name": m["item_name"],
            "expected": expected, "prep_low": low, "prep_high": high,
            "stock": stock, "potential_surplus": surplus, "shortfall": shortfall,
            "risk": risk, "note": note,
        })

    items.sort(key=lambda x: (-x["expected"], x["item_name"]))
    return dict(base, status="ok", method=method, confidence=confidence,
                message="Based on %d day(s) of order history." % n,
                items=items, recommendations=recommendations)


def build_cancellations(cur):
    cur.execute(
        "SELECT COUNT(*) AS n FROM orders WHERE status = 'Cancelled' AND cancelled_at >= CURDATE()"
    )
    cancelled_today = int(cur.fetchone()["n"])
    # Money still owed to students, whichever day the order was cancelled
    cur.execute("SELECT COUNT(*) AS n FROM orders WHERE status = 'Cancelled' AND refund_status = 'Pending'")
    pending = int(cur.fetchone()["n"])
    cur.execute(
        "SELECT COUNT(*) AS n FROM orders WHERE status = 'Cancelled' AND refund_status = 'Refunded' "
        "AND cancelled_at >= CURDATE()"
    )
    recorded = int(cur.fetchone()["n"])
    return {"cancelled_today": cancelled_today, "refunds_pending": pending, "refunds_recorded": recorded}


def build_feedback(cur):
    cur.execute(
        "SELECT COUNT(*) AS total, AVG(food_rating) AS food, AVG(service_rating) AS service, "
        "AVG(wait_rating) AS wait FROM feedback"
    )
    r = cur.fetchone()
    total = int(r["total"] or 0)
    out = {"food": None, "service": None, "wait": None, "overall": None, "total": total,
           "lowest_area": None, "lowest_items": [], "recent_comments": []}
    if not total:
        return out

    food, service, wait = float(r["food"]), float(r["service"]), float(r["wait"])
    out.update(food=round(food, 2), service=round(service, 2), wait=round(wait, 2),
               overall=round((food + service + wait) / 3.0, 2))
    areas = [("Food quality", food), ("Service", service), ("Waiting time", wait)]
    name, val = min(areas, key=lambda a: a[1])
    out["lowest_area"] = {"name": name, "average": round(val, 2)}

    # Average food rating of orders that contained each item (an estimate - ratings are per order)
    cur.execute(
        "SELECT mi.item_name, AVG(f.food_rating) AS avg_food, COUNT(DISTINCT f.feedback_id) AS ratings "
        "FROM feedback f JOIN order_items oi ON oi.order_id = f.order_id "
        "JOIN menu_items mi ON mi.item_id = oi.item_id "
        "GROUP BY mi.item_id, mi.item_name ORDER BY avg_food ASC, ratings DESC, mi.item_name ASC LIMIT 3"
    )
    out["lowest_items"] = [{"name": i["item_name"], "avg_food": round(float(i["avg_food"]), 2),
                            "ratings": int(i["ratings"])} for i in cur.fetchall()]

    cur.execute(
        "SELECT order_id, food_rating, service_rating, wait_rating, comment, created_at FROM feedback "
        "WHERE comment IS NOT NULL AND comment <> '' ORDER BY created_at DESC, feedback_id DESC LIMIT 5"
    )
    out["recent_comments"] = [{
        "order_id": c["order_id"], "food_rating": int(c["food_rating"]),
        "service_rating": int(c["service_rating"]), "wait_rating": int(c["wait_rating"]),
        "comment": c["comment"], "created_at": fmt_dt(c["created_at"]),
    } for c in cur.fetchall()]
    return out


def build_customizations(cur):
    since = "o.created_at >= DATE_SUB(CURDATE(), INTERVAL 29 DAY) AND o.status <> 'Cancelled'"

    def units_for(option_name):
        cur.execute(
            "SELECT COALESCE(SUM(oi.quantity), 0) AS n FROM order_item_options oio "
            "JOIN order_items oi ON oi.order_item_id = oio.order_item_id "
            "JOIN orders o ON o.order_id = oi.order_id "
            "WHERE oio.option_name = %s AND " + since,
            (option_name,),
        )
        return int(cur.fetchone()["n"])

    cur.execute(
        "SELECT COUNT(*) AS n FROM order_items oi JOIN orders o ON o.order_id = oi.order_id "
        "WHERE oi.special_instructions IS NOT NULL AND oi.special_instructions <> '' AND " + since
    )
    lines_with_notes = int(cur.fetchone()["n"])

    cur.execute(
        "SELECT oio.option_name, SUM(oi.quantity) AS qty FROM order_item_options oio "
        "JOIN order_items oi ON oi.order_item_id = oio.order_item_id "
        "JOIN orders o ON o.order_id = oi.order_id WHERE " + since + " "
        "GROUP BY oio.option_name ORDER BY qty DESC, oio.option_name ASC LIMIT 8"
    )
    top = [{"name": r["option_name"], "quantity": int(r["qty"])} for r in cur.fetchall()]

    cur.execute(
        "SELECT combo, SUM(qty) AS qty FROM ("
        "  SELECT GROUP_CONCAT(oio.option_name ORDER BY oio.option_name SEPARATOR ' + ') AS combo, "
        "         oi.quantity AS qty "
        "  FROM order_item_options oio JOIN order_items oi ON oi.order_item_id = oio.order_item_id "
        "  JOIN orders o ON o.order_id = oi.order_id WHERE " + since + " "
        "  GROUP BY oi.order_item_id, oi.quantity HAVING COUNT(*) >= 2"
        ") t GROUP BY combo ORDER BY qty DESC, combo ASC LIMIT 5"
    )
    combos = [{"combo": r["combo"], "quantity": int(r["qty"])} for r in cur.fetchall()]

    return {"no_onion": units_for("No onion"), "less_spicy": units_for("Less spicy"),
            "no_sugar": units_for("No sugar"), "lines_with_notes": lines_with_notes,
            "top": top, "combos": combos}


def build_analytics(cur):
    now = db_now(cur)

    # ---- KPIs (today) ----
    cur.execute(
        "SELECT COUNT(*) AS total_orders, COALESCE(SUM(total_price), 0) AS revenue, "
        "COALESCE(SUM(status = 'Completed'), 0) AS completed, "
        "COALESCE(SUM(status = 'Pending'), 0) AS pending, "
        "COALESCE(SUM(status = 'Preparing'), 0) AS preparing, "
        "COALESCE(SUM(status = 'Ready'), 0) AS ready "
        "FROM orders WHERE created_at >= CURDATE() AND status <> 'Cancelled'"
    )
    k = cur.fetchone()
    total_orders = int(k["total_orders"])
    revenue = float(k["revenue"])

    cur.execute(
        "SELECT AVG(TIMESTAMPDIFF(SECOND, preparing_at, ready_at)) AS avg_sec "
        "FROM orders WHERE created_at >= CURDATE() AND preparing_at IS NOT NULL "
        "AND ready_at IS NOT NULL AND ready_at >= preparing_at"
    )
    avg_sec = cur.fetchone()["avg_sec"]

    kpis = {
        "orders_today": total_orders,
        "revenue_today": round(revenue, 2),
        "completed": int(k["completed"]),
        "pending": int(k["pending"]),
        "preparing": int(k["preparing"]),
        "ready": int(k["ready"]),
        "avg_prep_minutes": round(float(avg_sec) / 60.0, 1) if avg_sec is not None else None,
        "avg_order_value": round(revenue / total_orders, 2) if total_orders else 0,
    }

    # ---- Orders / revenue by hour (today) ----
    cur.execute(
        "SELECT HOUR(created_at) AS h, COUNT(*) AS orders, COALESCE(SUM(total_price), 0) AS revenue "
        "FROM orders WHERE created_at >= CURDATE() AND status <> 'Cancelled' "
        "GROUP BY HOUR(created_at) ORDER BY h ASC"
    )
    hour_rows = {int(r["h"]): r for r in cur.fetchall()}
    lo = min([8] + list(hour_rows))
    hi = max([20] + list(hour_rows))
    hours = list(range(lo, hi + 1))
    hourly = {
        "labels": [fmt_hour(h) for h in hours],
        "orders": [int(hour_rows[h]["orders"]) if h in hour_rows else 0 for h in hours],
        "revenue": [float(hour_rows[h]["revenue"]) if h in hour_rows else 0 for h in hours],
    }
    peak_hour = None
    if hour_rows:
        ph = max(sorted(hour_rows), key=lambda h: int(hour_rows[h]["orders"]))
        peak_hour = {"label": "%s - %s" % (fmt_hour(ph), fmt_hour(ph + 1)),
                     "orders": int(hour_rows[ph]["orders"])}

    # ---- Last 7 days: revenue + orders + prep time ----
    dates = [now.date() - timedelta(days=i) for i in range(6, -1, -1)]
    cur.execute(
        "SELECT DATE(created_at) AS d, COUNT(*) AS orders, COALESCE(SUM(total_price), 0) AS revenue "
        "FROM orders WHERE created_at >= DATE_SUB(CURDATE(), INTERVAL 6 DAY) AND status <> 'Cancelled' "
        "GROUP BY DATE(created_at)"
    )
    day_rows = {r["d"]: r for r in cur.fetchall()}
    daily = {
        "labels": [d.strftime("%a %d %b") for d in dates],
        "revenue": [float(day_rows[d]["revenue"]) if d in day_rows else 0 for d in dates],
        "orders": [int(day_rows[d]["orders"]) if d in day_rows else 0 for d in dates],
    }
    cur.execute(
        "SELECT DATE(created_at) AS d, AVG(TIMESTAMPDIFF(SECOND, preparing_at, ready_at)) AS avg_sec "
        "FROM orders WHERE created_at >= DATE_SUB(CURDATE(), INTERVAL 6 DAY) "
        "AND preparing_at IS NOT NULL AND ready_at IS NOT NULL AND ready_at >= preparing_at "
        "GROUP BY DATE(created_at)"
    )
    prep_rows = {r["d"]: r["avg_sec"] for r in cur.fetchall()}
    prep_trend = {
        "labels": daily["labels"],
        "values": [round(float(prep_rows[d]) / 60.0, 2) if prep_rows.get(d) is not None else None
                   for d in dates],
    }

    # ---- Top selling items ----
    cur.execute(
        "SELECT mi.item_name, SUM(oi.quantity) AS qty FROM order_items oi "
        "JOIN orders o ON o.order_id = oi.order_id "
        "JOIN menu_items mi ON mi.item_id = oi.item_id "
        "WHERE o.created_at >= DATE_SUB(CURDATE(), INTERVAL %s DAY) AND o.status <> 'Cancelled' "
        "GROUP BY mi.item_id, mi.item_name ORDER BY qty DESC, mi.item_name ASC LIMIT 5",
        (TOP_ITEMS_DAYS - 1,),
    )
    top_items = [{"name": r["item_name"], "quantity": int(r["qty"])} for r in cur.fetchall()]

    # ---- Slot utilization (today) ----
    slots, _rec = get_slots(cur, now)
    slot_rows = [{"label": s["label"], "booked": s["booked"], "capacity": s["capacity"]} for s in slots]
    busiest = max(slots, key=lambda s: s["booked"]) if slots else None
    busiest_slot = None
    if busiest and busiest["booked"] > 0:
        busiest_slot = {"label": busiest["label"], "booked": busiest["booked"],
                        "capacity": busiest["capacity"]}

    cur.execute("SELECT COUNT(*) AS n FROM orders WHERE status IN (%s, %s, %s) AND created_at >= CURDATE()",
                ("Pending", "Preparing", "Ready"))
    active_orders = int(cur.fetchone()["n"])

    return {
        "success": True,
        "generated_at": fmt_dt(now),
        "kpis": dict(kpis, active_orders=active_orders),
        "peak_hour": peak_hour,
        "busiest_slot": busiest_slot,
        "most_popular": top_items[0] if top_items else None,
        "top_items_days": TOP_ITEMS_DAYS,
        "hourly": hourly,
        "daily": daily,
        "prep_trend": prep_trend,
        "top_items": top_items,
        "status_distribution": {
            "labels": ["Pending", "Preparing", "Ready", "Completed"],
            "values": [kpis["pending"], kpis["preparing"], kpis["ready"], kpis["completed"]],
        },
        "slots": slot_rows,
        "forecast": build_forecast(cur, now),
        "settings": {"max_orders_per_slot": MAX_ORDERS_PER_SLOT,
                     "default_prep_minutes": DEFAULT_PREPARATION_TIME},
        "cancellations": build_cancellations(cur),
        "feedback": build_feedback(cur),
        "customizations": build_customizations(cur),
    }


@app.route("/analytics")
def analytics():
    return render_template("analytics.html")


@app.route("/api/analytics")
def api_analytics():
    with db_cursor() as (conn, cur):
        payload = build_analytics(cur)
    return jsonify(payload)


@app.route("/api/stock", methods=["POST"])
def api_stock():
    """Set the stock (prepared quantity) of one menu item. Stock > 0 re-enables the item."""
    data = request.get_json(silent=True) or {}
    try:
        item_id = int(data.get("item_id"))
        stock = int(data.get("stock_quantity"))
    except (TypeError, ValueError):
        raise ApiError("Invalid item or stock value.")
    if stock < 0 or stock > 10000:
        raise ApiError("Stock must be between 0 and 10000.")
    with db_cursor(commit=True) as (conn, cur):
        cur.execute("SELECT item_id FROM menu_items WHERE item_id = %s FOR UPDATE", (item_id,))
        if cur.fetchone() is None:
            raise ApiError("Item not found.", 404)
        cur.execute(
            "UPDATE menu_items SET stock_quantity = %s, is_available = %s WHERE item_id = %s",
            (stock, 1 if stock > 0 else 0, item_id),
        )
    return jsonify({"success": True, "item_id": item_id, "stock_quantity": stock})


@app.route("/api/order/<int:order_id>/cancel", methods=["POST"])
def api_cancel_order(order_id):
    data = request.get_json(silent=True) or {}
    reason = re.sub(r"[\x00-\x1f\x7f]", " ", str(data.get("reason") or "")).strip()[:200]

    with db_cursor(commit=True) as (conn, cur):
        order = require_order_key(cur, order_id, str(data.get("key", "")), lock=True)
        if order["status"] == "Cancelled":
            raise ApiError("This order is already cancelled.", 409)
        if order["status"] != "Pending":
            raise ApiError("Cancellation is closed because the kitchen has started your order.", 409)
        if cancel_window_left(order, db_now(cur)) <= 0:
            raise ApiError("The %d-minute cancellation window has ended." % CANCEL_WINDOW_MINUTES, 409)

        # Paid orders need a refund; unpaid ones just disappear
        refund_status = "Pending" if order["payment_status"] == "Paid" else "None"
        cur.execute(
            "UPDATE orders SET status = 'Cancelled', cancelled_at = NOW(), cancellation_reason = %s, "
            "refund_status = %s WHERE order_id = %s",
            (reason or None, refund_status, order_id),
        )

        # Put the stock back (lock menu rows in id order, like create_order, to avoid deadlocks)
        cur.execute(
            "SELECT item_id, SUM(quantity) AS qty FROM order_items WHERE order_id = %s "
            "GROUP BY item_id ORDER BY item_id ASC", (order_id,))
        for item in cur.fetchall():
            cur.execute(
                "UPDATE menu_items SET stock_quantity = stock_quantity + %s, is_available = 1 "
                "WHERE item_id = %s", (int(item["qty"]), item["item_id"]))

    msg = ("Order cancelled. A refund is due - collect it at the canteen counter."
           if refund_status == "Pending" else "Order cancelled successfully.")
    return jsonify({"success": True, "message": msg})


@app.route("/api/order/<int:order_id>/refund-done", methods=["POST"])
def api_refund_done(order_id):
    """Kitchen confirms the refund has really been handed back (demo refunds are simulated)."""
    with db_cursor(commit=True) as (conn, cur):
        cur.execute("SELECT status, refund_status FROM orders WHERE order_id = %s FOR UPDATE", (order_id,))
        row = cur.fetchone()
        if not row:
            raise ApiError("Order not found.", 404)
        if row["status"] != "Cancelled" or row["refund_status"] != "Pending":
            raise ApiError("No refund is due for this order.", 409)
        cur.execute(
            "UPDATE orders SET refund_status = 'Refunded', refunded_at = NOW(), payment_status = 'Refunded' "
            "WHERE order_id = %s", (order_id,))
    return jsonify({"success": True, "message": "Refund recorded."})


@app.route("/api/order/<int:order_id>/feedback", methods=["POST"])
def api_submit_feedback(order_id):
    data = request.get_json(silent=True) or {}
    
    try:
        food = int(data.get("food_rating"))
        service = int(data.get("service_rating"))
        wait = int(data.get("wait_rating"))
    except (TypeError, ValueError):
        raise ApiError("Invalid ratings provided.")
        
    if not all(1 <= v <= 5 for v in (food, service, wait)):
        raise ApiError("Ratings must be between 1 and 5.")
    comment = str(data.get("comment") or "").strip()[:500] or None
    
    with db_cursor(commit=True) as (conn, cur):
        row = require_order_key(cur, order_id, str(data.get("key", "")))
        if row["status"] != "Completed":
            raise ApiError("Feedback can only be submitted for completed orders.", 400)
            
        try:
            # Save the star ratings and comment
            cur.execute("""
                INSERT INTO feedback (order_id, food_rating, service_rating, wait_rating, comment) 
                VALUES (%s, %s, %s, %s, %s)
            """, (order_id, food, service, wait, comment))
        except pymysql.err.IntegrityError:
            raise ApiError("Feedback already submitted for this order.", 400)
            
    return jsonify({"success": True, "message": "Feedback submitted successfully."})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)