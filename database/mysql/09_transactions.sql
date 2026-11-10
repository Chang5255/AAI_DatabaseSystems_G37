-- =====================================================================
-- INF2003 Group Project - Team 37
-- 09_transactions.sql : transactions, isolation and locking (Lecture 7)
--
-- Run AFTER 02_load.sql. 08_triggers.sql should be run first so the
-- audit trail in section C is visible, but it is not required.
--
-- Safe to re-run. Every section either ends in ROLLBACK or works on a
-- throw-away user (txn_demo_g37) that is deleted at the end, so the
-- loaded data is identical before and after.
--
-- CONTENTS
--   A. Where we start: autocommit and the default isolation level
--   B. Atomicity: a two-statement change that must not half-happen
--   C. SAVEPOINT: a batch import that keeps the good rows
--   D. Isolation: the same query, two isolation levels, two answers
--   E. Locking and deadlock: two sessions fighting over two rows
--   F. Optimistic concurrency without locks (the 3-phase idea)
--
-- Sections D and E need TWO connections. In Workbench: Database ->
-- New Connection (or open the same connection twice) and keep both
-- tabs side by side. Blocks are labelled SESSION 1 / SESSION 2.
-- =====================================================================

USE moviedb;

-- =====================================================================
-- A. Where we start
--
-- MySQL runs with autocommit ON: every single statement is its own
-- transaction and is committed the moment it finishes. That is why
-- 05_crud.sql needs no COMMIT. It also means that a sequence of
-- statements is NOT atomic unless we say so with START TRANSACTION.
--
-- Our Flask app does the same thing in Python: webapp/db.py normally
-- connects with autocommit=True, and only the operations that must be
-- all-or-nothing go through its mysql_transaction() helper, which
-- turns autocommit off for the duration.
-- =====================================================================
SELECT @@autocommit                AS autocommit,
       @@transaction_isolation     AS isolation_level,
       @@innodb_lock_wait_timeout  AS lock_wait_timeout_seconds,
       @@innodb_deadlock_detect    AS deadlock_detection;

-- InnoDB's default is REPEATABLE READ. The four levels, weakest first:
--   READ UNCOMMITTED  dirty reads allowed (InnoDB: rarely useful)
--   READ COMMITTED    no dirty reads; the same query can still return
--                     different rows later in the same transaction
--   REPEATABLE READ   a transaction sees one consistent snapshot
--                     (MySQL default; section D proves it)
--   SERIALIZABLE      as if transactions ran one after another

-- =====================================================================
-- B. Atomicity
--
-- The lecture's example is a bank transfer: take 100 from one account,
-- add 100 to another, and never do only one half. Our equivalent:
-- "merge two accounts", i.e. move every rating from an old profile to
-- a new one. It is two statements, and stopping in the middle would
-- either duplicate or lose a user's ratings.
--
-- Nothing below is kept: the whole section ends in ROLLBACK.
-- =====================================================================
START TRANSACTION;

-- Two real users: the one with the most ratings, and another one.
SET @from_user = (SELECT user_id FROM ratings GROUP BY user_id ORDER BY COUNT(*) DESC, user_id LIMIT 1);
SET @to_user   = (SELECT user_id FROM users WHERE user_id <> @from_user ORDER BY user_id LIMIT 1);

SELECT @from_user AS moving_from, @to_user AS moving_to,
       (SELECT COUNT(*) FROM ratings WHERE user_id = @from_user) AS from_ratings,
       (SELECT COUNT(*) FROM ratings WHERE user_id = @to_user)   AS to_ratings;

-- Step 1: films the target user has NOT rated can simply be reassigned.
UPDATE ratings
   SET user_id = @to_user
 WHERE user_id = @from_user
   AND movie_id NOT IN (SELECT movie_id FROM (SELECT movie_id FROM ratings WHERE user_id = @to_user) t);

-- Step 2: the rest would collide with the composite primary key
-- (user_id, movie_id), so they are dropped.
DELETE FROM ratings WHERE user_id = @from_user;

