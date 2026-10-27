-- =====================================================================
-- INF2003 Group Project - Team 37
-- 07_indexes.sql : indexing experiments with EXPLAIN ANALYZE
--
-- Run AFTER 04_views.sql. Re-runnable.
-- Adds three indexes that it keeps (sections 2, 3 and 4). Rerunning
-- 01_schema.sql removes them, so rerun this file after a rebuild.
--
-- How to read the output
--   EXPLAIN ANALYZE runs the query and returns ONE text cell with the
--   plan MySQL actually used. In Workbench: right-click the cell ->
--   Open Value in Viewer. The evidence to quote in the report is:
--     * the access method: "Table scan" vs "Index lookup" / "Covering
--       index lookup" / "Full-text index search"
--     * rows=...      : rows read at each step
--     * actual time=a..b : ms to first row .. ms to last row
--   Timings from one run are noisy. The benchmark scripts in
--   benchmark/ repeat each query many times; use those numbers for
--   timing charts and use this file for the plans.
--
-- Why IGNORE INDEX instead of DROP INDEX in some sections
--   InnoDB needs an index on every foreign-key column, so
--   idx_cast_movie, idx_crew_movie and idx_ratings_movie cannot be
--   dropped while their foreign keys exist. IGNORE INDEX (...) tells the
--   optimiser to plan as if the index were not there, without changing
--   the schema.
--
--   1. Foreign-key index: one movie's cast            IGNORE INDEX vs index
--   2. Composite index for the co-star self-join      before vs after CREATE INDEX
--   3. Composite index for actor-director pairs       before vs after CREATE INDEX
--   4. Text search: LIKE vs prefix LIKE vs FULLTEXT   three plans
--   5. Correlated subquery vs CTE rewrite (Q9)        same result, two plans
--   6. Ratings lookup by film                         IGNORE INDEX vs index
--   7. Final index list and sizes
-- =====================================================================

USE moviedb;

-- Fresh optimiser statistics so plans reflect the loaded data.
ANALYZE TABLE movies, genres, movie_genres, people, movie_cast, movie_crew, users, ratings;

-- Helper: MySQL 8.0 has no DROP INDEX IF EXISTS, so this procedure
-- checks information_schema first. It makes the file re-runnable.
DROP PROCEDURE IF EXISTS drop_index_if_exists;
DELIMITER $$
CREATE PROCEDURE drop_index_if_exists(IN p_table VARCHAR(64), IN p_index VARCHAR(64))
BEGIN
  IF EXISTS (SELECT 1
               FROM information_schema.STATISTICS
              WHERE TABLE_SCHEMA = DATABASE()
                AND TABLE_NAME   = p_table
                AND INDEX_NAME   = p_index) THEN
    SET @ddl = CONCAT('DROP INDEX `', p_index, '` ON `', p_table, '`');
    PREPARE stmt FROM @ddl;
    EXECUTE stmt;
    DEALLOCATE PREPARE stmt;
  END IF;
END $$
DELIMITER ;

-- Reset: remove the indexes this file adds, so every run starts from
-- the 01_schema.sql baseline. Without this, a second run would let
-- section 1 use the composite index from section 2 and hide the
-- table scan.
CALL drop_index_if_exists('movie_cast', 'idx_cast_movie_order_person');
CALL drop_index_if_exists('movie_crew', 'idx_crew_job_movie_person');
CALL drop_index_if_exists('movies',     'ft_movies_title');
ANALYZE TABLE movies, movie_cast, movie_crew;


-- =====================================================================
-- 1. Foreign-key index: retrieve one movie's cast
--    This is the SQL side of the main SQL-vs-MongoDB experiment.
--    Expect: without the index, a table scan reads all ~106,000 cast
--    rows to find 83. With it, an index lookup reads only those 83.
-- =====================================================================

-- Warm-up run so both plans are measured with the data in memory.
SELECT COUNT(*) FROM movie_cast WHERE movie_id = 19995;

-- 1a. Without the index
EXPLAIN ANALYZE
SELECT p.person_id, p.name, mc.character_name, mc.cast_order
  FROM movie_cast mc IGNORE INDEX (idx_cast_movie)
  JOIN people p ON p.person_id = mc.person_id
 WHERE mc.movie_id = 19995
 ORDER BY mc.cast_order;

-- 1b. With the index
EXPLAIN ANALYZE
SELECT p.person_id, p.name, mc.character_name, mc.cast_order
  FROM movie_cast mc
  JOIN people p ON p.person_id = mc.person_id
 WHERE mc.movie_id = 19995
 ORDER BY mc.cast_order;


-- =====================================================================
-- 2. Composite index for the co-star self-join (06_queries.sql Q11)
--    The query filters cast_order < 5 and joins on movie_id, then needs
--    person_id. A composite index (movie_id, cast_order, person_id)
--    holds all three columns, so MySQL can answer from the index alone
--    ("covering index") without reading the table rows.
-- =====================================================================
CALL drop_index_if_exists('movie_cast', 'idx_cast_movie_order_person');
ANALYZE TABLE movie_cast;

