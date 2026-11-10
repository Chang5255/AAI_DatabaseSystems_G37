-- =====================================================================
-- INF2003 Group Project - Team 37
-- 03_verify.sql : prove the schema and the load are correct
--
-- Run AFTER 02_load.sql. Sections A-C are safe to run all at once.
-- Section D is DELIBERATE FAILURES - run those statements ONE AT A TIME
-- and screenshot each error for the report / video.
-- =====================================================================

USE moviedb;

-- ---------------------------------------------------------------------
-- A. Schema proof: the constraints actually exist in the server
-- ---------------------------------------------------------------------

-- A1. every foreign key, with its delete rule
SELECT rc.CONSTRAINT_NAME, rc.TABLE_NAME, kcu.COLUMN_NAME,
       rc.REFERENCED_TABLE_NAME, rc.DELETE_RULE, rc.UPDATE_RULE
FROM information_schema.REFERENTIAL_CONSTRAINTS rc
JOIN information_schema.KEY_COLUMN_USAGE kcu
  ON kcu.CONSTRAINT_SCHEMA = rc.CONSTRAINT_SCHEMA
 AND kcu.CONSTRAINT_NAME   = rc.CONSTRAINT_NAME
WHERE rc.CONSTRAINT_SCHEMA = 'moviedb'
ORDER BY rc.TABLE_NAME, rc.CONSTRAINT_NAME;

-- A2. every CHECK constraint and its expression
SELECT cc.CONSTRAINT_NAME, tc.TABLE_NAME, cc.CHECK_CLAUSE
FROM information_schema.CHECK_CONSTRAINTS cc
JOIN information_schema.TABLE_CONSTRAINTS tc
  ON tc.CONSTRAINT_SCHEMA = cc.CONSTRAINT_SCHEMA
 AND tc.CONSTRAINT_NAME   = cc.CONSTRAINT_NAME
WHERE cc.CONSTRAINT_SCHEMA = 'moviedb'
ORDER BY tc.TABLE_NAME, cc.CONSTRAINT_NAME;

-- A3. full DDL of the two most interesting tables
SHOW CREATE TABLE ratings;
SHOW CREATE TABLE movie_cast;

-- A4. charset must be utf8mb4 everywhere (accented titles and names)
SELECT TABLE_NAME, TABLE_COLLATION
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = 'moviedb';

-- ---------------------------------------------------------------------
-- B. Referential and value integrity: every count below must be 0
-- ---------------------------------------------------------------------
SELECT 'movie_genres orphan movie' AS check_name,
       COUNT(*) AS violations
  FROM movie_genres mg LEFT JOIN movies m USING (movie_id)
 WHERE m.movie_id IS NULL
UNION ALL
SELECT 'movie_genres orphan genre',
       COUNT(*) FROM movie_genres mg LEFT JOIN genres g USING (genre_id)
 WHERE g.genre_id IS NULL
UNION ALL
SELECT 'cast orphan movie',
       COUNT(*) FROM movie_cast c LEFT JOIN movies m USING (movie_id)
 WHERE m.movie_id IS NULL
UNION ALL
SELECT 'cast orphan person',
       COUNT(*) FROM movie_cast c LEFT JOIN people p USING (person_id)
 WHERE p.person_id IS NULL
UNION ALL
SELECT 'crew orphan movie',
       COUNT(*) FROM movie_crew c LEFT JOIN movies m USING (movie_id)
 WHERE m.movie_id IS NULL
UNION ALL
SELECT 'crew orphan person',
       COUNT(*) FROM movie_crew c LEFT JOIN people p USING (person_id)
 WHERE p.person_id IS NULL
UNION ALL
SELECT 'ratings orphan user',
       COUNT(*) FROM ratings r LEFT JOIN users u USING (user_id)
 WHERE u.user_id IS NULL
UNION ALL
SELECT 'ratings orphan movie',
       COUNT(*) FROM ratings r LEFT JOIN movies m USING (movie_id)
 WHERE m.movie_id IS NULL
UNION ALL
SELECT 'rating out of range',
       COUNT(*) FROM ratings WHERE rating_value NOT BETWEEN 0.5 AND 5.0
UNION ALL
SELECT 'rating not half-star',
       COUNT(*) FROM ratings WHERE rating_value * 2 <> FLOOR(rating_value * 2)
UNION ALL
SELECT 'zero-value sentinels left',
       COUNT(*) FROM movies WHERE budget = 0 OR revenue = 0 OR runtime = 0;

-- B2. no silent truncation during LOAD DATA
SELECT MAX(CHAR_LENGTH(title))          AS max_title,      -- limit 255
       MAX(CHAR_LENGTH(original_title)) AS max_orig_title  -- limit 255
  FROM movies;
SELECT MAX(CHAR_LENGTH(character_name)) AS max_character   -- limit 300
  FROM movie_cast;
SELECT MAX(CHAR_LENGTH(name))           AS max_person_name -- limit 255
  FROM people;

