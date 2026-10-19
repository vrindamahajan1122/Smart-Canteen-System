CREATE DATABASE IF NOT EXISTS canteen_db
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE canteen_db;

-- 1) Core tables (created only if they do not exist yet)
CREATE TABLE IF NOT EXISTS menu_items (
    item_id        INT UNSIGNED  NOT NULL AUTO_INCREMENT,
    item_name      VARCHAR(100)  NOT NULL,
    price          DECIMAL(8,2)  NOT NULL,
    stock_quantity INT           NOT NULL DEFAULT 50,
    is_available   TINYINT(1)    NOT NULL DEFAULT 1,
    PRIMARY KEY (item_id),
    UNIQUE KEY uq_item_name (item_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS pickup_slots (
    slot_id      INT UNSIGNED NOT NULL AUTO_INCREMENT,
    slot_start   TIME         NOT NULL,
    slot_end     TIME         NOT NULL,
    max_capacity INT          NULL DEFAULT NULL,
    PRIMARY KEY (slot_id),
    UNIQUE KEY uq_slot_start (slot_start)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS orders (
    order_id            INT UNSIGNED  NOT NULL AUTO_INCREMENT,
    student_name        VARCHAR(100)  NOT NULL,
    total_price         DECIMAL(10,2) NOT NULL,
    slot_id             INT UNSIGNED  NOT NULL,
    pickup_time         VARCHAR(50)   NOT NULL,
    status              ENUM('Pending','Preparing','Ready','Completed','Cancelled') NOT NULL DEFAULT 'Pending',
    created_at          DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    preparing_at        DATETIME      NULL DEFAULT NULL,
    ready_at            DATETIME      NULL DEFAULT NULL,
    completed_at        DATETIME      NULL DEFAULT NULL,
    payment_method      ENUM('Cash','Online') NOT NULL DEFAULT 'Cash',
    payment_status      ENUM('Pending','Paid','Failed','Refunded') NOT NULL DEFAULT 'Pending',
    paid_at             DATETIME      NULL DEFAULT NULL,
    cancelled_at        DATETIME      NULL DEFAULT NULL,
    cancellation_reason VARCHAR(255)  NULL DEFAULT NULL,
    refund_status       ENUM('None','Pending','Refunded') NOT NULL DEFAULT 'None',
    refunded_at         DATETIME      NULL DEFAULT NULL,
    tracking_token      VARCHAR(64)   NULL DEFAULT NULL,
    PRIMARY KEY (order_id),
    KEY idx_orders_created (created_at),
    KEY idx_orders_status_created (status, created_at),
    KEY idx_orders_slot_created (slot_id, created_at),
    CONSTRAINT fk_orders_slot FOREIGN KEY (slot_id) REFERENCES pickup_slots (slot_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS order_items (
    order_item_id        INT UNSIGNED  NOT NULL AUTO_INCREMENT,
    order_id             INT UNSIGNED  NOT NULL,
    item_id              INT UNSIGNED  NOT NULL,
    quantity             INT           NOT NULL,
    price                DECIMAL(8,2)  NOT NULL,
    customization_cost   DECIMAL(8,2)  NOT NULL DEFAULT 0.00,
    special_instructions VARCHAR(200)  NULL DEFAULT NULL,
    PRIMARY KEY (order_item_id),
    KEY idx_oi_order (order_id),
    KEY idx_oi_item (item_id),
    CONSTRAINT fk_oi_order FOREIGN KEY (order_id) REFERENCES orders (order_id) ON DELETE CASCADE,
    CONSTRAINT fk_oi_item  FOREIGN KEY (item_id)  REFERENCES menu_items (item_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2) Add missing columns to tables that already existed (old versions)
SET @s := (SELECT IF(COUNT(*) = 0,
  "ALTER TABLE orders ADD COLUMN payment_method ENUM('Cash','Online') NOT NULL DEFAULT 'Cash'", "SELECT 1")
  FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = 'canteen_db' AND TABLE_NAME = 'orders' AND COLUMN_NAME = 'payment_method');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

SET @s := (SELECT IF(COUNT(*) = 0,
  "ALTER TABLE orders ADD COLUMN payment_status ENUM('Pending','Paid','Failed','Refunded') NOT NULL DEFAULT 'Pending'", "SELECT 1")
  FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = 'canteen_db' AND TABLE_NAME = 'orders' AND COLUMN_NAME = 'payment_status');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

SET @s := (SELECT IF(COUNT(*) = 0,
  "ALTER TABLE orders ADD COLUMN paid_at DATETIME NULL DEFAULT NULL", "SELECT 1")
  FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = 'canteen_db' AND TABLE_NAME = 'orders' AND COLUMN_NAME = 'paid_at');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

SET @s := (SELECT IF(COUNT(*) = 0,
  "ALTER TABLE orders ADD COLUMN cancelled_at DATETIME NULL DEFAULT NULL", "SELECT 1")
  FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = 'canteen_db' AND TABLE_NAME = 'orders' AND COLUMN_NAME = 'cancelled_at');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

SET @s := (SELECT IF(COUNT(*) = 0,
  "ALTER TABLE orders ADD COLUMN cancellation_reason VARCHAR(255) NULL DEFAULT NULL", "SELECT 1")
  FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = 'canteen_db' AND TABLE_NAME = 'orders' AND COLUMN_NAME = 'cancellation_reason');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

SET @s := (SELECT IF(COUNT(*) = 0,
  "ALTER TABLE orders ADD COLUMN refund_status ENUM('None','Pending','Refunded') NOT NULL DEFAULT 'None'", "SELECT 1")
  FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = 'canteen_db' AND TABLE_NAME = 'orders' AND COLUMN_NAME = 'refund_status');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

SET @s := (SELECT IF(COUNT(*) = 0,
  "ALTER TABLE orders ADD COLUMN refunded_at DATETIME NULL DEFAULT NULL", "SELECT 1")
  FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = 'canteen_db' AND TABLE_NAME = 'orders' AND COLUMN_NAME = 'refunded_at');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

SET @s := (SELECT IF(COUNT(*) = 0,
  "ALTER TABLE orders ADD COLUMN tracking_token VARCHAR(64) NULL DEFAULT NULL", "SELECT 1")
  FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = 'canteen_db' AND TABLE_NAME = 'orders' AND COLUMN_NAME = 'tracking_token');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

SET @s := (SELECT IF(COUNT(*) = 0,
  "ALTER TABLE order_items ADD COLUMN customization_cost DECIMAL(8,2) NOT NULL DEFAULT 0.00", "SELECT 1")
  FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = 'canteen_db' AND TABLE_NAME = 'order_items' AND COLUMN_NAME = 'customization_cost');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

SET @s := (SELECT IF(COUNT(*) = 0,
  "ALTER TABLE order_items ADD COLUMN special_instructions VARCHAR(200) NULL DEFAULT NULL", "SELECT 1")
  FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = 'canteen_db' AND TABLE_NAME = 'order_items' AND COLUMN_NAME = 'special_instructions');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

-- Make sure the status lists include the new values (harmless if already present)
ALTER TABLE orders MODIFY COLUMN status
  ENUM('Pending','Preparing','Ready','Completed','Cancelled') NOT NULL DEFAULT 'Pending';
ALTER TABLE orders MODIFY COLUMN payment_status
  ENUM('Pending','Paid','Failed','Refunded') NOT NULL DEFAULT 'Pending';

-- 3) Customization tables (option_group: options in one group exclude each other)
CREATE TABLE IF NOT EXISTS customization_options (
    option_id    INT UNSIGNED NOT NULL AUTO_INCREMENT,
    option_name  VARCHAR(60)  NOT NULL,
    extra_price  DECIMAL(6,2) NOT NULL DEFAULT 0.00,
    option_group VARCHAR(30)  NULL DEFAULT NULL,
    is_available TINYINT(1)   NOT NULL DEFAULT 1,
    PRIMARY KEY (option_id),
    UNIQUE KEY uq_option_name (option_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS menu_item_options (
    item_id   INT UNSIGNED NOT NULL,
    option_id INT UNSIGNED NOT NULL,
    PRIMARY KEY (item_id, option_id),
    CONSTRAINT fk_mio_item   FOREIGN KEY (item_id)   REFERENCES menu_items (item_id) ON DELETE CASCADE,
    CONSTRAINT fk_mio_option FOREIGN KEY (option_id) REFERENCES customization_options (option_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS order_item_options (
    order_item_id INT UNSIGNED NOT NULL,
    option_id     INT UNSIGNED NOT NULL,
    option_name   VARCHAR(60)  NOT NULL,
    extra_price   DECIMAL(6,2) NOT NULL DEFAULT 0.00,
    PRIMARY KEY (order_item_id, option_id),
    KEY idx_oio_name (option_name),
    CONSTRAINT fk_oio_line   FOREIGN KEY (order_item_id) REFERENCES order_items (order_item_id) ON DELETE CASCADE,
    CONSTRAINT fk_oio_option FOREIGN KEY (option_id)     REFERENCES customization_options (option_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 4) Feedback: one row per order, enforced by the UNIQUE key
CREATE TABLE IF NOT EXISTS feedback (
    feedback_id    INT UNSIGNED NOT NULL AUTO_INCREMENT,
    order_id       INT UNSIGNED NOT NULL,
    food_rating    TINYINT      NOT NULL,
    service_rating TINYINT      NOT NULL,
    wait_rating    TINYINT      NOT NULL,
    comment        VARCHAR(500) NULL DEFAULT NULL,
    created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (feedback_id),
    UNIQUE KEY uq_feedback_order (order_id),
    KEY idx_feedback_created (created_at),
    CONSTRAINT fk_feedback_order FOREIGN KEY (order_id) REFERENCES orders (order_id) ON DELETE CASCADE,
    CONSTRAINT chk_food_rating    CHECK (food_rating    BETWEEN 1 AND 5),
    CONSTRAINT chk_service_rating CHECK (service_rating BETWEEN 1 AND 5),
    CONSTRAINT chk_wait_rating    CHECK (wait_rating    BETWEEN 1 AND 5)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 5) Sample data (INSERT IGNORE: existing rows and stock are never overwritten)
INSERT IGNORE INTO menu_items (item_name, price, stock_quantity, is_available) VALUES
    ('Veg Burger',      60.00, 50, 1),
    ('Masala Dosa',     50.00, 50, 1),
    ('Samosa (2 pcs)',  25.00, 50, 1),
    ('Cold Coffee',     40.00, 50, 1),
    ('Paneer Sandwich', 55.00, 50, 1);

INSERT IGNORE INTO pickup_slots (slot_start, slot_end) VALUES
    ('11:30:00', '11:45:00'), ('11:45:00', '12:00:00'),
    ('12:00:00', '12:15:00'), ('12:15:00', '12:30:00'),
    ('12:30:00', '12:45:00'), ('12:45:00', '13:00:00'),
    ('13:00:00', '13:15:00'), ('13:15:00', '13:30:00'),
    ('13:30:00', '13:45:00'), ('13:45:00', '14:00:00'),
    ('14:00:00', '14:15:00'), ('14:15:00', '14:30:00'),
    ('14:30:00', '14:45:00'), ('14:45:00', '15:00:00');

INSERT IGNORE INTO customization_options (option_name, extra_price, option_group) VALUES
    ('No onion',     0.00,  NULL),
    ('No tomato',    0.00,  NULL),
    ('Less spicy',   0.00,  NULL),
    ('Extra cheese', 10.00, NULL),
    ('Extra chutney', 5.00, NULL),
    ('No butter',    0.00,  NULL),
    ('Less sugar',   0.00,  'sugar'),
    ('No sugar',     0.00,  'sugar'),
    ('Extra ice',    0.00,  NULL);

-- Only options the kitchen can really do for each dish
INSERT IGNORE INTO menu_item_options (item_id, option_id)
SELECT m.item_id, o.option_id
FROM menu_items m
JOIN customization_options o
WHERE (m.item_name, o.option_name) IN (
    ('Veg Burger',      'No onion'),
    ('Veg Burger',      'No tomato'),
    ('Veg Burger',      'Less spicy'),
    ('Veg Burger',      'Extra cheese'),
    ('Masala Dosa',     'No onion'),
    ('Masala Dosa',     'Less spicy'),
    ('Masala Dosa',     'Extra chutney'),
    ('Masala Dosa',     'No butter'),
    ('Samosa (2 pcs)',  'Extra chutney'),
    ('Cold Coffee',     'Less sugar'),
    ('Cold Coffee',     'No sugar'),
    ('Cold Coffee',     'Extra ice'),
    ('Paneer Sandwich', 'No onion'),
    ('Paneer Sandwich', 'No tomato'),
    ('Paneer Sandwich', 'Less spicy'),
    ('Paneer Sandwich', 'Extra cheese')
);

-- 6) Quick check: every number below should be greater than 0
SELECT
    (SELECT COUNT(*) FROM menu_items)            AS menu_items,
    (SELECT COUNT(*) FROM pickup_slots)          AS slots,
    (SELECT COUNT(*) FROM customization_options) AS custom_options,
    (SELECT COUNT(*) FROM menu_item_options)     AS dish_option_links,
    (SELECT COUNT(*) FROM information_schema.COLUMNS
       WHERE TABLE_SCHEMA = 'canteen_db' AND TABLE_NAME = 'orders'
         AND COLUMN_NAME IN ('paid_at','cancelled_at','refund_status','tracking_token')) AS new_order_columns;