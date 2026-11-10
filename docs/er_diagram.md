# Relational schema (MySQL `moviedb`)

The diagram below is written in Mermaid, which GitHub renders directly.
For the report, open this file on GitHub and screenshot it, or paste the
block into <https://mermaid.live> and export a PNG. The authoritative
version of everything here is `database/mysql/01_schema.sql`.

```mermaid
erDiagram
    MOVIES {
        int movie_id PK "TMDB id; shared key with MongoDB"
        varchar title "NOT NULL"
        varchar original_title
        char original_language "2 chars"
        date release_date
        smallint runtime "CHECK > 0"
        bigint budget "CHECK >= 0"
        bigint revenue "CHECK >= 0"
        decimal popularity
        decimal vote_average "CHECK 0..10"
        int vote_count "CHECK >= 0"
        varchar status
    }

    GENRES {
        int genre_id PK
        varchar name "UNIQUE, NOT NULL"
    }

    MOVIE_GENRES {
        int movie_id PK,FK "ON DELETE CASCADE"
        int genre_id PK,FK "ON DELETE RESTRICT"
    }

    PEOPLE {
        int person_id PK "TMDB person id"
        varchar name "NOT NULL"
        tinyint gender "CHECK 0..3"
    }

    MOVIE_CAST {
        char credit_id PK "TMDB credit id, 24 chars"
        int movie_id FK "ON DELETE CASCADE"
        int person_id FK "ON DELETE RESTRICT"
        varchar character_name
        smallint cast_order "CHECK >= 0; 0 is the lead"
    }

    MOVIE_CREW {
        char credit_id PK "TMDB credit id, 24 chars"
        int movie_id FK "ON DELETE CASCADE"
        int person_id FK "ON DELETE RESTRICT"
        varchar department "NOT NULL"
        varchar job "NOT NULL"
    }

    USERS {
        int user_id PK "AUTO_INCREMENT"
        varchar username "UNIQUE, CHECK length >= 3"
        varchar display_name
        timestamp created_at "DEFAULT CURRENT_TIMESTAMP"
    }

    RATINGS {
        int user_id PK,FK "ON DELETE CASCADE"
        int movie_id PK,FK "ON DELETE CASCADE"
        decimal rating_value "CHECK 0.5..5.0 and half-star steps"
        timestamp rated_at "DEFAULT CURRENT_TIMESTAMP"
        timestamp updated_at "ON UPDATE CURRENT_TIMESTAMP"
    }

    RATING_AUDIT {
        bigint audit_id PK "AUTO_INCREMENT"
        enum action "INSERT / UPDATE / DELETE"
        int user_id "no FK, on purpose"
        int movie_id "no FK, on purpose"
        decimal old_value
        decimal new_value
        varchar changed_by "SESSION_USER(): the account that connected"
        bigint connection_id "CONNECTION_ID()"
        timestamp changed_at
    }

    MOVIES   ||--o{ MOVIE_GENRES : "is classified as"
    GENRES   ||--o{ MOVIE_GENRES : "classifies"
    MOVIES   ||--o{ MOVIE_CAST   : "is acted in by"
    PEOPLE   ||--o{ MOVIE_CAST   : "acts in"
    MOVIES   ||--o{ MOVIE_CREW   : "is crewed by"
    PEOPLE   ||--o{ MOVIE_CREW   : "works on"
    USERS    ||--o{ RATINGS      : "gives"
    MOVIES   ||--o{ RATINGS      : "receives"
    RATINGS  }o..o{ RATING_AUDIT : "is recorded in (by trigger, no FK)"
```

## Relationships, and why each one is shaped that way

| Relationship | Cardinality | How it is implemented |
|---|---|---|
| movie — genre | **many-to-many** | `movie_genres`, a junction table whose primary key is the pair `(movie_id, genre_id)`. The composite key is the constraint: it makes attaching the same genre twice impossible. 12,160 rows. |
| movie — person (cast) | **many-to-many**, with attributes | `movie_cast` carries `character_name` and `cast_order`, which belong to the *relationship*, not to the film or the person. Keyed on TMDB's `credit_id`, not on `(movie_id, person_id)`, because one person can hold two credits on the same film (two roles). 106,257 rows. |
| movie — person (crew) | **many-to-many**, with attributes | `movie_crew`, same reasoning, carrying `department` and `job`. 129,581 rows. |
| user — movie (rating) | **many-to-many**, with attributes | `ratings`, keyed on `(user_id, movie_id)`. Here the composite key *is* the business rule "one rating per user per film", so no surrogate key is used. |
| rating — audit | one-to-many, **no foreign key** | Written by the triggers in `08_triggers.sql`. An audit row must outlive the rating and the user it describes, so a foreign key would be actively wrong. |

Cast and crew are kept as two tables rather than one with a `role_type`
column: a cast credit has a character and a billing position, a crew
credit has a department and a job, and merging them would leave half
the columns NULL in every row.

## Constraints, by kind

| Kind | Where | Examples |
|---|---|---|
| Primary keys | all 9 tables | including 3 composite ones (`movie_genres`, `ratings`) |
| Foreign keys | 8 | `CASCADE` where the child is meaningless alone (a genre link, a credit, a rating), `RESTRICT` where losing the parent would lose information (a genre or a person still in use) |
| `UNIQUE` | 2 | `genres.name`, `users.username` |
| `CHECK` | 11 | ranges (`vote_average` 0–10), signs (`budget >= 0`), string length (`username`), and `rating_value * 2 = FLOOR(rating_value * 2)`, which enforces half-star steps |
| `NOT NULL` + types | throughout | `CHAR(2)` for a language code, `DECIMAL(2,1)` for a rating, `DATE` for a release date |
| Triggers | 5 | the audit trail, and the cross-table rule a `CHECK` cannot express: a rating may not point at a film that has not been released. See `08_triggers.sql` |
| Privileges | 3 accounts | `01b_create_user.sql`: the application may not run DDL and may not write the catalogue or the audit table; the reporting account sees only views |

## Normalisation

Every table is in BCNF. The two decisions worth defending in the report
are both recorded in `data/cleaned/cleaning_report.txt`:

- `credits.title` was dropped. It duplicated `movies.title` (verified
  identical across all 4,803 rows), and since the join is on
  `movie_id`, keeping it would have been a transitive dependency
  `credit_id → movie_id → title`.
- `gender` was moved out of the credit rows onto `people`. It depends
  on the person, not on the credit, so storing it per credit repeated
  it 106,257 times instead of 104,842.

## Views (`04_views.sql`)

| View | What it adds |
|---|---|
| `v_movie_stats` | per film: TMDB score, a Bayesian weighted score, and our users' average |
| `v_movie_cards` | one row per film with genres and directors flattened into lists, for the browse page |
| `v_top_rated_per_genre` | top 10 per genre, using `RANK() OVER (PARTITION BY ...)` |
| `v_genre_year_stats` | counts, scores and revenue per genre and year |
| `v_director_stats` | per-director totals |
| `v_user_activity` | rating activity per user, keeping users with none |

The views also carry the security design: the reporting account can read
them but has no privilege on the tables underneath.
