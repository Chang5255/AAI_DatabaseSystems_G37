-- =====================================================================
-- INF2003 Group Project - Team 37
-- 06_queries.sql : search, movie detail, reports and analytics
--
-- Run AFTER 04_views.sql. Read-only: nothing here changes data.
--
-- Each query is labelled with the SQL feature it demonstrates.
-- Parameters are session variables (SET @...) so each block can be run
-- on its own in Workbench: put the cursor in the SET line, select down
-- to the end of the query, and press Ctrl+Shift+Enter.
-- In the Python back end the @variables become %s placeholders.
--
--   SEARCH
--   Q1  Search by title, genre, year and language     optional filters, EXISTS
--   Q2  Search by person (cast or crew)               UNION ALL
--   MOVIE DETAIL  (benchmark pair for MongoDB findOne)
--   Q3a Core fields and genres                        view
--   Q3b Cast list                                     3-table join
--   Q3c Crew list                                     3-table join
--   Q3d Whole movie as one JSON document              JSON_OBJECT, JSON_ARRAYAGG
--   REPORTS
--   Q4  Top 10 films: weighted vs naive average       Bayesian average
--   Q5  Top films per genre                           window view
--   Q6  Average score by genre and decade             GROUP BY WITH ROLLUP, GROUPING()
--   Q7  Most active raters                            DENSE_RANK()
--   Q8a Highest-grossing films with profit and ROI    computed columns
--   Q8b Highest-grossing film of each year            ROW_NUMBER() OVER (PARTITION BY)
--   ANALYTICS
--   Q9  Films far above their genre average           correlated subquery
--   Q10 Best directors (at least 5 films)             view + HAVING
--   Q11 Most frequent co-star pairs                   self-join
--   Q12 Repeat actor-director partnerships            join cast to crew
--   Q13 Year-on-year trend                            CTE + LAG()
--   Q14 Each user's favourite genre                   CTE + window over aggregate
-- =====================================================================

USE moviedb;


-- =====================================================================
-- SEARCH
-- =====================================================================

-- ---------------------------------------------------------------------
-- Q1. Search by title, genre, year range and language.
-- Every filter is optional: a NULL parameter switches that filter off,
-- so one statement serves the whole search form.
-- EXISTS checks genre membership without joining movie_genres into the
-- main query, which would repeat a film once per genre.
-- Note: LIKE '%...%' cannot use an ordinary B-tree index (leading
-- wildcard). 07_indexes.sql compares this with a FULLTEXT index.
-- ---------------------------------------------------------------------
SET @q         = 'star';
SET @genre     = 'Science Fiction';
SET @year_from = 1970;
SET @year_to   = NULL;
SET @lang      = 'en';

SELECT c.movie_id, c.title, c.release_year, c.original_language,
       c.genres, c.directors, c.tmdb_score, c.vote_count
  FROM v_movie_cards c
 WHERE (@q IS NULL         OR c.title LIKE CONCAT('%', @q, '%'))
   AND (@year_from IS NULL OR c.release_year >= @year_from)
   AND (@year_to IS NULL   OR c.release_year <= @year_to)
   AND (@lang IS NULL      OR c.original_language = @lang)
   AND (@genre IS NULL OR EXISTS (
          SELECT 1
            FROM movie_genres mg
            JOIN genres g ON g.genre_id = mg.genre_id
           WHERE mg.movie_id = c.movie_id
             AND g.name = @genre))
 ORDER BY c.weighted_score DESC
 LIMIT 20;

-- ---------------------------------------------------------------------
-- Q2. Search by person: every film a person worked on, as cast or crew.
-- UNION ALL stacks two result sets with the same columns. ALL keeps
-- duplicates, which is correct here: someone who directed and wrote a
-- film appears once per credit.
-- ---------------------------------------------------------------------
SET @person = 'Christopher Nolan';

SELECT p.name, 'Cast' AS credit_type, mc.character_name AS role,
       m.title, YEAR(m.release_date) AS release_year
  FROM people p
  JOIN movie_cast mc ON mc.person_id = p.person_id
  JOIN movies m      ON m.movie_id   = mc.movie_id
 WHERE p.name LIKE CONCAT('%', @person, '%')
UNION ALL
SELECT p.name, 'Crew', CONCAT(cr.department, ': ', cr.job),
       m.title, YEAR(m.release_date)
  FROM people p
  JOIN movie_crew cr ON cr.person_id = p.person_id
  JOIN movies m      ON m.movie_id   = cr.movie_id
 WHERE p.name LIKE CONCAT('%', @person, '%')
 ORDER BY release_year, title, credit_type;


