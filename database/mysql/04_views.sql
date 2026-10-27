-- =====================================================================
-- INF2003 Group Project - Team 37
-- 04_views.sql : reusable views for browsing, reports and analytics
--
-- Run AFTER 02_load.sql. Re-runnable (CREATE OR REPLACE).
--
-- Views:
--   v_movie_stats          one row per film: TMDB score, weighted score,
--                          our users' average rating
--   v_movie_cards          one row per film for browse/search pages:
--                          genres and directors as lists
--   v_top_rated_per_genre  top 10 films in every genre (window function)
--   v_genre_year_stats     film count, scores and revenue per genre-year
--   v_director_stats       per-director totals and averages
--   v_user_activity        rating activity per user
-- =====================================================================

USE moviedb;

-- ---------------------------------------------------------------------
-- v_movie_stats
-- * TMDB stores vote_average = 0 for films with no votes. That is
--   "no score", not a score of zero, so it is returned as NULL.
-- * weighted_score is the IMDb Bayesian average:
--       WR = v/(v+m) * R  +  m/(v+m) * C
--   v = film's vote_count, R = film's vote_average,
--   C = mean vote_average across all voted films,
--   m = 90th-percentile vote_count (a film needs about this many votes
--       before its own average outweighs the global mean).
--   This stops a film with one 10/10 vote topping the chart.
-- * User ratings are aggregated in a derived table BEFORE the join, and
--   joined with LEFT JOIN, so unrated films are kept (user_rating_count
--   = 0) and the join cannot multiply rows.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_movie_stats AS
WITH params AS (
  SELECT
    (SELECT AVG(vote_average) FROM movies WHERE vote_count > 0) AS c_mean,
    (SELECT vote_count
       FROM (SELECT vote_count,
                    PERCENT_RANK() OVER (ORDER BY vote_count) AS pr
               FROM movies WHERE vote_count > 0) ranked
      WHERE pr >= 0.9
      ORDER BY pr
      LIMIT 1)                                                AS m_min_votes
),
user_ratings AS (
  SELECT movie_id,
         AVG(rating_value) AS user_avg_rating,
         COUNT(*)          AS user_rating_count
    FROM ratings
   GROUP BY movie_id
)
SELECT
  m.movie_id,
  m.title,
  YEAR(m.release_date)                                   AS release_year,
  m.original_language,
  m.runtime,
  m.budget,
  m.revenue,
  m.popularity,
  CASE WHEN m.vote_count > 0 THEN m.vote_average END     AS tmdb_score,
  m.vote_count,
  CASE WHEN m.vote_count > 0 THEN
    ROUND(  m.vote_count / (m.vote_count + p.m_min_votes) * m.vote_average
          + p.m_min_votes / (m.vote_count + p.m_min_votes) * p.c_mean, 3)
  END                                                    AS weighted_score,
  ROUND(ur.user_avg_rating, 2)                           AS user_avg_rating,
  COALESCE(ur.user_rating_count, 0)                      AS user_rating_count
FROM movies m
CROSS JOIN params p
LEFT JOIN user_ratings ur ON ur.movie_id = m.movie_id;

-- ---------------------------------------------------------------------
-- v_movie_cards
-- One row per film with its genres and directors flattened into
-- comma-separated lists, for the browse and search pages.
-- Genres and directors are each aggregated separately first; joining
-- both to movies before grouping would produce genre x director
-- duplicates.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_movie_cards AS
SELECT
  s.movie_id,
  s.title,
  s.release_year,
  s.original_language,
  s.runtime,
  g.genres,
  d.directors,
  s.tmdb_score,
  s.vote_count,
  s.weighted_score,
  s.user_avg_rating,
  s.user_rating_count
FROM v_movie_stats s
LEFT JOIN (
  SELECT mg.movie_id,
         GROUP_CONCAT(gn.name ORDER BY gn.name SEPARATOR ', ') AS genres
    FROM movie_genres mg
    JOIN genres gn ON gn.genre_id = mg.genre_id
   GROUP BY mg.movie_id
) g ON g.movie_id = s.movie_id
LEFT JOIN (
  SELECT cr.movie_id,
         GROUP_CONCAT(DISTINCT p.name ORDER BY p.name SEPARATOR ', ') AS directors
    FROM movie_crew cr
    JOIN people p ON p.person_id = cr.person_id
   WHERE cr.job = 'Director'
   GROUP BY cr.movie_id
) d ON d.movie_id = s.movie_id;

