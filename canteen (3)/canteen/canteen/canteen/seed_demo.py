"""
Fill the database with demo data so the analytics charts look alive.

    python seed_demo.py            # add extra menu items + ~80 completed orders for today
    python seed_demo.py --clear    # remove the demo orders again

Demo orders are marked with "(demo)" after the student name, and are saved as
Completed so they never show up on the kitchen dashboard or display board.
"""
import random
import sys
from datetime import datetime, timedelta

from app import get_db_connection

EXTRA_ITEMS = [
    ("Chole Bhature", 70.00),
    ("Veg Fried Rice", 80.00),
    ("Masala Chai", 15.00),
    ("Pasta", 75.00),
    ("Fresh Lime Soda", 30.00),
]

NAMES = ["Aarav", "Priya", "Rohan", "Simran", "Kabir", "Ananya", "Harpreet", "Neha",
         "Arjun", "Meera", "Gurpreet", "Ishaan", "Tanvi", "Vikram", "Riya", "Jashan"]

NUM_ORDERS = 80


def fmt_time(total_minutes):
    h, m = divmod(total_minutes, 60)
    suffix = "PM" if h >= 12 else "AM"
    h12 = 12 if h % 12 == 0 else h % 12
    return "%d:%02d %s" % (h12, m, suffix)


def clear():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM orders WHERE student_name LIKE %s", ("% (demo)",))
            print("Removed %d demo orders." % cur.rowcount)
        conn.commit()
    finally:
        conn.close()


def seed():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT IGNORE INTO menu_items (item_name, price, is_available) VALUES (%s, %s, 1)",
                EXTRA_ITEMS,
            )
            cur.execute("SELECT item_name, price FROM menu_items")
            menu = [(r["item_name"], r["price"]) for r in cur.fetchall()]

            # Slots every 15 min from 11:30 to 14:45, busiest around 1 PM
            starts = list(range(11 * 60 + 30, 15 * 60, 15))
            weights = [max(1, int(10 - abs(m - 13 * 60) / 15 * 1.5)) for m in starts]

            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            for _ in range(NUM_ORDERS):
                start = random.choices(starts, weights)[0]
                slot = "%s - %s" % (fmt_time(start), fmt_time(start + 15))
                created = today + timedelta(minutes=start - random.randint(5, 45))

                picks = random.sample(menu, random.randint(1, 3))
                parts, total = [], 0
                for name, price in picks:
                    qty = random.choices([1, 2, 3], [6, 3, 1])[0]
                    parts.append("%d x %s" % (qty, name))
                    total += price * qty

                cur.execute(
                    "INSERT INTO orders (student_name, items_summary, total_price, "
                    "pickup_time, status, created_at) VALUES (%s, %s, %s, %s, 'Completed', %s)",
                    ("%s (demo)" % random.choice(NAMES), ", ".join(parts), total, slot, created),
                )
        conn.commit()
        print("Added %d demo orders and %d extra menu items." % (NUM_ORDERS, len(EXTRA_ITEMS)))
    finally:
        conn.close()


if __name__ == "__main__":
    if "--clear" in sys.argv:
        clear()
    else:
        seed()
