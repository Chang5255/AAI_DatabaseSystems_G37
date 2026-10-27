from pymongo import MongoClient
from dotenv import load_dotenv
import os

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI")
DATABASE_NAME = os.getenv("MONGODB_DATABASE")

client = MongoClient(MONGODB_URI)
db = client[DATABASE_NAME]


validators = {

    "movie_features": {
        "$jsonSchema": {
            "bsonType": "object",
            "required": ["movie_id"],
            "properties": {
                "movie_id": {
                    "bsonType": "int",
                    "description": "TMDB movie ID"
                },
                "overview": {
                    "bsonType": ["string", "null"]
                },
                "keywords": {
                    "bsonType": "array",
                    "items": {
                        "bsonType": "object",
                        "required": ["id", "name"],
                        "properties": {
                            "id": {"bsonType": "int"},
                            "name": {"bsonType": "string"}
                        }
                    }
                },
                "cast": {
                    "bsonType": "array",
                    "items": {
                        "bsonType": "object",
                        "properties": {
                            "person_id": {"bsonType": "int"},
                            "name": {"bsonType": "string"},
                            "character": {"bsonType": ["string", "null"]},      
                            "order": {"bsonType": "int"}
                        }
                    }
                },
                "crew": {
                    "bsonType": "array",
                    "items": {
                        "bsonType": "object",
                        "properties": {
                            "person_id": {"bsonType": "int"},
                            "name": {"bsonType": "string"},
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

    "user_tags": {
        "$jsonSchema": {
            "bsonType": "object",
            "required": ["user_id", "movie_id", "tag", "created_at"],
            "properties": {
                "user_id": {"bsonType": "int"},
                "movie_id": {"bsonType": "int"},
                "tag": {"bsonType": "string"},
                "created_at": {"bsonType": "date"}
            }
        }
    },

    "user_activity_logs": {
        "$jsonSchema": {
            "bsonType": "object",
            "required": ["user_id", "event_type", "timestamp"],
            "properties": {
                "user_id": {"bsonType": "int"},
                "event_type": {"bsonType": "string"},
                "movie_id": {"bsonType": "int"},
                "timestamp": {"bsonType": "date"},
                "metadata": {
                    "bsonType": "object"
                }
            }
        }
    },

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
                "user_id": {"bsonType": "int"},
                "generated_at": {"bsonType": "date"},
                "algorithm": {"bsonType": "string"},
                "recommendations": {
                    "bsonType": "array",
                    "items": {
                        "bsonType": "object",
                        "required": ["movie_id", "score"],
                        "properties": {
                            "movie_id": {"bsonType": "int"},
                            "score": {"bsonType": "double"},
                            "matched_keywords": {
                                "bsonType": "array",
                                "items": {
                                    "bsonType": "string"
                                }
                            },
                            "reason": {
                                "bsonType": "string"
                            }
                        }
                    }
                }
            }
        }
    }
}


existing_collections = db.list_collection_names()

for collection_name, validator in validators.items():

    if collection_name not in existing_collections:
        db.create_collection(
            collection_name,
            validator=validator
        )

        print(f"Created collection: {collection_name}")

    else:
        db.command(
            "collMod",
            collection_name,
            validator=validator
        )

        print(f"Updated validator: {collection_name}")


print("\nMongoDB collections ready.")

for collection in db.list_collection_names():
    print(f"- {collection}")

client.close()