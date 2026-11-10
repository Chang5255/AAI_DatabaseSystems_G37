"""
INF2003 Group Project - Team 37
database/mongodb/create_collections.py : collections and their validators

MongoDB does not need a schema, but it can enforce one. Each collection
below is created with a $jsonSchema validator, which is MongoDB's
answer to the DDL constraints in database/mysql/01_schema.sql:

  MySQL                                   MongoDB here
  --------------------------------------  ----------------------------------
  NOT NULL                                "required": [...]
  data type (INT, VARCHAR, DATE)          "bsonType"
  CHECK (x BETWEEN a AND b)               "minimum" / "maximum"
  CHECK (CHAR_LENGTH(x) >= n)             "minLength" / "maxLength"
  CHECK (x IN ('a','b'))                  "enum"
  UNIQUE                                  a unique index (create_indexes.py)
  FOREIGN KEY                             NOT AVAILABLE - see the note below

validationLevel / validationAction are set explicitly rather than left
at their defaults, so the behaviour is documented in the code:
  validationLevel  = "strict"  -> validate inserts AND updates
  validationAction = "error"   -> reject the write (not just warn)

NO FOREIGN KEYS: MongoDB cannot check that user_id 7 exists in MySQL's
users table, so "every tag belongs to a real user" is the application's
job (webapp/routes.py), not the database's. That difference is one of
the main trade-offs in the SQL vs NoSQL comparison in the report.

Re-runnable: an existing collection is updated with collMod, which
changes the validator without touching the documents.
"""

import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import MongoClient

from event_types import EVENT_TYPES

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
DATABASE_NAME = os.getenv("MONGODB_DATABASE", "movie_app")

client = MongoClient(MONGODB_URI)
db = client[DATABASE_NAME]


validators = {

    # -----------------------------------------------------------------
    # movie_features: the denormalised film document.
    # One document per film, holding what MySQL spreads over movies,
    # movie_cast, movie_crew and people. movie_id is the shared key
    # with MySQL (movies.movie_id, the TMDB id).
    # -----------------------------------------------------------------
    "movie_features": {
        "$jsonSchema": {
            "bsonType": "object",
            "required": ["movie_id"],
            "properties": {
                "movie_id": {
                    "bsonType": "int",
                    "minimum": 1,
                    "description": "TMDB movie ID; equals MySQL movies.movie_id"
                },
                "overview": {
                    "bsonType": ["string", "null"]
                },
                "poster_path": {
                    "bsonType": ["string", "null"],
                    "description": "TMDB image path, e.g. /abc123.jpg"
                },
                "keywords": {
                    "bsonType": "array",
                    "items": {
                        "bsonType": "object",
                        "required": ["id", "name"],
                        "properties": {
                            "id": {"bsonType": "int"},
                            # No minLength here, deliberately: two of the
                            # 4,803 films carry a TMDB keyword whose name
                            # is an empty string. Requiring a non-empty
                            # name would make import_movie_features.py
                            # fail on those two documents. The type is
                            # still enforced.
                            "name": {"bsonType": "string"}
                        }
                    }
                },
                "cast": {
                    "bsonType": "array",
                    "items": {
                        "bsonType": "object",
                        "required": ["person_id", "name"],
                        "properties": {
                            "person_id": {"bsonType": "int", "minimum": 1},
                            "name": {"bsonType": "string", "minLength": 1},
                            "character": {"bsonType": ["string", "null"]},
                            # Billing position: 0 is the lead role.
                            "order": {"bsonType": "int", "minimum": 0}
                        }
                    }
                },
                "crew": {
                    "bsonType": "array",
                    "items": {
                        "bsonType": "object",
                        "required": ["person_id", "name"],
                        "properties": {
                            "person_id": {"bsonType": "int", "minimum": 1},
                            "name": {"bsonType": "string", "minLength": 1},
                            "department": {"bsonType": "string"},
                            "job": {"bsonType": "string"}
                        }
                    }
                },
                "production_companies": {
                    "bsonType": "array",
                    "items": {
                        "bsonType": "object",
                        "properties": {
                            "company_id": {"bsonType": "int"},
                            "name": {"bsonType": "string"}
                        }
                    }
                },
                "production_countries": {
                    "bsonType": "array",
                    "items": {
                        "bsonType": "object",
                        "properties": {
                            "country_code": {"bsonType": "string"},
                            "name": {"bsonType": "string"}
                        }
                    }
                },
                "spoken_languages": {
                    "bsonType": "array",
                    "items": {
                        "bsonType": "object",
                        "properties": {
                            "language_code": {"bsonType": "string"},
                            "name": {"bsonType": "string"}
                        }
                    }
                }
            }
        }
    },

    # -----------------------------------------------------------------
    # user_tags: free-text labels a user puts on a film. 72,600 of
    # these are real tag applications from MovieLens 25M.
    # The length limits are the NoSQL version of
    # CHECK (CHAR_LENGTH(TRIM(tag)) BETWEEN 1 AND 100), and they matter:
    # tags are grouped in the reports, so a 230-character "tag" (the
    # longest in the real data) would wreck the output. The bounds were
    # set FROM the data rather than guessed: 1 to 100 characters keeps
    # 99.994% of real MovieLens tags, and 100 is also what
    # webapp/routes.py truncates a typed tag to, so the application and
    # the database agree. "one user cannot apply the same tag twice to
    # the same film" is a unique index, in create_indexes.py.
    # -----------------------------------------------------------------
    "user_tags": {
        "$jsonSchema": {
            "bsonType": "object",
            "required": ["user_id", "movie_id", "tag", "created_at"],
            "properties": {
                "user_id": {"bsonType": "int", "minimum": 1},
                "movie_id": {"bsonType": "int", "minimum": 1},
                "tag": {
                    "bsonType": "string",
                    "minLength": 1,
                    "maxLength": 100,
                    "description": "1-100 characters; matches routes.py tag[:100]"
                },
                "created_at": {"bsonType": "date"},
                "updated_at": {"bsonType": ["date", "null"]}
            }
        }
    },

    # -----------------------------------------------------------------
    # user_activity_logs: append-only behaviour stream.
    # This is the collection that justifies using a document database
    # at all: metadata holds a different shape per event (a search has
    # a query and a result count, a rating has a score), which would
    # need either a sparse table or one table per event type in MySQL.
    # event_type is pinned to a vocabulary (see event_types.py) so the
    # $group aggregations cannot be silently split by a typo.
    # A TTL index in create_indexes.py expires these documents.
    # -----------------------------------------------------------------
    "user_activity_logs": {
        "$jsonSchema": {
            "bsonType": "object",
            "required": ["user_id", "event_type", "timestamp"],
            "properties": {
                "user_id": {"bsonType": "int", "minimum": 1},
                "event_type": {
                    "bsonType": "string",
                    "enum": EVENT_TYPES,
                    "description": "one of event_types.EVENT_TYPES"
                },
                "movie_id": {"bsonType": "int", "minimum": 1},
                "timestamp": {"bsonType": "date"},
                # Deliberately unconstrained: the shape differs per
                # event type, and that freedom is the reason this data
                # lives in MongoDB rather than in a MySQL table.
                "metadata": {"bsonType": "object"}
            }
        }
    },

    # -----------------------------------------------------------------
    # recommendations: one document per generated list, kept as history
    # (not overwritten), so we can compare algorithms over time.
    # score is a double in 0..1; "algorithm" records which generator
    # produced the list - keyword_similarity (MongoDB only) or
    # hybrid_sql_keyword (MySQL ratings + MongoDB keywords).
    # -----------------------------------------------------------------
    "recommendations": {
        "$jsonSchema": {
            "bsonType": "object",
            "required": [
                "user_id",
                "generated_at",
                "algorithm",
                "recommendations"
            ],
            "properties": {
                "user_id": {"bsonType": "int", "minimum": 1},
                "generated_at": {"bsonType": "date"},
                "algorithm": {"bsonType": "string", "minLength": 3},
                "based_on": {
                    "bsonType": "object",
                    "description": "what the list was built from, for explainability"
                },
                "recommendations": {
                    "bsonType": "array",
                    "items": {
                        "bsonType": "object",
                        "required": ["movie_id", "score"],
                        "properties": {
                            "movie_id": {"bsonType": "int", "minimum": 1},
                            "score": {
                                "bsonType": "double",
                                "minimum": 0,
                                "maximum": 1
                            },
                            "matched_keywords": {
                                "bsonType": "array",
                                "items": {"bsonType": "string"}
                            },
                            "reason": {"bsonType": "string"}
                        }
                    }
                }
            }
        }
    },

    # -----------------------------------------------------------------
    # people_graph: the co-star graph, built by build_costar_graph.py
    # from movie_features. Each document is one person plus the people
    # they have shared top billing with, which turns the cast arrays
    # into an adjacency list that $graphLookup can walk.
    # -----------------------------------------------------------------
    "people_graph": {
        "$jsonSchema": {
            "bsonType": "object",
            "required": ["person_id", "name", "costar_ids"],
            "properties": {
                "person_id": {"bsonType": "int", "minimum": 1},
                "name": {"bsonType": "string", "minLength": 1},
                "film_count": {"bsonType": "int", "minimum": 0},
                "costar_ids": {
                    "bsonType": "array",
                    "items": {"bsonType": "int"},
                    "description": "adjacency list: people billed alongside this person"
                },
                "built_at": {"bsonType": "date"}
            }
        }
    },
}


