"""
INF2003 Group Project - Team 37
webapp/queries.py : the SQL the UI runs

Taken from database/mysql/06_queries.sql and 05_crud.sql, with the
@session variables replaced by %s placeholders (never string
concatenation, so user input cannot inject SQL). A literal % inside a
query must be written %% because %s is the placeholder syntax.
"""

# ---------------------------------------------------------------------
# Search (Q1, Q2)
# ---------------------------------------------------------------------
BROWSE = """
SELECT c.movie_id, c.title, c.release_year, c.original_language,
       c.genres, c.directors, c.tmdb_score, c.vote_count,
       c.user_avg_rating, c.user_rating_count
  FROM v_movie_cards c
 WHERE (%(q)s IS NULL         OR c.title LIKE CONCAT('%%', %(q)s, '%%'))
   AND (%(year_from)s IS NULL OR c.release_year >= %(year_from)s)
   AND (%(year_to)s IS NULL   OR c.release_year <= %(year_to)s)
   AND (%(lang)s IS NULL      OR c.original_language = %(lang)s)
   AND (%(genre)s IS NULL OR EXISTS (
          SELECT 1
            FROM movie_genres mg
            JOIN genres g ON g.genre_id = mg.genre_id
           WHERE mg.movie_id = c.movie_id
             AND g.name = %(genre)s))
 ORDER BY c.weighted_score DESC, c.title
 LIMIT %(limit)s OFFSET %(offset)s
"""

GENRES = "SELECT name FROM genres ORDER BY name"

LANGUAGES = """
SELECT original_language AS code, COUNT(*) AS films
  FROM movies
 WHERE original_language IS NOT NULL
 GROUP BY original_language
 ORDER BY films DESC
"""

PEOPLE_SEARCH = """
SELECT p.person_id, p.name, 'Cast' AS credit_type, mc.character_name AS role,
       m.movie_id, m.title, YEAR(m.release_date) AS release_year
  FROM people p
  JOIN movie_cast mc ON mc.person_id = p.person_id
  JOIN movies m      ON m.movie_id   = mc.movie_id
 WHERE p.name LIKE CONCAT('%%', %s, '%%')
UNION ALL
SELECT p.person_id, p.name, 'Crew', CONCAT(cr.department, ': ', cr.job),
       m.movie_id, m.title, YEAR(m.release_date)
  FROM people p
  JOIN movie_crew cr ON cr.person_id = p.person_id
  JOIN movies m      ON m.movie_id   = cr.movie_id
 WHERE p.name LIKE CONCAT('%%', %s, '%%')
 ORDER BY name, release_year DESC, title, credit_type
 LIMIT 300
"""

# ---------------------------------------------------------------------
# Movie detail (Q3a-c)
# ---------------------------------------------------------------------
MOVIE = """
SELECT c.*, m.original_title, m.release_date, m.budget, m.revenue,
       m.popularity, m.status
  FROM v_movie_cards c
  JOIN movies m ON m.movie_id = c.movie_id
 WHERE c.movie_id = %s
"""

MOVIE_CAST = """
SELECT p.person_id, p.name,
       mc.character_name AS `character`,
       mc.cast_order     AS `order`
  FROM movie_cast mc
  JOIN people p ON p.person_id = mc.person_id
 WHERE mc.movie_id = %s
 ORDER BY mc.cast_order
"""

MOVIE_CREW = """
SELECT p.person_id, p.name, cr.department, cr.job
  FROM movie_crew cr
  JOIN people p ON p.person_id = cr.person_id
 WHERE cr.movie_id = %s
 ORDER BY cr.department, cr.job, p.name
"""

MOVIE_RATINGS = """
SELECT r.user_id, u.username, u.display_name, r.rating_value,
       r.rated_at, r.updated_at
  FROM ratings r
  JOIN users u ON u.user_id = r.user_id
 WHERE r.movie_id = %s
 ORDER BY COALESCE(r.updated_at, r.rated_at) DESC
"""

TITLES_FOR_IDS = """
SELECT movie_id, title, release_year, genres, tmdb_score
  FROM v_movie_cards
 WHERE movie_id IN ({placeholders})
"""

# ---------------------------------------------------------------------
# Users and ratings (05_crud.sql)
# ---------------------------------------------------------------------
USERS = """
SELECT user_id, username, display_name, ratings_given, avg_rating_given,
       latest_rating_at
  FROM v_user_activity
 ORDER BY username
"""

USER = "SELECT user_id, username, display_name, created_at FROM users WHERE user_id = %s"

USER_CREATE = "INSERT INTO users (username, display_name) VALUES (%s, %s)"
USER_UPDATE = "UPDATE users SET display_name = %s WHERE user_id = %s"
USER_DELETE = "DELETE FROM users WHERE user_id = %s"

USER_RATINGS = """
SELECT r.movie_id, m.title, YEAR(m.release_date) AS release_year,
       r.rating_value, r.rated_at, r.updated_at
  FROM ratings r
  JOIN movies m ON m.movie_id = r.movie_id
 WHERE r.user_id = %s
 ORDER BY COALESCE(r.updated_at, r.rated_at) DESC
"""

