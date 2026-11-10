-- =====================================================================
-- INF2003 Group Project - Team 37
-- 08_triggers.sql : audit trail and non-standard integrity constraints
--
-- Run AFTER 02_load.sql (and after 04_views.sql if you want the demo
-- at the bottom to read v_user_activity). Re-runnable: every object is
-- dropped first, and the demo rolls itself back.
--
-- WHY THIS FILE EXISTS
-- A CHECK constraint can only look at the row being written. Two of
-- our real business rules cannot be written as a CHECK:
--
--   R1 "a user may not rate a film that has not been released yet"
--      needs movies.release_date, i.e. another table. MySQL rejects
--      subqueries inside CHECK (error 3814), and the SQL standard's
--      CREATE ASSERTION is not implemented by any mainstream DBMS.
--
--   R2 "every change to a rating must be recorded, with who and when"
--      is not a constraint on a value at all: it is an action that has
--      to happen alongside the write.
--
-- Both are enforced here with triggers, so they hold for EVERY client:
-- the Flask app, MySQL Workbench, and the benchmark scripts alike. Had
-- we written the same rules in Python, anyone connecting with
-- Workbench could break them (Lecture 2: "ICs are enforced centrally,
-- the stored data is then more faithful to the real world").
--
-- CONTENTS
--   1. rating_audit table
--   2. R2  AFTER INSERT / UPDATE / DELETE  -> write the audit trail
--   3. R1  BEFORE INSERT / UPDATE          -> reject unreleased films
--   4. Proof the objects exist
--   5. Demo: a normal change, a rejected change, and the audit rows
--   6. Documented limitation: foreign-key CASCADE does not fire triggers
-- =====================================================================

USE moviedb;

-- =====================================================================
-- 1. The audit table
--    One row per change to one rating. Append-only in practice: the
--    application account has no INSERT/UPDATE/DELETE privilege on it
--    (01b_create_user.sql), yet the triggers can still write to it
--    because a trigger runs with its DEFINER's privileges, not the
--    caller's. That is what makes the trail trustworthy.
-- =====================================================================
DROP TABLE IF EXISTS rating_audit;

CREATE TABLE rating_audit (
  audit_id      BIGINT       NOT NULL AUTO_INCREMENT,
  action        ENUM('INSERT', 'UPDATE', 'DELETE') NOT NULL,
  user_id       INT          NOT NULL,
  movie_id      INT          NOT NULL,
  old_value     DECIMAL(2,1) NULL,
  new_value     DECIMAL(2,1) NULL,
  changed_by    VARCHAR(100) NOT NULL,
  connection_id BIGINT       NOT NULL,
  changed_at    TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (audit_id),
  INDEX idx_audit_user  (user_id, changed_at),
  INDEX idx_audit_movie (movie_id, changed_at),
  INDEX idx_audit_time  (changed_at),
  CONSTRAINT chk_audit_values CHECK (
       (action = 'INSERT' AND old_value IS NULL     AND new_value IS NOT NULL)
    OR (action = 'UPDATE' AND old_value IS NOT NULL AND new_value IS NOT NULL)
    OR (action = 'DELETE' AND old_value IS NOT NULL AND new_value IS NULL))
) ENGINE=InnoDB;

-- No foreign key to users/ratings on purpose: the audit row must
-- survive the deletion of the rating (and of the user) it describes.
-- An audit trail that disappears with its subject is worthless.

-- =====================================================================
-- 2. R2: record every change to a rating
--    SESSION_USER(), not CURRENT_USER(): inside a trigger
--    CURRENT_USER() returns the trigger's DEFINER, which is root
--    because root creates the triggers, so every row would look the
--    same. SESSION_USER() returns the account that actually connected,
--    so the trail shows whether a change came from the application
--    account or from someone sitting in Workbench. CONNECTION_ID()
--    groups the changes made in one session.
-- =====================================================================
DROP TRIGGER IF EXISTS trg_ratings_after_insert;
DROP TRIGGER IF EXISTS trg_ratings_after_update;
DROP TRIGGER IF EXISTS trg_ratings_after_delete;

DELIMITER $$

CREATE TRIGGER trg_ratings_after_insert
AFTER INSERT ON ratings
FOR EACH ROW
BEGIN
  INSERT INTO rating_audit (action, user_id, movie_id, old_value, new_value,
                            changed_by, connection_id)
  VALUES ('INSERT', NEW.user_id, NEW.movie_id, NULL, NEW.rating_value,
          SESSION_USER(), CONNECTION_ID());
END $$

CREATE TRIGGER trg_ratings_after_update
AFTER UPDATE ON ratings
FOR EACH ROW
BEGIN
  -- Only log a real change of score. An UPDATE that sets the same
  -- value (as the "rate again with the same stars" button can) would
  -- otherwise fill the trail with noise.
  IF NEW.rating_value <> OLD.rating_value THEN
    INSERT INTO rating_audit (action, user_id, movie_id, old_value, new_value,
                              changed_by, connection_id)
    VALUES ('UPDATE', NEW.user_id, NEW.movie_id, OLD.rating_value, NEW.rating_value,
            SESSION_USER(), CONNECTION_ID());
  END IF;
END $$

CREATE TRIGGER trg_ratings_after_delete
AFTER DELETE ON ratings
FOR EACH ROW
BEGIN
  INSERT INTO rating_audit (action, user_id, movie_id, old_value, new_value,
                            changed_by, connection_id)
  VALUES ('DELETE', OLD.user_id, OLD.movie_id, OLD.rating_value, NULL,
          SESSION_USER(), CONNECTION_ID());
END $$

-- =====================================================================
-- 3. R1: a rating must point at a film that has already been released
--    This is the cross-table rule a CHECK cannot express. SIGNAL
--    SQLSTATE '45000' raises error 1644 with our own message, which
--    the Flask app catches and shows to the user, exactly as it shows
--    CHECK and FOREIGN KEY errors.
--    One trigger per event: MySQL allows only one BEFORE INSERT and one
--    BEFORE UPDATE trigger per table, so the shared logic is repeated
--    rather than factored into a procedure (a procedure call inside a
--    trigger would need EXECUTE rights for every caller).
-- =====================================================================
DROP TRIGGER IF EXISTS trg_ratings_before_insert;
DROP TRIGGER IF EXISTS trg_ratings_before_update;

CREATE TRIGGER trg_ratings_before_insert
BEFORE INSERT ON ratings
FOR EACH ROW
BEGIN
  DECLARE v_release DATE;

  SELECT release_date INTO v_release
    FROM movies
   WHERE movie_id = NEW.movie_id;

  IF v_release IS NOT NULL AND v_release > CURDATE() THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'Rejected by trigger trg_ratings_before_insert: cannot rate a film before its release date';
  END IF;
END $$

CREATE TRIGGER trg_ratings_before_update
BEFORE UPDATE ON ratings
FOR EACH ROW
BEGIN
  DECLARE v_release DATE;

  SELECT release_date INTO v_release
    FROM movies
   WHERE movie_id = NEW.movie_id;

  IF v_release IS NOT NULL AND v_release > CURDATE() THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'Rejected by trigger trg_ratings_before_update: cannot rate a film before its release date';
  END IF;
END $$

DELIMITER ;

-- =====================================================================
-- 4. Proof: the objects exist in the server
-- =====================================================================
SELECT TRIGGER_NAME, EVENT_MANIPULATION AS event_type, ACTION_TIMING AS timing,
       EVENT_OBJECT_TABLE AS on_table
  FROM information_schema.TRIGGERS
 WHERE TRIGGER_SCHEMA = 'moviedb'
 ORDER BY EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION;

SHOW CREATE TABLE rating_audit;

-- =====================================================================
-- 5. Demo. Everything here is rolled back, so the loaded data is
--    unchanged afterwards EXCEPT that rating_audit keeps its rows only
--    if you COMMIT. Because the whole block is one transaction ending
--    in ROLLBACK, the audit rows disappear too - which is correct: the
--    change never happened, so it must not be in the trail.
--    Run the block, read the output, then run section 5b to see the
--    trail survive a committed change.
-- =====================================================================

-- 5a. Rolled back: insert, update, delete one rating and watch the trail
START TRANSACTION;

SET @demo_user  = (SELECT user_id FROM users ORDER BY user_id LIMIT 1);
SET @demo_movie = 19995;   -- Avatar, released 2009

-- INSERT ... SELECT rather than VALUES, so that running this file on an
-- empty database (before 02_load.sql) inserts nothing instead of
-- failing with "Column 'user_id' cannot be null".
DELETE FROM ratings WHERE user_id = @demo_user AND movie_id = @demo_movie;

INSERT INTO ratings (user_id, movie_id, rating_value)
SELECT @demo_user, @demo_movie, 4.0
 WHERE @demo_user IS NOT NULL
   AND EXISTS (SELECT 1 FROM movies WHERE movie_id = @demo_movie);

UPDATE ratings SET rating_value = 2.5 WHERE user_id = @demo_user AND movie_id = @demo_movie;
UPDATE ratings SET rating_value = 2.5 WHERE user_id = @demo_user AND movie_id = @demo_movie;  -- no change: not logged
DELETE FROM ratings WHERE user_id = @demo_user AND movie_id = @demo_movie;

-- Expect 3 or 4 rows: the pre-clean DELETE (only if that rating
-- existed), then INSERT 4.0, UPDATE 4.0 -> 2.5, DELETE 2.5.
-- The repeated UPDATE to the same value is absent.
SELECT action, user_id, movie_id, old_value, new_value, changed_by, changed_at
  FROM rating_audit
 WHERE user_id = @demo_user AND movie_id = @demo_movie
 ORDER BY audit_id;

ROLLBACK;

-- The trail is gone with the transaction, and so is the rating.
SELECT COUNT(*) AS audit_rows_after_rollback
  FROM rating_audit WHERE movie_id = @demo_movie AND user_id = @demo_user;

-- 5b. R1 rejected. Give an existing film a future release date inside a
--     transaction, try to rate it, then roll the whole thing back.
--     RUN THE INSERT ON ITS OWN and screenshot error 1644.
START TRANSACTION;
UPDATE movies SET release_date = DATE_ADD(CURDATE(), INTERVAL 1 YEAR) WHERE movie_id = 19995;
-- INSERT INTO ratings (user_id, movie_id, rating_value) VALUES (1, 19995, 4.0);
--   -> Error 1644: Rejected by trigger trg_ratings_before_insert:
--      cannot rate a film before its release date
ROLLBACK;

SELECT release_date AS release_date_restored FROM movies WHERE movie_id = 19995;

-- =====================================================================
-- 6. DOCUMENTED LIMITATION (worth a sentence in the report)
--    MySQL does not fire triggers for rows removed by a foreign-key
--    action. Deleting a user removes their ratings through
--    fk_ratings_user ON DELETE CASCADE, and trg_ratings_after_delete
--    does NOT run for those rows, so they are not audited.
--    Demonstrate it, rolled back:
-- =====================================================================
START TRANSACTION;

SET @victim = (SELECT user_id FROM ratings GROUP BY user_id ORDER BY COUNT(*) DESC LIMIT 1);
SELECT @victim AS user_with_most_ratings,
       (SELECT COUNT(*) FROM ratings WHERE user_id = @victim) AS ratings_before,
       (SELECT COUNT(*) FROM rating_audit WHERE user_id = @victim) AS audit_before;

DELETE FROM users WHERE user_id = @victim;

SELECT (SELECT COUNT(*) FROM ratings     WHERE user_id = @victim) AS ratings_after,     -- 0
       (SELECT COUNT(*) FROM rating_audit WHERE user_id = @victim) AS audit_after;      -- unchanged!

ROLLBACK;

-- Our fix lives in the application: webapp/routes.py deletes a user
-- inside an explicit transaction and writes its own record of what it
-- removed, in MySQL and in MongoDB. The lesson for the report: a
-- database-side trigger is not a complete audit solution once
-- referential actions are involved.

SELECT 'Triggers and audit table ready' AS status;
