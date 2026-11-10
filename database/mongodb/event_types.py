"""
INF2003 Group Project - Team 37
database/mongodb/event_types.py : the allowed values of event_type

MongoDB is schema-less by default: any document with any field can be
inserted. That flexibility is the point of a document database, but it
also means a typo ("veiw_movie") creates a new category instead of an
error, and every aggregation that groups by event_type is then wrong.

We therefore pin the vocabulary down in ONE place:
  * create_collections.py puts this list into the $jsonSchema validator
    of user_activity_logs, so the SERVER rejects anything else - the
    NoSQL equivalent of a CHECK ... IN (...) constraint;
  * webapp/routes.py imports the same list, so the application and the
    database can never disagree about what a valid event is.

Adding an event type means editing this file and re-running
create_collections.py (which uses collMod, so no data is lost).
"""

# Search and browsing
SEARCH = "search"
VIEW_MOVIE = "view_movie"

# Ratings (MySQL writes, logged here)
RATE_MOVIE = "rate_movie"
DELETE_RATING = "delete_rating"
IMPORT_RATINGS = "import_ratings"

# Tags (MongoDB writes)
ADD_TAG = "add_tag"
EDIT_TAG = "edit_tag"
DELETE_TAG = "delete_tag"

# Recommendations and analytics pages
GENERATE_RECOMMENDATIONS = "generate_recommendations"
VIEW_NETWORK = "view_network"
VIEW_INSIGHTS = "view_insights"

# Account lifecycle
DELETE_USER = "delete_user"

EVENT_TYPES = [
    SEARCH,
    VIEW_MOVIE,
    RATE_MOVIE,
    DELETE_RATING,
    IMPORT_RATINGS,
    ADD_TAG,
    EDIT_TAG,
    DELETE_TAG,
    GENERATE_RECOMMENDATIONS,
    VIEW_NETWORK,
    VIEW_INSIGHTS,
    DELETE_USER,
]


if __name__ == "__main__":
    print(f"{len(EVENT_TYPES)} allowed event types:")
    for name in EVENT_TYPES:
        print(f"  {name}")
