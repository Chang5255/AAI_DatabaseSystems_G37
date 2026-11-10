-- =====================================================================
-- INF2003 Group Project - Team 37
-- 01b_create_user.sql : application database accounts (run as root)
--
-- Run AFTER 04_views.sql. A table-level GRANT fails with error 1146 if
-- the object does not exist yet, and section B grants SELECT on the six
-- views, so the views have to be there first. The order is therefore
--     01_schema -> 02_load -> 04_views -> 01b_create_user -> 07 -> 08
-- which is what setup_all.py does. Re-runnable.
--
-- Replace every CHANGE_ME_* with your own password and put the same
-- values in your local .env file. Never commit a real password.
--
-- WHY THREE ACCOUNTS (Lecture 1, stage S6 "Application and Security
-- Design"): a database account should be able to do exactly what its
-- job needs and nothing else. One all-powerful account means a bug or
-- an injected statement in the web app could drop the catalogue.
--
--   inf2003         the web application.
--                   Reads everything, but may only write the two tables
--                   that hold user-generated data (users, ratings).
--                   No DDL at all: it cannot CREATE, ALTER or DROP.
--   inf2003_report  reporting / marking account.
--                   SELECT on the six views ONLY, not on the base
--                   tables. This is the "users see data through a view"
--                   pattern from the lecture: the view decides what is
--                   visible, so the account cannot read raw rows.
--   inf2003_bench   the benchmark scripts.
--                   Full rights on the scratch database moviedb_bench
--                   (which benchmark/ratings_scaling.py creates and
--                   drops), and read-only on moviedb.
--
-- Section D at the bottom contains statements that MUST be refused.
-- Run those while connected AS the relevant account and screenshot the
-- errors for the report and the video.
-- =====================================================================

-- ---------------------------------------------------------------------
-- A. Application account: read all, write only user-generated data
-- ---------------------------------------------------------------------
CREATE USER IF NOT EXISTS 'inf2003'@'localhost' IDENTIFIED BY 'CHANGE_ME_APP';

-- Start from a clean slate so re-running this file cannot silently
-- leave an older, wider grant in place (an earlier version of this
-- project granted ALL PRIVILEGES here).
REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'inf2003'@'localhost';

-- Read-only on the whole application database: the catalogue, the
-- views and the audit table are all query-only for the app.
GRANT SELECT ON moviedb.* TO 'inf2003'@'localhost';

-- Write access, table by table, only where the application really
-- inserts, updates or deletes rows.
GRANT INSERT, UPDATE, DELETE ON moviedb.users   TO 'inf2003'@'localhost';
GRANT INSERT, UPDATE, DELETE ON moviedb.ratings TO 'inf2003'@'localhost';

-- Notice what is NOT granted:
--   * no CREATE / ALTER / DROP / INDEX  -> the app cannot change the schema
--   * no write access to movies, people, genres, movie_cast, movie_crew,
--     movie_genres                      -> the catalogue is immutable to the app
--   * no write access to rating_audit   -> only the triggers in
--     08_triggers.sql write audit rows, and they run with the
--     definer's rights, so the app cannot forge or erase an audit trail
--   * no FILE privilege                 -> the app cannot run LOAD DATA
--   * no GRANT OPTION                   -> the app cannot widen its own rights

-- ---------------------------------------------------------------------
-- B. Reporting account: the views only
-- ---------------------------------------------------------------------
CREATE USER IF NOT EXISTS 'inf2003_report'@'localhost' IDENTIFIED BY 'CHANGE_ME_REPORT';
REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'inf2003_report'@'localhost';

GRANT SELECT ON moviedb.v_movie_stats         TO 'inf2003_report'@'localhost';
GRANT SELECT ON moviedb.v_movie_cards         TO 'inf2003_report'@'localhost';
GRANT SELECT ON moviedb.v_top_rated_per_genre TO 'inf2003_report'@'localhost';
GRANT SELECT ON moviedb.v_genre_year_stats    TO 'inf2003_report'@'localhost';
GRANT SELECT ON moviedb.v_director_stats      TO 'inf2003_report'@'localhost';
GRANT SELECT ON moviedb.v_user_activity       TO 'inf2003_report'@'localhost';

-- The views are created by root (04_views.sql), so they run with
-- DEFINER rights: this account can read through them even though it
-- has no privilege on the underlying tables. That is exactly the point.

-- ---------------------------------------------------------------------
-- C. Benchmark account: owns the scratch database, reads the real one
-- ---------------------------------------------------------------------
CREATE USER IF NOT EXISTS 'inf2003_bench'@'localhost' IDENTIFIED BY 'CHANGE_ME_BENCH';
REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'inf2003_bench'@'localhost';

-- benchmark/ratings_scaling.py creates moviedb_bench, fills it with
-- synthetic ratings, builds indexes on it and drops it again, so it
-- needs DDL rights there. The database does not exist yet, which is
-- fine: MySQL can grant on a name before the object exists.
GRANT ALL PRIVILEGES ON moviedb_bench.* TO 'inf2003_bench'@'localhost';

-- It also reads the real catalogue to draw realistic film ids, and
-- benchmark/txn_cost_bench.py writes into its own throw-away user.
GRANT SELECT ON moviedb.* TO 'inf2003_bench'@'localhost';
GRANT INSERT, UPDATE, DELETE ON moviedb.users   TO 'inf2003_bench'@'localhost';
GRANT INSERT, UPDATE, DELETE ON moviedb.ratings TO 'inf2003_bench'@'localhost';

-- One more, and the reason the benchmarks need an account of their own:
-- Experiment 1 task T4 measures "rename one person" on both databases,
-- so it UPDATEs people.name and immediately puts the old name back. The
-- application account is refused that statement (error 1142), which is
-- the privilege design doing its job - the catalogue is not the app's to
-- edit. Widening the app account to make the benchmark run would have
-- thrown that away; a second account keeps both properties.
GRANT UPDATE ON moviedb.people TO 'inf2003_bench'@'localhost';

FLUSH PRIVILEGES;

-- ---------------------------------------------------------------------
-- Proof: what each account may do
-- ---------------------------------------------------------------------
SHOW GRANTS FOR 'inf2003'@'localhost';
SHOW GRANTS FOR 'inf2003_report'@'localhost';
SHOW GRANTS FOR 'inf2003_bench'@'localhost';

-- The same thing from the data dictionary, as one table for the report.
SELECT grantee, table_schema, table_name, GROUP_CONCAT(privilege_type
         ORDER BY privilege_type SEPARATOR ', ') AS privileges
  FROM information_schema.table_privileges
 WHERE grantee LIKE '\'inf2003%'
 GROUP BY grantee, table_schema, table_name
 ORDER BY grantee, table_name;

SELECT grantee, table_schema, GROUP_CONCAT(privilege_type
         ORDER BY privilege_type SEPARATOR ', ') AS schema_wide_privileges
  FROM information_schema.schema_privileges
 WHERE grantee LIKE '\'inf2003%'
 GROUP BY grantee, table_schema
 ORDER BY grantee, table_schema;

-- ---------------------------------------------------------------------
-- D. DELIBERATE DENIALS - each statement below MUST be refused.
--    Open a NEW Workbench connection as the named account, run the
--    statement, and screenshot the error. Run them one at a time.
-- ---------------------------------------------------------------------

-- As 'inf2003' (the web application):
--   D1. Schema change blocked -> Error 1142 (DROP command denied)
-- DROP TABLE moviedb.ratings;
--
--   D2. Catalogue is read-only to the app -> Error 1142 (DELETE denied)
-- DELETE FROM moviedb.movies WHERE movie_id = 19995;
--
--   D3. Audit trail cannot be tampered with -> Error 1142 (DELETE denied)
-- DELETE FROM moviedb.rating_audit;
--
--   D4. Cannot widen its own rights -> Error 1142 / 1045
-- GRANT ALL PRIVILEGES ON moviedb.* TO 'inf2003'@'localhost';
--
--   D5. Cannot read another database -> Error 1142 / 1044
-- SELECT COUNT(*) FROM mysql.user;

-- As 'inf2003_report' (reporting):
--   D6. Base tables are invisible -> Error 1142 (SELECT denied)
-- SELECT * FROM moviedb.ratings LIMIT 1;
--
--   D7. But the view works, because the view's definer may read them:
-- SELECT title, weighted_score FROM moviedb.v_movie_stats
--  ORDER BY weighted_score DESC LIMIT 5;
--
--   D8. Read-only: no writes even through a view -> Error 1142
-- UPDATE moviedb.v_user_activity SET display_name = 'x' WHERE user_id = 1;

-- ---------------------------------------------------------------------
-- Clean-up (only if you want to start over)
-- ---------------------------------------------------------------------
-- DROP USER IF EXISTS 'inf2003'@'localhost',
--                     'inf2003_report'@'localhost',
--                     'inf2003_bench'@'localhost';