-- 2a. Before
EXPLAIN ANALYZE
SELECT a.person_id AS p1, b.person_id AS p2, COUNT(*) AS films_together
  FROM movie_cast a
  JOIN movie_cast b ON b.movie_id  = a.movie_id
                   AND a.person_id < b.person_id
 WHERE a.cast_order < 5
   AND b.cast_order < 5
 GROUP BY a.person_id, b.person_id
HAVING COUNT(*) >= 4;

CREATE INDEX idx_cast_movie_order_person
    ON movie_cast (movie_id, cast_order, person_id);
ANALYZE TABLE movie_cast;

-- 2b. After
EXPLAIN ANALYZE
SELECT a.person_id AS p1, b.person_id AS p2, COUNT(*) AS films_together
  FROM movie_cast a
  JOIN movie_cast b ON b.movie_id  = a.movie_id
                   AND a.person_id < b.person_id
 WHERE a.cast_order < 5
   AND b.cast_order < 5
 GROUP BY a.person_id, b.person_id
HAVING COUNT(*) >= 4;


-- =====================================================================
-- 3. Composite index for actor-director partnerships (Q12)
--    idx_crew_job (job) finds Director rows, but each match then needs
--    a trip to the table for movie_id and person_id. The composite
--    (job, movie_id, person_id) covers the whole access.
-- =====================================================================
CALL drop_index_if_exists('movie_crew', 'idx_crew_job_movie_person');
ANALYZE TABLE movie_crew;

-- 3a. Before
EXPLAIN ANALYZE
SELECT cr.person_id AS director_id, mc.person_id AS actor_id,
       COUNT(DISTINCT mc.movie_id) AS films_together
  FROM movie_crew cr
  JOIN movie_cast mc ON mc.movie_id = cr.movie_id
                    AND mc.person_id <> cr.person_id
 WHERE cr.job = 'Director'
 GROUP BY cr.person_id, mc.person_id
HAVING COUNT(DISTINCT mc.movie_id) >= 4;

CREATE INDEX idx_crew_job_movie_person
    ON movie_crew (job, movie_id, person_id);
ANALYZE TABLE movie_crew;

-- 3b. After
EXPLAIN ANALYZE
SELECT cr.person_id AS director_id, mc.person_id AS actor_id,
       COUNT(DISTINCT mc.movie_id) AS films_together
  FROM movie_crew cr
  JOIN movie_cast mc ON mc.movie_id = cr.movie_id
                    AND mc.person_id <> cr.person_id
 WHERE cr.job = 'Director'
 GROUP BY cr.person_id, mc.person_id
HAVING COUNT(DISTINCT mc.movie_id) >= 4;


-- =====================================================================
-- 4. Text search on titles and names
--    4a  LIKE '%star%'   leading wildcard: no B-tree index can help,
--                        every title is read (table or index scan)
--    4b  LIKE 'Star%'    prefix only: uses idx_movies_title as a range
--    4c  MATCH AGAINST   FULLTEXT index: word search via an inverted
--                        index, closest to MongoDB's text index
--    Note the results differ: FULLTEXT matches whole words, so 'star'
--    finds "Star Wars" but not "Starship Troopers". That difference in
--    meaning is part of the comparison, not just speed.
-- =====================================================================
CALL drop_index_if_exists('movies', 'ft_movies_title');
CREATE FULLTEXT INDEX ft_movies_title ON movies (title);
ANALYZE TABLE movies;

-- 4a. Leading wildcard
EXPLAIN ANALYZE
SELECT movie_id, title FROM movies WHERE title LIKE '%star%';

-- 4b. Prefix match
EXPLAIN ANALYZE
SELECT movie_id, title FROM movies WHERE title LIKE 'Star%';

-- 4c. FULLTEXT
EXPLAIN ANALYZE
SELECT movie_id, title
  FROM movies
 WHERE MATCH(title) AGAINST ('star' IN NATURAL LANGUAGE MODE);

-- Result counts for the report: how many titles each method returns.
SELECT
  (SELECT COUNT(*) FROM movies WHERE title LIKE '%star%')  AS like_contains,
  (SELECT COUNT(*) FROM movies WHERE title LIKE 'Star%')   AS like_prefix,
  (SELECT COUNT(*) FROM movies
    WHERE MATCH(title) AGAINST ('star' IN NATURAL LANGUAGE MODE)) AS fulltext_word;

-- 4d. Person names: prefix LIKE can use idx_people_name,
--     a leading wildcard cannot (105k names scanned).
EXPLAIN ANALYZE SELECT person_id, name FROM people WHERE name LIKE 'Christopher%';
EXPLAIN ANALYZE SELECT person_id, name FROM people WHERE name LIKE '%Nolan%';


