-- =====================================================================
-- INF2003 Group Project - Team 37
-- Movie Discovery / Ratings System
-- 01_schema.sql : database and table definitions (MySQL 8.0.16+)
--
-- Run first. Re-runnable: drops and recreates the whole database.
-- =====================================================================

DROP DATABASE IF EXISTS moviedb;
CREATE DATABASE moviedb
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;
USE moviedb;

-- ---------------------------------------------------------------------
-- movies : one row per film. movie_id is the TMDB id and is also the
-- shared key with the MongoDB collections.
-- ---------------------------------------------------------------------
CREATE TABLE movies (
  movie_id          INT           NOT NULL,
  title             VARCHAR(255)  NOT NULL,
  original_title    VARCHAR(255)  NULL,
  original_language CHAR(2)       NULL,
  release_date      DATE          NULL,
  runtime           SMALLINT      NULL,
  budget            BIGINT        NULL,
  revenue           BIGINT        NULL,
  popularity        DECIMAL(10,4) NULL,
  vote_average      DECIMAL(3,1)  NULL,
  vote_count        INT           NULL,
  status            VARCHAR(20)   NULL,
  PRIMARY KEY (movie_id),
  CONSTRAINT chk_movies_runtime CHECK (runtime IS NULL OR runtime > 0),
  CONSTRAINT chk_movies_budget  CHECK (budget  IS NULL OR budget  >= 0),
  CONSTRAINT chk_movies_revenue CHECK (revenue IS NULL OR revenue >= 0),
  CONSTRAINT chk_movies_vote    CHECK (vote_average IS NULL
                                       OR vote_average BETWEEN 0 AND 10),
  CONSTRAINT chk_movies_votecnt CHECK (vote_count IS NULL OR vote_count >= 0),
  INDEX idx_movies_title (title),
  INDEX idx_movies_release (release_date)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- genres : 20 TMDB genres. name is UNIQUE (non-key integrity constraint).
-- ---------------------------------------------------------------------
CREATE TABLE genres (
  genre_id INT          NOT NULL,
  name     VARCHAR(50)  NOT NULL,
  PRIMARY KEY (genre_id),
  CONSTRAINT uq_genres_name UNIQUE (name)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- movie_genres : many-to-many between movies and genres.
-- Composite PK prevents the same genre being attached twice.
-- Deleting a movie removes its genre links (CASCADE); a genre that is
-- still in use cannot be deleted (RESTRICT).
-- ---------------------------------------------------------------------
CREATE TABLE movie_genres (
  movie_id INT      NOT NULL,
  genre_id INT      NOT NULL,
  PRIMARY KEY (movie_id, genre_id),
  CONSTRAINT fk_mg_movie FOREIGN KEY (movie_id) REFERENCES movies (movie_id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT fk_mg_genre FOREIGN KEY (genre_id) REFERENCES genres (genre_id)
    ON DELETE RESTRICT ON UPDATE CASCADE,
  INDEX idx_mg_genre (genre_id)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- people : cast and crew members (TMDB person id).
-- gender uses the TMDB code: 0 unspecified, 1 female, 2 male, 3 non-binary.
-- ---------------------------------------------------------------------
CREATE TABLE people (
  person_id INT          NOT NULL,
  name      VARCHAR(255) NOT NULL,
  gender    TINYINT      NULL,
  PRIMARY KEY (person_id),
  CONSTRAINT chk_people_gender CHECK (gender IS NULL OR gender BETWEEN 0 AND 3),
  INDEX idx_people_name (name)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- movie_cast : many-to-many between movies and people (acting credits).
-- Keyed on the TMDB credit_id because one person can hold several
-- credits on the same film, so (movie_id, person_id) is not unique.
-- ---------------------------------------------------------------------
CREATE TABLE movie_cast (
  credit_id      CHAR(24)     NOT NULL,
  movie_id       INT          NOT NULL,
  person_id      INT          NOT NULL,
  character_name VARCHAR(300) NULL,
  cast_order     SMALLINT     NULL,
  PRIMARY KEY (credit_id),
  CONSTRAINT fk_cast_movie  FOREIGN KEY (movie_id)  REFERENCES movies (movie_id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT fk_cast_person FOREIGN KEY (person_id) REFERENCES people (person_id)
    ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT chk_cast_order CHECK (cast_order IS NULL OR cast_order >= 0),
  INDEX idx_cast_movie (movie_id),
  INDEX idx_cast_person (person_id)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- movie_crew : many-to-many between movies and people (crew credits).
-- ---------------------------------------------------------------------
CREATE TABLE movie_crew (
  credit_id  CHAR(24)     NOT NULL,
  movie_id   INT          NOT NULL,
  person_id  INT          NOT NULL,
  department VARCHAR(50)  NOT NULL,
  job        VARCHAR(100) NOT NULL,
  PRIMARY KEY (credit_id),
  CONSTRAINT fk_crew_movie  FOREIGN KEY (movie_id)  REFERENCES movies (movie_id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT fk_crew_person FOREIGN KEY (person_id) REFERENCES people (person_id)
    ON DELETE RESTRICT ON UPDATE CASCADE,
  INDEX idx_crew_movie (movie_id),
  INDEX idx_crew_person (person_id),
  INDEX idx_crew_job (job)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- users : lightweight application users (no passwords by design).
-- ---------------------------------------------------------------------
CREATE TABLE users (
  user_id      INT          NOT NULL AUTO_INCREMENT,
  username     VARCHAR(50)  NOT NULL,
  display_name VARCHAR(100) NULL,
  created_at   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (user_id),
  CONSTRAINT uq_users_username UNIQUE (username),
  CONSTRAINT chk_users_username CHECK (CHAR_LENGTH(TRIM(username)) >= 3)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- ratings : one row per (user, movie). Composite PK enforces
-- "one rating per user per film" without a surrogate key.
-- CHECK enforces the 0.5-5.0 range AND half-star steps.
-- ---------------------------------------------------------------------
CREATE TABLE ratings (
  user_id      INT          NOT NULL,
  movie_id     INT          NOT NULL,
  rating_value DECIMAL(2,1) NOT NULL,
  rated_at     TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   TIMESTAMP    NULL     DEFAULT NULL
                            ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (user_id, movie_id),
  CONSTRAINT fk_ratings_user  FOREIGN KEY (user_id)  REFERENCES users (user_id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT fk_ratings_movie FOREIGN KEY (movie_id) REFERENCES movies (movie_id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT chk_ratings_range CHECK (rating_value BETWEEN 0.5 AND 5.0),
  CONSTRAINT chk_ratings_step  CHECK (rating_value * 2 = FLOOR(rating_value * 2)),
  CONSTRAINT chk_ratings_time  CHECK (updated_at IS NULL OR updated_at >= rated_at),
  INDEX idx_ratings_movie (movie_id)
) ENGINE=InnoDB;

SELECT 'Schema created' AS status, VERSION() AS mysql_version;
