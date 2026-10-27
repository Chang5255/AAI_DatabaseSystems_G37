-- =====================================================================
-- INF2003 Group Project - Team 37
-- 02_load.sql : bulk load the cleaned TMDB CSVs into moviedb
--
-- Run AFTER 01_schema.sql.
--
-- BEFORE RUNNING:
--   1. Edit the 8 file paths below to point at your cleaned/ folder.
--      Use FORWARD slashes even on Windows:
--        'C:/path/to/AAI_DatabaseSystems_G37/data/cleaned/movies.csv'
--   2. Enable local file loading (see user manual / step 3 of the guide):
--        SET GLOBAL local_infile = 1;
--      and connect with local-infile enabled on the client side.
--
-- Load order matters: parents before children, or the foreign keys
-- will reject the rows.
-- Nulls in the CSVs are written as \N, which LOAD DATA reads as NULL.
-- =====================================================================

USE moviedb;

-- 1. movies ------------------------------------------------------------
LOAD DATA LOCAL INFILE 'CHANGE/ME/cleaned/movies.csv'
INTO TABLE movies
CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' ENCLOSED BY '"'
LINES TERMINATED BY '\n'
IGNORE 1 LINES
(movie_id, title, original_title, original_language, release_date,
 runtime, budget, revenue, popularity, vote_average, vote_count, status);

-- 2. genres ------------------------------------------------------------
LOAD DATA LOCAL INFILE 'CHANGE/ME/cleaned/genres.csv'
INTO TABLE genres
CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' ENCLOSED BY '"'
LINES TERMINATED BY '\n'
IGNORE 1 LINES
(genre_id, name);

-- 3. people ------------------------------------------------------------
LOAD DATA LOCAL INFILE 'CHANGE/ME/cleaned/people.csv'
INTO TABLE people
CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' ENCLOSED BY '"'
LINES TERMINATED BY '\n'
IGNORE 1 LINES
(person_id, name, gender);

-- 4. users -------------------------------------------------------------
LOAD DATA LOCAL INFILE 'CHANGE/ME/cleaned/users.csv'
INTO TABLE users
CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' ENCLOSED BY '"'
LINES TERMINATED BY '\n'
IGNORE 1 LINES
(user_id, username, display_name, created_at);

-- 5. movie_genres (needs movies + genres) -------------------------------
LOAD DATA LOCAL INFILE 'CHANGE/ME/cleaned/movie_genres.csv'
INTO TABLE movie_genres
CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' ENCLOSED BY '"'
LINES TERMINATED BY '\n'
IGNORE 1 LINES
(movie_id, genre_id);

-- 6. movie_cast (needs movies + people) ---------------------------------
LOAD DATA LOCAL INFILE 'CHANGE/ME/cleaned/movie_cast.csv'
INTO TABLE movie_cast
CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' ENCLOSED BY '"'
LINES TERMINATED BY '\n'
IGNORE 1 LINES
(credit_id, movie_id, person_id, character_name, cast_order);

-- 7. movie_crew (needs movies + people) ---------------------------------
LOAD DATA LOCAL INFILE 'CHANGE/ME/cleaned/movie_crew.csv'
INTO TABLE movie_crew
CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' ENCLOSED BY '"'
LINES TERMINATED BY '\n'
IGNORE 1 LINES
(credit_id, movie_id, person_id, department, job);

-- 8. ratings (needs users + movies) -------------------------------------
LOAD DATA LOCAL INFILE 'CHANGE/ME/cleaned/ratings.csv'
INTO TABLE ratings
CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' ENCLOSED BY '"'
LINES TERMINATED BY '\n'
IGNORE 1 LINES
(user_id, movie_id, rating_value, rated_at, updated_at);

-- Row counts after loading --------------------------------------------
SELECT 'movies' AS table_name, COUNT(*) AS rows_loaded, 4803 AS expected FROM movies
UNION ALL SELECT 'genres',       COUNT(*),  20     FROM genres
UNION ALL SELECT 'movie_genres', COUNT(*),  12160  FROM movie_genres
UNION ALL SELECT 'people',       COUNT(*),  104842 FROM people
UNION ALL SELECT 'movie_cast',   COUNT(*),  106257 FROM movie_cast
UNION ALL SELECT 'movie_crew',   COUNT(*),  129581 FROM movie_crew
UNION ALL SELECT 'users',        COUNT(*),  12     FROM users
UNION ALL SELECT 'ratings',      COUNT(*),  784    FROM ratings;
