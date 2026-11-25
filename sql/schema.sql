-- Select the intended database in MySQL Workbench before running this script.
CREATE TABLE IF NOT EXISTS customers (
    c_id INT PRIMARY KEY AUTO_INCREMENT,
    name VARCHAR(50),
    email VARCHAR(50),
    dob DATE,
    age INT,
    balance FLOAT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS customer_submissions (
    source VARCHAR(8) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    key_hash BINARY(32) NOT NULL,
    payload_hash BINARY(32) NOT NULL,
    customer_id INT DEFAULT NULL,
    PRIMARY KEY (source, key_hash)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS customer_rate_limits (
    scope_hash BINARY(32) NOT NULL,
    window_start BIGINT UNSIGNED NOT NULL,
    hits INT UNSIGNED NOT NULL DEFAULT 1,
    PRIMARY KEY (scope_hash, window_start),
    KEY expiry (window_start)
) ENGINE=InnoDB;
