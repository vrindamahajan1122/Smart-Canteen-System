"""
Smart Campus Canteen Pre-Ordering & Queue System
Flask + PyMySQL backend.

Run:  python app.py

Pages
  /            student menu + ordering   (this is what the QR code opens)
  /kitchen     kitchen dashboard + sold-out toggles
  /display     public order status board (voice + chime when a token is ready)
  /analytics   sales analytics dashboard
  /qr          printable QR code poster for the menu
"""
import os
import re
import socket
from collections import Counter
from decimal import Decimal

import pymysql
from pymysql.cursors import DictCursor
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Database configuration (override with environment variables if needed)
# ---------------------------------------------------------------------------
DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", "3306")),
    "user": os.environ.get("DB_USER", "root"),
    "password": os.environ.get("DB_PASSWORD", "system"),
    "database": os.environ.get("DB_NAME", "canteen_db"),
    "charset": "utf8mb4",
    "cursorclass": DictCursor,
    "autocommit": False,
}

PORT = int(os.environ.get("PORT", "5000"))
VALID_STATUSES = {"Preparing", "Ready", "Completed"}
MAX_QTY_PER_ITEM = 20

# Matches "2 x Veg Burger" inside "2 x Veg Burger, 1 x Cold Coffee"
ITEM_RE = re.compile(r"(\d+) x (.+?)(?:, (?=\d+ x )|$)")


def get_db_connection():
    """Return a new MySQL connection using DictCursor."""
    return pymysql.connect(**DB_CONFIG)


def serialize_order(row):
    """Convert a DB row into a JSON-safe dict."""
    created = row.get("created_at")
    return {
        "order_id": row["order_id"],
        "student_name": row["student_name"],
        "items_summary": row["items_summary"],
        "total_price": float(row["total_price"]),
        "pickup_time": row["pickup_time"],
        "status": row["status"],
        "created_at": created.strftime("%Y-%m-%d %H:%M:%S") if created else "",
    }