-- =====================================================================
-- MOVIE DETAIL
-- These are the SQL side of the "retrieve one movie with its cast and
-- crew" benchmark. MongoDB answers the same question with one
-- findOne({movie_id: ...}) on movie_features.
-- Column aliases `character` and `order` match the MongoDB field names,
-- so both databases return the same JSON shape to the front end.
-- =====================================================================
SET @movie_id = 19995;   -- Avatar

-- Q3a. Core fields and genres
SELECT * FROM v_movie_cards WHERE movie_id = @movie_id;

-- Q3b. Cast in billing order
SELECT p.person_id, p.name,
       mc.character_name AS `character`,
       mc.cast_order     AS `order`
  FROM movie_cast mc
  JOIN people p ON p.person_id = mc.person_id
 WHERE mc.movie_id = @movie_id
 ORDER BY mc.cast_order;

-- Q3c. Crew grouped by department
SELECT p.person_id, p.name, cr.department, cr.job
  FROM movie_crew cr
  JOIN people p ON p.person_id = cr.person_id
 WHERE cr.movie_id = @movie_id
 ORDER BY cr.department, cr.job, p.name;

-- Q3d. The whole movie as ONE JSON document, in one statement.
-- This is the fairest SQL counterpart to MongoDB's findOne: same output
-- shape, one round trip. The joins still happen; they are just hidden
-- inside the correlated subqueries.
SELECT JSON_OBJECT(
         'movie_id', m.movie_id,
         'title',    m.title,
         'release_date', m.release_date,
         'genres', (SELECT JSON_ARRAYAGG(g.name)
                      FROM movie_genres mg
                      JOIN genres g ON g.genre_id = mg.genre_id
                     WHERE mg.movie_id = m.movie_id),
         'cast',   (SELECT JSON_ARRAYAGG(JSON_OBJECT(
                             'person_id', p.person_id,
                             'name',      p.name,
                             'character', mc.character_name,
                             'order',     mc.cast_order))
                      FROM movie_cast mc
                      JOIN people p ON p.person_id = mc.person_id
                     WHERE mc.movie_id = m.movie_id),
         'crew',   (SELECT JSON_ARRAYAGG(JSON_OBJECT(
                             'person_id',  p.person_id,
                             'name',       p.name,
                             'department', cr.department,
                             'job',        cr.job))
                      FROM movie_crew cr
                      JOIN people p ON p.person_id = cr.person_id
                     WHERE cr.movie_id = m.movie_id)
       ) AS movie_document
  FROM movies m
 WHERE m.movie_id = @movie_id;


-- =====================================================================
-- REPORTS
-- =====================================================================

-- ---------------------------------------------------------------------
-- Q4. Top 10 films.
-- Q4a uses the weighted (Bayesian) score from v_movie_stats.
-- Q4b ranks by the raw average and shows why that is misleading:
-- obscure films with a handful of votes take the top places.
-- Put both outputs side by side in the report.
-- ---------------------------------------------------------------------
-- Q4a. Weighted score
SELECT movie_id, title, release_year, tmdb_score, vote_count, weighted_score
  FROM v_movie_stats
 WHERE weighted_score IS NOT NULL
 ORDER BY weighted_score DESC
 LIMIT 10;

-- Q4b. Naive average (for comparison only)
SELECT movie_id, title, release_year, tmdb_score, vote_count
  FROM v_movie_stats
 WHERE tmdb_score IS NOT NULL
 ORDER BY tmdb_score DESC, vote_count DESC
 LIMIT 10;

-- ---------------------------------------------------------------------
-- Q5. Top 5 films in one genre (or every genre: set @genre = NULL).
-- ---------------------------------------------------------------------
SET @genre = 'Animation';

SELECT genre, genre_rank, title, release_year, tmdb_score, vote_count, weighted_score
  FROM v_top_rated_per_genre
 WHERE genre_rank <= 5
   AND (@genre IS NULL OR genre = @genre)
 ORDER BY genre, genre_rank;

-- ---------------------------------------------------------------------
-- Q6. Average score by genre and decade, with subtotals.
-- WITH ROLLUP adds a subtotal row per genre (decade = NULL) and a grand
-- total row (both NULL). GROUPING() tells a real NULL from a subtotal
-- NULL, so the labels are exact.
-- The decade is computed in a derived table first, so the outer query
-- groups by plain columns (required by ONLY_FULL_GROUP_BY with ROLLUP).
-- It works on films, not on v_genre_year_stats, so each average is over
-- films rather than over yearly averages.
-- ---------------------------------------------------------------------
SELECT
  IF(GROUPING(genre),  'All genres',  genre)                          AS genre,
  IF(GROUPING(decade), 'All decades', CONCAT(decade, 's'))            AS decade,
  COUNT(*)                                                            AS films,
  ROUND(AVG(tmdb_score), 2)                                           AS avg_tmdb_score,
  ROUND(SUM(revenue) / 1e9, 2)                                        AS revenue_billions