SELECT (SELECT COUNT(*) FROM ratings WHERE user_id = @from_user) AS from_ratings_after,  -- 0
       (SELECT COUNT(*) FROM ratings WHERE user_id = @to_user)   AS to_ratings_after;

-- Imagine the server dying between step 1 and step 2. With autocommit
-- the ratings would already have moved and the old profile would be
-- half-empty. Inside a transaction, nothing is visible to anyone else
-- until COMMIT, and a crash rolls the whole thing back (durability is
-- provided by the redo log, which is written before the commit
-- returns).
ROLLBACK;

SELECT 'B: rolled back' AS section,
       (SELECT COUNT(*) FROM ratings WHERE user_id = @from_user) AS from_ratings_restored;

-- =====================================================================
-- C. SAVEPOINT: a batch import that keeps the good rows
--
-- The application lets a user paste a list of "movie_id, rating" lines
-- (webapp/routes.py -> /import-ratings). One bad line must not throw
-- away the whole list. A SAVEPOINT before each line, and ROLLBACK TO
-- SAVEPOINT when that line fails, gives per-row error handling inside
-- one transaction: the good rows are still committed together.
--
-- This section COMMITS, then cleans up after itself, so that the audit
-- trail from 08_triggers.sql is visible in between.
-- =====================================================================
DELETE FROM users WHERE username = 'txn_demo_g37';   -- leftover from an interrupted run
INSERT INTO users (username, display_name) VALUES ('txn_demo_g37', 'Transaction demo');
SET @demo = LAST_INSERT_ID();

START TRANSACTION;

-- line 1: valid
SAVEPOINT line1;
INSERT INTO ratings (user_id, movie_id, rating_value) VALUES (@demo, 19995, 4.5);

-- line 2: valid
SAVEPOINT line2;
INSERT INTO ratings (user_id, movie_id, rating_value) VALUES (@demo, 155, 5.0);

-- line 3: INVALID (3.7 is not a half-star step -> CHECK chk_ratings_step).
-- Run the next two lines TOGETHER: the INSERT fails with error 3819 and
-- the ROLLBACK TO SAVEPOINT undoes only that line.
SAVEPOINT line3;
-- INSERT INTO ratings (user_id, movie_id, rating_value) VALUES (@demo, 27205, 3.7);
ROLLBACK TO SAVEPOINT line3;

-- line 4: INVALID (film 999999999 does not exist -> FOREIGN KEY, error 1452)
SAVEPOINT line4;
-- INSERT INTO ratings (user_id, movie_id, rating_value) VALUES (@demo, 999999999, 4.0);
ROLLBACK TO SAVEPOINT line4;

-- line 5: valid
SAVEPOINT line5;
INSERT INTO ratings (user_id, movie_id, rating_value) VALUES (@demo, 603, 3.5);

COMMIT;

-- Expect 3 rows: the two bad lines were undone, the three good ones
-- were committed as one unit.
SELECT movie_id, rating_value FROM ratings WHERE user_id = @demo ORDER BY movie_id;

-- And the trail shows exactly those three inserts (08_triggers.sql).
SELECT action, movie_id, old_value, new_value, changed_by
  FROM rating_audit WHERE user_id = @demo ORDER BY audit_id;

-- Note: ROLLBACK TO SAVEPOINT releases the locks taken after the
-- savepoint but does NOT end the transaction, so the rows from lines
-- 1, 2 and 5 are still uncommitted and still locked at that moment.

-- Clean up: the user, their ratings (ON DELETE CASCADE) and the audit
-- rows the triggers wrote. Deleting the audit rows needs an account
-- with DELETE on rating_audit, i.e. root - the application account
-- deliberately cannot do this (01b_create_user.sql).
DELETE FROM users        WHERE user_id = @demo;
DELETE FROM rating_audit WHERE user_id = @demo;

SELECT 'C: cleaned up' AS section,
       (SELECT COUNT(*) FROM users WHERE username = 'txn_demo_g37') AS demo_users_left;

-- =====================================================================
-- D. Isolation: two sessions, one query, two answers
--
-- Shows why the isolation level matters. Run the blocks in the order
-- of the step numbers, swapping between the two Workbench tabs.
-- =====================================================================