def get_lan_ip():
    """Best-effort guess of this machine's LAN IP (so phones can reach it)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packets are actually sent
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Student view
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    menu_items = []
    error = None
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT item_id, item_name, price FROM menu_items "
                "WHERE is_available = %s ORDER BY item_id ASC",
                (1,),
            )
            menu_items = cur.fetchall()
        for item in menu_items:
            item["price"] = float(item["price"])
    except pymysql.MySQLError as exc:
        error = "Could not load the menu. Please check the database connection. (%s)" % exc
    finally:
        if conn:
            conn.close()
    return render_template("index.html", menu_items=menu_items, error=error)


@app.route("/api/order", methods=["POST"])
def create_order():
    data = request.get_json(silent=True) or {}

    student_name = str(data.get("student_name", "")).strip()[:100]
    pickup_time = str(data.get("pickup_time", "")).strip()[:50]
    raw_items = data.get("items")

    if not student_name:
        return jsonify({"success": False, "error": "Student name is required."}), 400
    if not pickup_time:
        return jsonify({"success": False, "error": "Please select a pickup time slot."}), 400
    if not isinstance(raw_items, list) or not raw_items:
        return jsonify({"success": False, "error": "Your cart is empty."}), 400

    # Aggregate and validate quantities
    quantities = {}
    for entry in raw_items:
        try:
            item_id = int(entry.get("item_id"))
            qty = int(entry.get("quantity"))
        except (TypeError, ValueError, AttributeError):
            return jsonify({"success": False, "error": "Invalid item data."}), 400
        if qty < 1 or qty > MAX_QTY_PER_ITEM:
            return jsonify({"success": False, "error": "Invalid quantity for an item."}), 400
        quantities[item_id] = quantities.get(item_id, 0) + qty

    if any(q > MAX_QTY_PER_ITEM for q in quantities.values()):
        return jsonify({"success": False, "error": "Quantity limit exceeded."}), 400

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            ids = list(quantities.keys())
            placeholders = ",".join(["%s"] * len(ids))
            cur.execute(
                "SELECT item_id, item_name, price FROM menu_items "
                "WHERE is_available = 1 AND item_id IN (" + placeholders + ")",
                ids,
            )
            rows = {r["item_id"]: r for r in cur.fetchall()}

            if len(rows) != len(ids):
                conn.rollback()
                return jsonify({
                    "success": False,
                    "error": "One or more items are no longer available. Please refresh the page.",
                }), 400

            # Prices are always taken from the database, never from the client
            total = Decimal("0.00")
            parts = []
            for item_id in ids:
                row = rows[item_id]
                qty = quantities[item_id]
                total += Decimal(row["price"]) * qty
                parts.append("%d x %s" % (qty, row["item_name"]))
            summary = ", ".join(parts)

            cur.execute(
                "INSERT INTO orders (student_name, items_summary, total_price, pickup_time, status) "
                "VALUES (%s, %s, %s, %s, %s)",
                (student_name, summary, total, pickup_time, "Pending"),
            )
            order_id = cur.lastrowid
        conn.commit()
        return jsonify({
            "success": True,
            "token": order_id,
            "total_price": float(total),
            "items_summary": summary,
        }), 201
    except pymysql.MySQLError as exc:
        if conn:
            conn.rollback()
        return jsonify({"success": False, "error": "Database error: %s" % exc}), 500
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------
# Kitchen dashboard
# ---------------------------------------------------------------------------
@app.route("/kitchen")
def kitchen():
    return render_template("kitchen.html")


@app.route("/api/orders")
def api_orders():
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT order_id, student_name, items_summary, total_price, "
                "pickup_time, status, created_at "
                "FROM orders WHERE status <> %s ORDER BY created_at ASC, order_id ASC",
                ("Completed",),
            )
            rows = cur.fetchall()
        return jsonify({"success": True, "orders": [serialize_order(r) for r in rows]})
    except pymysql.MySQLError as exc:
        return jsonify({"success": False, "error": "Database error: %s" % exc, "orders": []}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/update_status", methods=["POST"])
def update_status():
    data = request.get_json(silent=True) or {}
    status = str(data.get("status", "")).strip()

    try:
        order_id = int(data.get("order_id"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Invalid order id."}), 400

    if status not in VALID_STATUSES:
        return jsonify({"success": False, "error": "Invalid status."}), 400

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT order_id FROM orders WHERE order_id = %s", (order_id,))
            if cur.fetchone() is None:
                return jsonify({"success": False, "error": "Order not found."}), 404
            cur.execute(
                "UPDATE orders SET status = %s WHERE order_id = %s",
                (status, order_id),
            )
        conn.commit()
        return jsonify({"success": True, "order_id": order_id, "status": status})
    except pymysql.MySQLError as exc:
        if conn:
            conn.rollback()
        return jsonify({"success": False, "error": "Database error: %s" % exc}), 500
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------
# Sold-out toggle (kitchen)
# ---------------------------------------------------------------------------
@app.route("/api/menu")
def api_menu():
    """All menu items (including sold-out ones) for the kitchen toggle panel."""
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT item_id, item_name, price, is_available "
                "FROM menu_items ORDER BY item_id ASC"
            )
            rows = cur.fetchall()
        items = [{
            "item_id": r["item_id"],
            "item_name": r["item_name"],
            "price": float(r["price"]),
            "is_available": bool(r["is_available"]),
        } for r in rows]
        return jsonify({"success": True, "items": items})
    except pymysql.MySQLError as exc:
        return jsonify({"success": False, "error": "Database error: %s" % exc, "items": []}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/toggle_item", methods=["POST"])
def toggle_item():
    data = request.get_json(silent=True) or {}

    try:
        item_id = int(data.get("item_id"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Invalid item id."}), 400

    raw_flag = data.get("is_available")
    if not isinstance(raw_flag, (bool, int)):
        return jsonify({"success": False, "error": "is_available must be true or false."}), 400
    flag = 1 if raw_flag else 0

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT item_id FROM menu_items WHERE item_id = %s", (item_id,))
            if cur.fetchone() is None:
                return jsonify({"success": False, "error": "Item not found."}), 404
            cur.execute(
                "UPDATE menu_items SET is_available = %s WHERE item_id = %s",
                (flag, item_id),
            )
        conn.commit()
        return jsonify({"success": True, "item_id": item_id, "is_available": bool(flag)})
    except pymysql.MySQLError as exc:
        if conn:
            conn.rollback()
        return jsonify({"success": False, "error": "Database error: %s" % exc}), 500
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------
# Public display board
# ---------------------------------------------------------------------------
def fetch_board_tokens():
    """Return (ready_tokens, preparing_tokens) as lists of order ids."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT order_id FROM orders WHERE status = %s ORDER BY created_at ASC, order_id ASC",
                ("Ready",),
            )
            ready = [r["order_id"] for r in cur.fetchall()]
            cur.execute(
                "SELECT order_id FROM orders WHERE status IN (%s, %s) ORDER BY created_at ASC, order_id ASC",
                ("Pending", "Preparing"),
            )
            preparing = [r["order_id"] for r in cur.fetchall()]
        return ready, preparing
    finally:
        conn.close()