FROM (
  SELECT g.name                                         AS genre,
         FLOOR(YEAR(m.release_date) / 10) * 10          AS decade,
         CASE WHEN m.vote_count > 0 THEN m.vote_average END AS tmdb_score,
         m.revenue
    FROM movies m
    JOIN movie_genres mg ON mg.movie_id = m.movie_id
    JOIN genres g        ON g.genre_id  = mg.genre_id
   WHERE m.release_date >= '1980-01-01'
     AND g.name IN ('Action', 'Comedy', 'Drama', 'Horror', 'Science Fiction')
) t
GROUP BY genre, decade WITH ROLLUP;

-- ---------------------------------------------------------------------
-- Q7. Most active raters.
-- DENSE_RANK gives tied users the same rank with no gaps (1, 2, 2, 3).
-- ---------------------------------------------------------------------
SELECT DENSE_RANK() OVER (ORDER BY ratings_given DESC) AS activity_rank,
       username, display_name, ratings_given, avg_rating_given,
       latest_rating_at
  FROM v_user_activity
 ORDER BY activity_rank, username;

-- ---------------------------------------------------------------------
-- Q8a. Highest-grossing films with profit and return on investment.
-- Films with unknown budget (NULL after cleaning) are excluded from ROI
-- instead of producing a divide-by-zero or a fake infinite return.
-- ---------------------------------------------------------------------
SELECT movie_id, title, release_year,
       ROUND(revenue / 1e6)                  AS revenue_millions,
       ROUND(budget  / 1e6)                  AS budget_millions,
       ROUND((revenue - budget) / 1e6)       AS profit_millions,
       ROUND(revenue / budget, 1)            AS roi_multiple
  FROM v_movie_stats
 WHERE revenue IS NOT NULL
   AND budget  IS NOT NULL
 ORDER BY revenue DESC
 LIMIT 10;

-- ---------------------------------------------------------------------
-- Q8b. The highest-grossing film of each year since 2000.
-- ROW_NUMBER() restarts at 1 for every year (PARTITION BY), then the
-- outer query keeps only number 1.
-- ---------------------------------------------------------------------
SELECT release_year, title, ROUND(revenue / 1e6) AS revenue_millions
  FROM (
    SELECT release_year, title, revenue,
           ROW_NUMBER() OVER (PARTITION BY release_year
                              ORDER BY revenue DESC) AS rn
      FROM v_movie_stats
     WHERE revenue IS NOT NULL
       AND release_year >= 2000
  ) ranked
 WHERE rn = 1
 ORDER BY release_year;


-- =====================================================================
-- ANALYTICS
-- =====================================================================

-- ---------------------------------------------------------------------
-- Q9. Films that beat their genre's average score by at least 2.0,
-- with at least 2,000 votes.
-- Correlated subquery: the inner SELECT is re-evaluated for each row of
-- the outer query, using the outer row's genre_id.
-- ---------------------------------------------------------------------
SELECT g.name AS genre, m.title, YEAR(m.release_date) AS release_year,
       m.vote_average AS tmdb_score,
       ROUND((SELECT AVG(m2.vote_average)
                FROM movies m2
                JOIN movie_genres mg2 ON mg2.movie_id = m2.movie_id
               WHERE mg2.genre_id = mg.genre_id
                 AND m2.vote_count > 0), 2) AS genre_avg
  FROM movies m
  JOIN movie_genres mg ON mg.movie_id = m.movie_id
  JOIN genres g        ON g.genre_id  = mg.genre_id
 WHERE m.vote_count >= 2000
   AND m.vote_average >= 2.0 + (
         SELECT AVG(m2.vote_average)
           FROM movies m2
           JOIN movie_genres mg2 ON mg2.movie_id = m2.movie_id
          WHERE mg2.genre_id = mg.genre_id
            AND m2.vote_count > 0)
 ORDER BY genre, tmdb_score DESC;

-- ---------------------------------------------------------------------
-- Q10. Best directors with at least 5 films, by average weighted score.
-- ---------------------------------------------------------------------
SELECT director, films_directed, avg_tmdb_score, avg_weighted_score,
       ROUND(total_revenue / 1e9, 2) AS revenue_billions,
       first_film_year, latest_film_year
  FROM v_director_stats
 WHERE films_directed >= 5
 ORDER BY avg_weighted_score DESC
 LIMIT 15;

