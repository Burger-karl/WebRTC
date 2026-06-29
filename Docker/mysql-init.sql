-- docker/mysql-init.sql
-- Runs automatically on first MySQL container start.
-- Ensures the meetfree database and user exist with correct permissions.

CREATE DATABASE IF NOT EXISTS meetfree
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

-- Create user if not exists (safe for re-runs)
CREATE USER IF NOT EXISTS 'meetfree'@'%'
  IDENTIFIED BY 'meetfree_local_pass';

-- Grant all privileges on the meetfree database
GRANT ALL PRIVILEGES ON meetfree.* TO 'meetfree'@'%';

FLUSH PRIVILEGES;