def apply_validators():
    existing_collections = db.list_collection_names()

    for collection_name, validator in validators.items():

        if collection_name not in existing_collections:
            db.create_collection(
                collection_name,
                validator=validator,
                validationLevel="strict",
                validationAction="error"
            )

            print(f"Created collection: {collection_name}")

        else:
            db.command(
                "collMod",
                collection_name,
                validator=validator,
                validationLevel="strict",
                validationAction="error"
            )

            print(f"Updated validator: {collection_name}")


def show_validator_demo():
    """Prove the validator rejects bad documents (nothing is kept)."""

    print("\nValidator check (every write below must be refused)")

    bad_documents = [
        ("user_tags", {"user_id": 1, "movie_id": 19995, "tag": ""},
         "tag shorter than minLength 1"),
        ("user_tags", {"user_id": 1, "movie_id": 19995, "tag": "a" * 150},
         "tag longer than maxLength 100"),
        ("user_tags", {"user_id": 1, "movie_id": 19995, "tag": "fine"},
         "missing required field created_at"),
        ("user_activity_logs",
         {"user_id": 1, "event_type": "veiw_movie",
          "timestamp": datetime.now(timezone.utc)},
         "event_type not in the enum (typo)"),
        ("movie_features", {"movie_id": "19995"},
         "movie_id is a string, not an int"),
    ]

    for collection_name, document, why in bad_documents:
        try:
            db[collection_name].insert_one(dict(document))
            print(f"  NOT REJECTED (problem!): {collection_name} - {why}")
            db[collection_name].delete_one(document)
        except Exception as exc:
            reason = type(exc).__name__
            print(f"  rejected  {collection_name:<20} {why}  ({reason})")


if __name__ == "__main__":
    apply_validators()

    print("\nMongoDB collections ready.")
    for collection in sorted(db.list_collection_names()):
        print(f"- {collection}  ({db[collection].count_documents({})} documents)")

    show_validator_demo()

    client.close()