-- ---- D1. REPEATABLE READ (the default): a stable snapshot ----------
--
-- SESSION 1, step 1
--   USE moviedb;
--   SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ;
--   START TRANSACTION;
--   SELECT COUNT(*) AS c1 FROM ratings WHERE movie_id = 19995;
--
-- SESSION 2, step 2  (autocommit, so this is committed immediately)
--   USE moviedb;
--   INSERT INTO ratings (user_id, movie_id, rating_value)
--   SELECT u.user_id, 19995, 4.0 FROM users u
--    WHERE NOT EXISTS (SELECT 1 FROM ratings r
--                       WHERE r.user_id = u.user_id AND r.movie_id = 19995)
--    LIMIT 1;
--
-- SESSION 1, step 3
--   SELECT COUNT(*) AS c2 FROM ratings WHERE movie_id = 19995;   -- SAME as c1
--   COMMIT;
--   SELECT COUNT(*) AS c3 FROM ratings WHERE movie_id = 19995;   -- now +1
--
-- Session 1 keeps reading the snapshot taken by its first SELECT, so
-- c1 = c2 even though the row is already committed elsewhere. This is
-- the "I" in ACID: session 1's result does not change under its feet.
--
-- ---- D2. READ COMMITTED: the same test now shifts ------------------
--
-- SESSION 1, step 1
--   SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED;
--   START TRANSACTION;
--   SELECT COUNT(*) AS c1 FROM ratings WHERE movie_id = 19995;
--
-- SESSION 2, step 2: insert another row (same statement as above)
--
-- SESSION 1, step 3
--   SELECT COUNT(*) AS c2 FROM ratings WHERE movie_id = 19995;   -- c2 = c1 + 1
--   COMMIT;
--   SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ;     -- restore
--
-- A non-repeatable read. Nothing is corrupt - this is the documented
-- behaviour of READ COMMITTED - but a report that read a count, then
-- read the rows, could print a total that does not match its own list.
--
-- Clean-up for both runs (SESSION 2):
--   DELETE FROM ratings WHERE movie_id = 19995 AND rating_value = 4.0
--     AND rated_at >= NOW() - INTERVAL 10 MINUTE;
--   DELETE FROM rating_audit WHERE movie_id = 19995
--     AND changed_at >= NOW() - INTERVAL 10 MINUTE;

-- =====================================================================
-- E. Locking and deadlock
--
-- A write takes a row lock that is held until the transaction ends.
-- Two transactions that lock the same two rows in opposite orders wait
-- for each other forever; InnoDB notices and kills one of them.
-- =====================================================================

-- ---- E1. A plain lock wait ----------------------------------------
--
-- SESSION 1
--   START TRANSACTION;
--   UPDATE movies SET popularity = popularity WHERE movie_id = 19995;  -- locks the row
--
-- SESSION 2
--   START TRANSACTION;
--   UPDATE movies SET popularity = popularity WHERE movie_id = 19995;  -- hangs
--
-- SESSION 3 (or session 1): who is waiting for whom
--   SELECT waiting_pid, waiting_query, blocking_pid, blocking_query, wait_age
--     FROM sys.innodb_lock_waits;
--   SELECT trx_id, trx_state, trx_started, trx_rows_locked, trx_query
--     FROM information_schema.innodb_trx;
--
-- SESSION 1
--   ROLLBACK;        -- session 2 immediately proceeds
-- SESSION 2
--   ROLLBACK;
--
-- Left alone, session 2 fails after @@innodb_lock_wait_timeout
-- seconds (50 by default) with error 1205 "Lock wait timeout exceeded".

