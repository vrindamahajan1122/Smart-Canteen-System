-- Smart Campus Canteen Pre-Ordering & Queue System
-- Compatible with MySQL 5.7+ / MySQL 8 / MariaDB / phpMyAdmin (Import tab)

CREATE DATABASE IF NOT EXISTS canteen_db
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE canteen_db;

-- ---------------------------------------------------------
-- Table: menu_items
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS menu_items (
    item_id      INT UNSIGNED  NOT NULL AUTO_INCREMENT,
    item_name    VARCHAR(100)  NOT NULL,
    price        DECIMAL(8,2)  NOT NULL,
    is_available TINYINT(1)    NOT NULL DEFAULT 1,
    PRIMARY KEY (item_id),
    UNIQUE KEY uq_item_name (item_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------
-- Table: orders
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders (
    order_id       INT UNSIGNED  NOT NULL AUTO_INCREMENT,
    student_name   VARCHAR(100)  NOT NULL,
    items_summary  TEXT          NOT NULL,
    total_price    DECIMAL(10,2) NOT NULL,
    pickup_time    VARCHAR(50)   NOT NULL,
    status         ENUM('Pending','Preparing','Ready','Completed') NOT NULL DEFAULT 'Pending',
    created_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (order_id),
    KEY idx_status_created (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------
-- Sample data (INSERT IGNORE makes re-running this script safe)
-- ---------------------------------------------------------
INSERT IGNORE INTO menu_items (item_name, price, is_available) VALUES
    ('Veg Burger',      60.00, 1),
    ('Masala Dosa',     50.00, 1),
    ('Samosa (2 pcs)',  25.00, 1),
    ('Cold Coffee',     40.00, 1),
    ('Paneer Sandwich', 55.00, 1);