RATING_GET = "SELECT rating_value FROM ratings WHERE user_id = %s AND movie_id = %s"
RATING_INSERT = "INSERT INTO ratings (user_id, movie_id, rating_value) VALUES (%s, %s, %s)"
RATING_UPDATE = "UPDATE ratings SET rating_value = %s WHERE user_id = %s AND movie_id = %s"
RATING_DELETE = "DELETE FROM ratings WHERE user_id = %s AND movie_id = %s"

# Q14 for a single user
USER_FAVOURITE_GENRE = """
SELECT g.name AS genre, COUNT(*) AS ratings_in_genre,
       ROUND(AVG(r.rating_value), 2) AS avg_rating
  FROM ratings r
  JOIN movie_genres mg ON mg.movie_id = r.movie_id
  JOIN genres g        ON g.genre_id  = mg.genre_id
 WHERE r.user_id = %s
 GROUP BY g.name
HAVING COUNT(*) >= 3
 ORDER BY avg_rating DESC, ratings_in_genre DESC
 LIMIT 3
"""

# ---------------------------------------------------------------------
# Reports (06_queries.sql). Each entry: title, SQL feature, query.
# ---------------------------------------------------------------------
SQL_REPORTS = {
    "top_weighted": (
        "Top 10 films by weighted score", "Q4a - Bayesian average (view)",
        """SELECT movie_id, title, release_year, tmdb_score, vote_count, weighted_score
             FROM v_movie_stats
            WHERE weighted_score IS NOT NULL
            ORDER BY weighted_score DESC
            LIMIT 10"""),
    "top_naive": (
        "Top 10 films by raw average (for comparison)", "Q4b - why raw averages mislead",
        """SELECT movie_id, title, release_year, tmdb_score, vote_count
             FROM v_movie_stats
            WHERE tmdb_score IS NOT NULL
            ORDER BY tmdb_score DESC, vote_count DESC
            LIMIT 10"""),
    "top_per_genre": (
        "Top 5 films in each genre", "Q5 - RANK() OVER (PARTITION BY) view",
        """SELECT genre, genre_rank, movie_id, title, release_year, tmdb_score, vote_count, weighted_score
             FROM v_top_rated_per_genre
            WHERE genre_rank <= 5
            ORDER BY genre, genre_rank"""),
    "genre_decade": (
        "Average score by genre and decade, with subtotals", "Q6 - GROUP BY WITH ROLLUP, GROUPING()",
        """SELECT
             IF(GROUPING(genre),  'All genres',  genre)               AS genre,
             IF(GROUPING(decade), 'All decades', CONCAT(decade, 's')) AS decade,
             COUNT(*)                                                 AS films,
             ROUND(AVG(tmdb_score), 2)                                AS avg_tmdb_score,
             ROUND(SUM(revenue) / 1e9, 2)                             AS revenue_billions
           FROM (
             SELECT g.name AS genre,
                    FLOOR(YEAR(m.release_date) / 10) * 10 AS decade,
                    CASE WHEN m.vote_count > 0 THEN m.vote_average END AS tmdb_score,
                    m.revenue
               FROM movies m
               JOIN movie_genres mg ON mg.movie_id = m.movie_id
               JOIN genres g        ON g.genre_id  = mg.genre_id
              WHERE m.release_date >= '1980-01-01'
                AND g.name IN ('Action', 'Comedy', 'Drama', 'Horror', 'Science Fiction')
           ) t
           GROUP BY genre, decade WITH ROLLUP"""),
    "active_raters": (
        "Most active raters", "Q7 - DENSE_RANK()",
        """SELECT DENSE_RANK() OVER (ORDER BY ratings_given DESC) AS activity_rank,
                  username, display_name, ratings_given, avg_rating_given, latest_rating_at
             FROM v_user_activity
            ORDER BY activity_rank, username"""),
    "grossing": (
        "Highest-grossing films with profit and ROI", "Q8a - computed columns",
        """SELECT movie_id, title, release_year,
                  ROUND(revenue / 1e6)            AS revenue_millions,
                  ROUND(budget  / 1e6)            AS budget_millions,
                  ROUND((revenue - budget) / 1e6) AS profit_millions,
                  ROUND(revenue / budget, 1)      AS roi_multiple
             FROM v_movie_stats
            WHERE revenue IS NOT NULL AND budget IS NOT NULL
            ORDER BY revenue DESC
            LIMIT 10"""),
    "grossing_per_year": (
        "Highest-grossing film of each year since 2000", "Q8b - ROW_NUMBER() OVER (PARTITION BY)",
        """SELECT release_year, movie_id, title, ROUND(revenue / 1e6) AS revenue_millions
             FROM (
               SELECT release_year, movie_id, title, revenue,
                      ROW_NUMBER() OVER (PARTITION BY release_year ORDER BY revenue DESC) AS rn
                 FROM v_movie_stats
                WHERE revenue IS NOT NULL AND release_year >= 2000
             ) ranked
            WHERE rn = 1
            ORDER BY release_year"""),
    "above_genre_avg": (
        "Films far above their genre's average", "Q9 - correlated subquery",
        """SELECT g.name AS genre, m.movie_id, m.title, YEAR(m.release_date) AS release_year,
                  m.vote_average AS tmdb_score,
                  ROUND((SELECT AVG(m2.vote_average)
                           FROM movies m2
                           JOIN movie_genres mg2 ON mg2.movie_id = m2.movie_id
                          WHERE mg2.genre_id = mg.genre_id AND m2.vote_count > 0), 2) AS genre_avg
             FROM movies m
             JOIN movie_genres mg ON mg.movie_id = m.movie_id
             JOIN genres g        ON g.genre_id  = mg.genre_id
            WHERE m.vote_count >= 2000
              AND m.vote_average >= 2.0 + (
                    SELECT AVG(m2.vote_average)
                      FROM movies m2
                      JOIN movie_genres mg2 ON mg2.movie_id = m2.movie_id
                     WHERE mg2.genre_id = mg.genre_id AND m2.vote_count > 0)
            ORDER BY genre, tmdb_score DESC"""),
    "directors": (
        "Best directors (at least 5 films)", "Q10 - view + HAVING-style filter",
        """SELECT director, films_directed, avg_tmdb_score, avg_weighted_score,
                  ROUND(total_revenue / 1e9, 2) AS revenue_billions,
                  first_film_year, latest_film_year
             FROM v_director_stats
            WHERE films_directed >= 5
            ORDER BY avg_weighted_score DESC
            LIMIT 15"""),
    "costars": (
        "Actor pairs who most often share top billing", "Q11 - self-join",
        """SELECT pa.name AS actor_1, pb.name AS actor_2, COUNT(*) AS films_together
             FROM movie_cast a
             JOIN movie_cast b ON b.movie_id = a.movie_id AND a.person_id < b.person_id
             JOIN people pa    ON pa.person_id = a.person_id
             JOIN people pb    ON pb.person_id = b.person_id
            WHERE a.cast_order < 5 AND b.cast_order < 5
            GROUP BY pa.name, pb.name
           HAVING COUNT(*) >= 4
            ORDER BY films_together DESC, actor_1
            LIMIT 15"""),
    "partnerships": (
        "Actors who worked with the same director 4+ times", "Q12 - join cast to crew",
        """SELECT pd.name AS director, pa.name AS actor,
                  COUNT(DISTINCT mc.movie_id) AS films_together,
                  GROUP_CONCAT(DISTINCT m.title ORDER BY m.release_date SEPARATOR '; ') AS films
             FROM movie_crew cr
             JOIN movie_cast mc ON mc.movie_id = cr.movie_id AND mc.person_id <> cr.person_id
             JOIN people pd     ON pd.person_id = cr.person_id
             JOIN people pa     ON pa.person_id = mc.person_id
             JOIN movies m      ON m.movie_id   = cr.movie_id
            WHERE cr.job = 'Director'
            GROUP BY pd.name, pa.name
           HAVING COUNT(DISTINCT mc.movie_id) >= 4
            ORDER BY films_together DESC, director
            LIMIT 15"""),
    "yearly_trend": (
        "Year-on-year trend, 1995-2015", "Q13 - CTE + LAG()",
        """WITH yearly AS (
             SELECT YEAR(release_date) AS yr, COUNT(*) AS films,
                    ROUND(AVG(CASE WHEN vote_count > 0 THEN vote_average END), 2) AS avg_score,
                    ROUND(SUM(revenue) / 1e9, 2) AS revenue_billions
               FROM movies
              WHERE YEAR(release_date) BETWEEN 1995 AND 2015
              GROUP BY YEAR(release_date)
           )
           SELECT yr, films, avg_score, revenue_billions,
                  films - LAG(films) OVER (ORDER BY yr) AS films_change,
                  ROUND(revenue_billions - LAG(revenue_billions) OVER (ORDER BY yr), 2) AS revenue_change_billions
             FROM yearly
            ORDER BY yr"""),
    "favourite_genre": (
        "Each user's favourite genre", "Q14 - CTE + ROW_NUMBER() over an aggregate",
        """WITH user_genre AS (
             SELECT r.user_id, g.name AS genre, COUNT(*) AS ratings_in_genre,
                    AVG(r.rating_value) AS avg_rating
               FROM ratings r
               JOIN movie_genres mg ON mg.movie_id = r.movie_id
               JOIN genres g        ON g.genre_id  = mg.genre_id
              GROUP BY r.user_id, g.name
             HAVING COUNT(*) >= 3
           ),
           ranked AS (
             SELECT ug.*, ROW_NUMBER() OVER (PARTITION BY ug.user_id
                                             ORDER BY ug.avg_rating DESC, ug.ratings_in_genre DESC) AS rn
               FROM user_genre ug
           )
           SELECT u.username, r.genre AS favourite_genre, r.ratings_in_genre,
                  ROUND(r.avg_rating, 2) AS avg_rating
             FROM ranked r
             JOIN users u ON u.user_id = r.user_id
            WHERE r.rn = 1
            ORDER BY u.username"""),
}