-- ---- E2. A real deadlock ------------------------------------------
--
-- SESSION 1, step 1
--   START TRANSACTION;
--   UPDATE movies SET popularity = popularity WHERE movie_id = 19995;
--
-- SESSION 2, step 2
--   START TRANSACTION;
--   UPDATE movies SET popularity = popularity WHERE movie_id = 155;
--
-- SESSION 1, step 3  (wants the row session 2 holds -> waits)
--   UPDATE movies SET popularity = popularity WHERE movie_id = 155;
--
-- SESSION 2, step 4  (wants the row session 1 holds -> cycle)
--   UPDATE movies SET popularity = popularity WHERE movie_id = 19995;
--
-- One session now fails instantly with
--   Error 1213: Deadlock found when trying to get lock; try restarting transaction
-- InnoDB detects the cycle and rolls back the cheaper transaction; the
-- other one completes. Both sessions should then ROLLBACK.
--
-- The post-mortem, for a screenshot:
--   SHOW ENGINE INNODB STATUS;      -- see the LATEST DETECTED DEADLOCK block
--   SELECT * FROM performance_schema.events_errors_summary_global_by_error
--    WHERE error_number IN (1213, 1205);
--
-- Prevention (lecture 7): always take locks in the same order, keep
-- transactions short, and be ready to retry on 1213 - a deadlock is a
-- normal event in a busy system, not a bug to be eliminated.

-- =====================================================================
-- F. Optimistic concurrency: no locks at all
--
-- Lecture 7's 3-phase method: read a private copy, validate that
-- nobody changed the row in the meantime, then write. Our ratings
-- table already carries a version marker: updated_at.
--
-- The validation lives in the WHERE clause, so it is checked by the
-- server atomically: if another session wrote the row first, the
-- UPDATE matches 0 rows and the application knows to re-read and retry
-- instead of silently overwriting someone's work (a "lost update").
-- =====================================================================
DELETE FROM users WHERE username = 'txn_demo_g37';
INSERT INTO users (username, display_name) VALUES ('txn_demo_g37', 'Transaction demo');
SET @demo = LAST_INSERT_ID();
INSERT INTO ratings (user_id, movie_id, rating_value) VALUES (@demo, 19995, 3.0);

-- Phase 1, READ: the client holds the row and its version.
SELECT rating_value, updated_at INTO @seen_value, @seen_version
  FROM ratings WHERE user_id = @demo AND movie_id = 19995;
SELECT @seen_value AS value_i_read, @seen_version AS version_i_read;

-- Someone else changes the row before we write (this is the race).
UPDATE ratings SET rating_value = 5.0 WHERE user_id = @demo AND movie_id = 19995;

-- Phases 2 and 3, VALIDATE + WRITE in one statement.
UPDATE ratings
   SET rating_value = 4.0
 WHERE user_id = @demo AND movie_id = 19995
   AND (updated_at <=> @seen_version);     -- <=> is NULL-safe equality

SELECT ROW_COUNT() AS rows_updated;        -- 0 -> our write was refused
SELECT rating_value AS value_now FROM ratings WHERE user_id = @demo AND movie_id = 19995;  -- 5.0

-- Retry: read the new version, then write again.
SELECT updated_at INTO @seen_version FROM ratings WHERE user_id = @demo AND movie_id = 19995;
UPDATE ratings SET rating_value = 4.0
 WHERE user_id = @demo AND movie_id = 19995 AND (updated_at <=> @seen_version);
SELECT ROW_COUNT() AS rows_updated_on_retry;   -- 1

-- Pessimistic alternative, for comparison: lock the row while deciding.
START TRANSACTION;
SELECT rating_value FROM ratings
 WHERE user_id = @demo AND movie_id = 19995
   FOR UPDATE;                             -- exclusive lock until COMMIT/ROLLBACK
UPDATE ratings SET rating_value = 2.0 WHERE user_id = @demo AND movie_id = 19995;
COMMIT;
-- FOR UPDATE never loses a write, but every other session waits.
-- FOR SHARE would allow concurrent readers but still block writers.

-- Clean up
DELETE FROM users        WHERE user_id = @demo;
DELETE FROM rating_audit WHERE user_id = @demo;

SELECT 'F: done, data unchanged' AS section,
       (SELECT COUNT(*) FROM users)   AS users_total,     -- as loaded
       (SELECT COUNT(*) FROM ratings) AS ratings_total;   -- as loaded