-- =====================================================================
-- 5. Correlated subquery vs CTE rewrite (06_queries.sql Q9)
--    Both return films scoring at least 2.0 above their genre average
--    with 2,000+ votes. 5a recomputes a genre average inside a
--    correlated subquery; 5b computes all 20 genre averages once in a
--    CTE and joins to it. Compare the loops= and rows= figures.
-- =====================================================================

-- 5a. Correlated subquery
EXPLAIN ANALYZE
SELECT g.name, m.title, m.vote_average
  FROM movies m
  JOIN movie_genres mg ON mg.movie_id = m.movie_id
  JOIN genres g        ON g.genre_id  = mg.genre_id
 WHERE m.vote_count >= 2000
   AND m.vote_average >= 2.0 + (
         SELECT AVG(m2.vote_average)
           FROM movies m2
           JOIN movie_genres mg2 ON mg2.movie_id = m2.movie_id
          WHERE mg2.genre_id = mg.genre_id
            AND m2.vote_count > 0);

-- 5b. CTE rewrite
EXPLAIN ANALYZE
WITH genre_avg AS (
  SELECT mg.genre_id, AVG(m.vote_average) AS avg_score
    FROM movies m
    JOIN movie_genres mg ON mg.movie_id = m.movie_id
   WHERE m.vote_count > 0
   GROUP BY mg.genre_id
)
SELECT g.name, m.title, m.vote_average
  FROM movies m
  JOIN movie_genres mg ON mg.movie_id = m.movie_id
  JOIN genres g        ON g.genre_id  = mg.genre_id
  JOIN genre_avg ga    ON ga.genre_id = mg.genre_id
 WHERE m.vote_count >= 2000
   AND m.vote_average >= ga.avg_score + 2.0;

-- Both versions must return the same rows (expect two equal counts).
SELECT
  (SELECT COUNT(*)
     FROM movies m JOIN movie_genres mg ON mg.movie_id = m.movie_id
    WHERE m.vote_count >= 2000
      AND m.vote_average >= 2.0 + (
            SELECT AVG(m2.vote_average)
              FROM movies m2 JOIN movie_genres mg2 ON mg2.movie_id = m2.movie_id
             WHERE mg2.genre_id = mg.genre_id AND m2.vote_count > 0)) AS correlated_rows,
  (WITH genre_avg AS (
     SELECT mg.genre_id, AVG(m.vote_average) AS avg_score
       FROM movies m JOIN movie_genres mg ON mg.movie_id = m.movie_id
      WHERE m.vote_count > 0 GROUP BY mg.genre_id)
   SELECT COUNT(*)
     FROM movies m JOIN movie_genres mg ON mg.movie_id = m.movie_id
     JOIN genre_avg ga ON ga.genre_id = mg.genre_id
    WHERE m.vote_count >= 2000 AND m.vote_average >= ga.avg_score + 2.0) AS cte_rows;


-- =====================================================================
-- 6. Ratings lookup by film
--    user_id needs no extra index: it is the first column of the
--    composite primary key (user_id, movie_id), so WHERE user_id = ?
--    always uses PRIMARY (leftmost-prefix rule). movie_id is the second
--    column, so it needs its own index, idx_ratings_movie.
--    With only ~784 ratings the timing difference is negligible; the
--    plan difference (table scan vs index lookup) is still visible.
--    benchmark/ratings_scaling.py repeats this at 1k to 1M rows.
-- =====================================================================

-- 6a. user_id: always the primary key
EXPLAIN ANALYZE SELECT * FROM ratings WHERE user_id = 3;

-- 6b. movie_id without its index
EXPLAIN ANALYZE SELECT * FROM ratings IGNORE INDEX (idx_ratings_movie) WHERE movie_id = 155;

-- 6c. movie_id with its index
EXPLAIN ANALYZE SELECT * FROM ratings WHERE movie_id = 155;


-- =====================================================================
-- 7. Final index list and storage used by data vs indexes
-- =====================================================================
SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, INDEX_TYPE,
       GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX SEPARATOR ', ') AS columns_in_order
  FROM information_schema.STATISTICS
 WHERE TABLE_SCHEMA = 'moviedb'
 GROUP BY TABLE_NAME, INDEX_NAME, NON_UNIQUE, INDEX_TYPE
 ORDER BY TABLE_NAME, INDEX_NAME = 'PRIMARY' DESC, INDEX_NAME;

SELECT TABLE_NAME,
       TABLE_ROWS                         AS approx_rows,
       ROUND(DATA_LENGTH  / 1024 / 1024, 2) AS data_mb,
       ROUND(INDEX_LENGTH / 1024 / 1024, 2) AS index_mb
  FROM information_schema.TABLES
 WHERE TABLE_SCHEMA = 'moviedb'
   AND TABLE_TYPE = 'BASE TABLE'
 ORDER BY DATA_LENGTH DESC;