-- ---------------------------------------------------------------------
-- v_top_rated_per_genre
-- Top 10 films in each genre by weighted score.
-- RANK() OVER (PARTITION BY ...) is a window function: it ranks films
-- within each genre without collapsing rows like GROUP BY would.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_top_rated_per_genre AS
SELECT *
FROM (
  SELECT
    gn.name                                              AS genre,
    RANK() OVER (PARTITION BY gn.genre_id
                 ORDER BY s.weighted_score DESC)         AS genre_rank,
    s.movie_id,
    s.title,
    s.release_year,
    s.tmdb_score,
    s.vote_count,
    s.weighted_score
  FROM v_movie_stats s
  JOIN movie_genres mg ON mg.movie_id = s.movie_id
  JOIN genres gn       ON gn.genre_id = mg.genre_id
  WHERE s.weighted_score IS NOT NULL
) ranked
WHERE genre_rank <= 10;

-- ---------------------------------------------------------------------
-- v_genre_year_stats
-- Film count, average TMDB score, average user rating and revenue for
-- every genre in every release year. Feeds the "average rating by genre
-- and year" report.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_genre_year_stats AS
SELECT
  gn.name                                AS genre,
  s.release_year,
  COUNT(*)                               AS film_count,
  ROUND(AVG(s.tmdb_score), 2)            AS avg_tmdb_score,
  ROUND(AVG(s.user_avg_rating), 2)       AS avg_user_rating,
  SUM(s.revenue)                         AS total_revenue,
  ROUND(AVG(s.runtime))                  AS avg_runtime
FROM v_movie_stats s
JOIN movie_genres mg ON mg.movie_id = s.movie_id
JOIN genres gn       ON gn.genre_id = mg.genre_id
WHERE s.release_year IS NOT NULL
GROUP BY gn.name, s.release_year;

-- ---------------------------------------------------------------------
-- v_director_stats
-- One row per director. A director can be credited twice on one film
-- (e.g. two Director rows), so films are counted with DISTINCT.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_director_stats AS
SELECT
  p.person_id,
  p.name                                 AS director,
  COUNT(DISTINCT s.movie_id)             AS films_directed,
  ROUND(AVG(s.tmdb_score), 2)            AS avg_tmdb_score,
  ROUND(AVG(s.weighted_score), 3)        AS avg_weighted_score,
  SUM(s.revenue)                         AS total_revenue,
  MIN(s.release_year)                    AS first_film_year,
  MAX(s.release_year)                    AS latest_film_year
FROM (
  SELECT DISTINCT movie_id, person_id
    FROM movie_crew
   WHERE job = 'Director'
) d
JOIN people p        ON p.person_id = d.person_id
JOIN v_movie_stats s ON s.movie_id  = d.movie_id
GROUP BY p.person_id, p.name;

-- ---------------------------------------------------------------------
-- v_user_activity
-- Rating activity per user. LEFT JOIN keeps users who have not rated
-- anything yet (ratings_given = 0).
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_user_activity AS
SELECT
  u.user_id,
  u.username,
  u.display_name,
  COUNT(r.movie_id)                      AS ratings_given,
  ROUND(AVG(r.rating_value), 2)          AS avg_rating_given,
  MIN(r.rated_at)                        AS first_rating_at,
  MAX(r.rated_at)                        AS latest_rating_at
FROM users u
LEFT JOIN ratings r ON r.user_id = u.user_id
GROUP BY u.user_id, u.username, u.display_name;

-- ---------------------------------------------------------------------
-- Check: list the views and preview each one
-- ---------------------------------------------------------------------
SELECT TABLE_NAME AS view_name
  FROM information_schema.VIEWS
 WHERE TABLE_SCHEMA = 'moviedb'
 ORDER BY TABLE_NAME;

SELECT * FROM v_movie_stats          ORDER BY weighted_score DESC LIMIT 5;
SELECT * FROM v_movie_cards          WHERE movie_id = 19995;
SELECT * FROM v_top_rated_per_genre  WHERE genre = 'Science Fiction';
SELECT * FROM v_genre_year_stats     WHERE genre = 'Action' ORDER BY release_year DESC LIMIT 5;
SELECT * FROM v_director_stats       WHERE films_directed >= 5 ORDER BY avg_weighted_score DESC LIMIT 5;
SELECT * FROM v_user_activity        ORDER BY ratings_given DESC;
