-- =====================================================================
-- INF2003 Group Project - Team 37
-- 01b_create_user.sql : application database account (run as root)
--
-- Run AFTER 01_schema.sql (the grant refers to moviedb).
-- Replace CHANGE_ME with your own password, and put the same password
-- in your local .env file as MYSQL_PASSWORD. Do not commit a real one.
--
-- The account can only reach moviedb (the application) and
-- moviedb_bench (the benchmark scratch database created by
-- benchmark/ratings_scaling.py): least privilege for the back end.
-- =====================================================================

CREATE USER IF NOT EXISTS 'inf2003'@'localhost' IDENTIFIED BY 'CHANGE_ME';
GRANT ALL PRIVILEGES ON moviedb.*       TO 'inf2003'@'localhost';
GRANT ALL PRIVILEGES ON moviedb_bench.* TO 'inf2003'@'localhost';
FLUSH PRIVILEGES;

SHOW GRANTS FOR 'inf2003'@'localhost';