-- ---------------------------------------------------------------------
-- C. Volume and density: is there enough data to demo properly?
-- ---------------------------------------------------------------------
SELECT 'movies' AS table_name, COUNT(*) AS n FROM movies
UNION ALL SELECT 'genres',       COUNT(*) FROM genres
UNION ALL SELECT 'movie_genres', COUNT(*) FROM movie_genres
UNION ALL SELECT 'people',       COUNT(*) FROM people
UNION ALL SELECT 'movie_cast',   COUNT(*) FROM movie_cast
UNION ALL SELECT 'movie_crew',   COUNT(*) FROM movie_crew
UNION ALL SELECT 'users',        COUNT(*) FROM users
UNION ALL SELECT 'ratings',      COUNT(*) FROM ratings;

-- C2. rating density. The target was films_ge_20 >= 1,000; with the
--     real MovieLens ratings loaded it is about 2,200, so an index on
--     ratings.movie_id now has something to be measured against.
SELECT COUNT(*)                                  AS rated_films,
       SUM(c >= 5)                               AS films_ge_5,
       SUM(c >= 20)                              AS films_ge_20,
       SUM(c >= 50)                              AS films_ge_50,
       ROUND(AVG(c), 2)                          AS avg_ratings_per_film
  FROM (SELECT movie_id, COUNT(*) AS c FROM ratings GROUP BY movie_id) t;

-- C3. every genre must be usable in a per-genre aggregate
SELECT g.name,
       COUNT(DISTINCT mg.movie_id) AS films,
       COUNT(r.rating_value)       AS ratings
  FROM genres g
  JOIN movie_genres mg ON mg.genre_id = g.genre_id
  LEFT JOIN ratings  r ON r.movie_id  = mg.movie_id
 GROUP BY g.name
 ORDER BY ratings ASC;

-- C4. ratings per user, and the time span they cover
SELECT MIN(c) AS min_per_user, ROUND(AVG(c)) AS avg_per_user, MAX(c) AS max_per_user
  FROM (SELECT user_id, COUNT(*) AS c FROM ratings GROUP BY user_id) t;
SELECT MIN(rated_at) AS first_rating, MAX(rated_at) AS last_rating,
       COUNT(DISTINCT DATE_FORMAT(rated_at, '%Y-%m')) AS distinct_months
  FROM ratings;

-- C5. provenance: which rows are real and which are synthetic.
--     Ratings for user ids 1-12 were generated by the first cleaning
--     pass (TMDB publishes no per-user ratings); everything from 1001
--     up is real MovieLens 25M data. The report must not blur the two.
SELECT CASE WHEN user_id <= 1000 THEN 'synthetic (ids 1-12)'
            ELSE 'real (MovieLens 25M, ids 1001+)' END AS provenance,
       COUNT(DISTINCT user_id)                         AS users,
       COUNT(*)                                        AS ratings,
       COUNT(DISTINCT movie_id)                        AS films,
       ROUND(AVG(rating_value), 3)                     AS avg_rating,
       MIN(DATE(rated_at))                             AS earliest,
       MAX(DATE(rated_at))                             AS latest
  FROM ratings
 GROUP BY provenance;

-- ---------------------------------------------------------------------
-- D. DELIBERATE VIOLATIONS - each statement below MUST fail.
--    Run them ONE AT A TIME and screenshot the error message.
--    They are wrapped in a transaction that is rolled back, so nothing
--    is written even if one unexpectedly succeeds.
-- ---------------------------------------------------------------------
START TRANSACTION;

-- D1. CHECK: rating above the allowed range  -> Error 3819
-- INSERT INTO ratings (user_id, movie_id, rating_value) VALUES (1, 19995, 6.0);

-- D2. CHECK: rating not on a half-star step  -> Error 3819
-- INSERT INTO ratings (user_id, movie_id, rating_value) VALUES (1, 19995, 3.7);

-- D3. PRIMARY KEY: same user rating the same film twice -> Error 1062
-- INSERT INTO ratings (user_id, movie_id, rating_value)
-- SELECT user_id, movie_id, 4.0 FROM ratings LIMIT 1;

-- D4. FOREIGN KEY: rating a film that does not exist -> Error 1452
-- INSERT INTO ratings (user_id, movie_id, rating_value) VALUES (1, 999999999, 4.0);

-- D5. FOREIGN KEY RESTRICT: deleting a genre still used by films -> Error 1451
-- DELETE FROM genres WHERE genre_id = 28;

-- D6. UNIQUE: duplicate username -> Error 1062
-- INSERT INTO users (username, display_name)
-- SELECT username, 'copy' FROM users LIMIT 1;

-- D7. CHECK: username shorter than 3 characters -> Error 3819
-- INSERT INTO users (username, display_name) VALUES ('ab', 'Too Short');

ROLLBACK;

-- ---------------------------------------------------------------------
-- E. CASCADE demo: deleting a movie removes its dependent rows.
--    Rolled back, so the data is unchanged.
-- ---------------------------------------------------------------------
START TRANSACTION;
SELECT COUNT(*) AS cast_rows_before FROM movie_cast WHERE movie_id = 19995;
DELETE FROM movies WHERE movie_id = 19995;
SELECT COUNT(*) AS cast_rows_after,
       (SELECT COUNT(*) FROM movie_genres WHERE movie_id = 19995) AS genre_rows_after,
       (SELECT COUNT(*) FROM ratings      WHERE movie_id = 19995) AS rating_rows_after
  FROM movie_cast WHERE movie_id = 19995;
ROLLBACK;
SELECT COUNT(*) AS cast_rows_restored FROM movie_cast WHERE movie_id = 19995;