@app.route("/display")
def display():
    try:
        ready, preparing = fetch_board_tokens()
    except pymysql.MySQLError:
        ready, preparing = [], []
    return render_template("display.html", ready=ready, preparing=preparing)


@app.route("/api/display")
def api_display():
    try:
        ready, preparing = fetch_board_tokens()
        return jsonify({"success": True, "ready": ready, "preparing": preparing})
    except pymysql.MySQLError as exc:
        return jsonify({"success": False, "error": "Database error: %s" % exc,
                        "ready": [], "preparing": []}), 500


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------
def slot_sort_key(label):
    """Sort '11:30 AM - 11:45 AM' style labels by their start time."""
    m = re.match(r"\s*(\d{1,2}):(\d{2})\s*(AM|PM)", label or "", re.I)
    if not m:
        return 9999
    h, mins, suffix = int(m.group(1)), int(m.group(2)), m.group(3).upper()
    h = h % 12 + (12 if suffix == "PM" else 0)
    return h * 60 + mins


@app.route("/analytics")
def analytics():
    return render_template("analytics.html")


@app.route("/api/analytics")
def api_analytics():
    rng = request.args.get("range", "today")
    where = "WHERE DATE(created_at) = CURDATE()" if rng == "today" else ""
    if rng != "today":
        rng = "all"

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(total_price), 0) AS revenue "
                "FROM orders " + where
            )
            totals = cur.fetchone()

            cur.execute(
                "SELECT HOUR(created_at) AS h, COUNT(*) AS n, "
                "COALESCE(SUM(total_price), 0) AS revenue "
                "FROM orders " + where + " GROUP BY HOUR(created_at) ORDER BY h"
            )
            hour_rows = {r["h"]: r for r in cur.fetchall()}

            cur.execute(
                "SELECT pickup_time, COUNT(*) AS n FROM orders " + where +
                " GROUP BY pickup_time"
            )
            slot_rows = cur.fetchall()

            cur.execute("SELECT status, COUNT(*) AS n FROM orders " + where + " GROUP BY status")
            status_rows = cur.fetchall()

            cur.execute("SELECT items_summary FROM orders " + where)
            summaries = [r["items_summary"] for r in cur.fetchall()]

        total_orders = int(totals["n"])
        total_revenue = float(totals["revenue"])

        # Hourly series, zero-filled across the canteen's working hours
        hours = list(hour_rows.keys()) + [10, 16]
        hourly = []
        for h in range(min(hours), max(hours) + 1):
            r = hour_rows.get(h)
            hourly.append({
                "hour": h,
                "orders": int(r["n"]) if r else 0,
                "revenue": float(r["revenue"]) if r else 0.0,
            })
        peak = max(hourly, key=lambda x: x["orders"]) if total_orders else None

        # Top items: parse "2 x Veg Burger, 1 x Cold Coffee"
        counter = Counter()
        for s in summaries:
            for qty, name in ITEM_RE.findall(s or ""):
                counter[name] += int(qty)
        top_items = [{"name": n, "qty": q} for n, q in counter.most_common(8)]

        slots = sorted(
            [{"slot": r["pickup_time"], "orders": int(r["n"])} for r in slot_rows],
            key=lambda x: slot_sort_key(x["slot"]),
        )

        status = {r["status"]: int(r["n"]) for r in status_rows}

        return jsonify({
            "success": True,
            "range": rng,
            "total_orders": total_orders,
            "total_revenue": total_revenue,
            "avg_order": (total_revenue / total_orders) if total_orders else 0.0,
            "peak_hour": peak["hour"] if peak and peak["orders"] else None,
            "hourly": hourly,
            "top_items": top_items,
            "slots": slots,
            "status": status,
        })
    except pymysql.MySQLError as exc:
        return jsonify({"success": False, "error": "Database error: %s" % exc}), 500
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------
# QR code poster for the menu
# ---------------------------------------------------------------------------
@app.route("/qr")
def qr_page():
    default_url = "http://%s:%d/" % (get_lan_ip(), PORT)
    return render_template("qr.html", default_url=default_url)


if __name__ == "__main__":
    print("\n  Student menu : http://%s:%d/   (QR poster at /qr)\n" % (get_lan_ip(), PORT))
    app.run(host="0.0.0.0", port=PORT, debug=False)