-- ---------------------------------------------------------------------
-- Q11. Actor pairs who most often share top billing.
-- Self-join: movie_cast is joined to itself on the same film.
-- a.person_id < b.person_id keeps each pair once (not A-B and B-A) and
-- drops an actor paired with themselves.
-- cast_order < 5 limits this to the top five billed actors per film.
-- ---------------------------------------------------------------------
SELECT pa.name AS actor_1, pb.name AS actor_2, COUNT(*) AS films_together
  FROM movie_cast a
  JOIN movie_cast b ON b.movie_id  = a.movie_id
                   AND a.person_id < b.person_id
  JOIN people pa    ON pa.person_id = a.person_id
  JOIN people pb    ON pb.person_id = b.person_id
 WHERE a.cast_order < 5
   AND b.cast_order < 5
 GROUP BY pa.name, pb.name
HAVING COUNT(*) >= 4
 ORDER BY films_together DESC, actor_1
 LIMIT 15;

-- ---------------------------------------------------------------------
-- Q12. Actors who worked with the same director at least 4 times.
-- Joins the cast table to the crew table on the same film.
-- ---------------------------------------------------------------------
SELECT pd.name AS director, pa.name AS actor,
       COUNT(DISTINCT mc.movie_id) AS films_together,
       GROUP_CONCAT(DISTINCT m.title ORDER BY m.release_date SEPARATOR '; ') AS films
  FROM movie_crew cr
  JOIN movie_cast mc ON mc.movie_id = cr.movie_id
                    AND mc.person_id <> cr.person_id
  JOIN people pd     ON pd.person_id = cr.person_id
  JOIN people pa     ON pa.person_id = mc.person_id
  JOIN movies m      ON m.movie_id   = cr.movie_id
 WHERE cr.job = 'Director'
 GROUP BY pd.name, pa.name
HAVING COUNT(DISTINCT mc.movie_id) >= 4
 ORDER BY films_together DESC, director
 LIMIT 15;

-- ---------------------------------------------------------------------
-- Q13. Year-on-year trend, 1995-2015.
-- The CTE computes one row per year; LAG() reads the previous year's
-- row so the change can be calculated without a self-join.
-- ---------------------------------------------------------------------
WITH yearly AS (
  SELECT YEAR(release_date)                                        AS yr,
         COUNT(*)                                                  AS films,
         ROUND(AVG(CASE WHEN vote_count > 0 THEN vote_average END), 2) AS avg_score,
         ROUND(SUM(revenue) / 1e9, 2)                              AS revenue_billions
    FROM movies
   WHERE YEAR(release_date) BETWEEN 1995 AND 2015
   GROUP BY YEAR(release_date)
)
SELECT yr, films, avg_score, revenue_billions,
       films - LAG(films) OVER (ORDER BY yr)                       AS films_change,
       ROUND(revenue_billions
             - LAG(revenue_billions) OVER (ORDER BY yr), 2)        AS revenue_change_billions
  FROM yearly
 ORDER BY yr;

-- ---------------------------------------------------------------------
-- Q14. Each user's favourite genre: the genre they rate highest, among
-- genres they have rated at least 3 times.
-- Step 1 (CTE) aggregates per user and genre; step 2 ranks genres
-- inside each user with ROW_NUMBER(); step 3 keeps rank 1.
-- This is the input a simple content-based recommender would use.
-- ---------------------------------------------------------------------
WITH user_genre AS (
  SELECT r.user_id, g.name AS genre,
         COUNT(*)               AS ratings_in_genre,
         AVG(r.rating_value)    AS avg_rating
    FROM ratings r
    JOIN movie_genres mg ON mg.movie_id = r.movie_id
    JOIN genres g        ON g.genre_id  = mg.genre_id
   GROUP BY r.user_id, g.name
  HAVING COUNT(*) >= 3
),
ranked AS (
  SELECT ug.*,
         ROW_NUMBER() OVER (PARTITION BY ug.user_id
                            ORDER BY ug.avg_rating DESC,
                                     ug.ratings_in_genre DESC) AS rn
    FROM user_genre ug
)
SELECT u.username, r.genre AS favourite_genre,
       r.ratings_in_genre, ROUND(r.avg_rating, 2) AS avg_rating
  FROM ranked r
  JOIN users u ON u.user_id = r.user_id
 WHERE r.rn = 1
 ORDER BY u.username;
