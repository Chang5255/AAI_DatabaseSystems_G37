# MongoDB Integration Handover

This folder contains the completed MongoDB / non-relational database component
---

## 1. What Has Already Been Implemented

The MongoDB database is called:

`movie_app`

The following collections have been implemented:

- `movie_features`
- `user_tags`
- `user_activity_logs`
- `recommendations`

The MongoDB component currently includes:

- MongoDB connection configuration
- Collection creation
- JSON schema validation
- TMDB movie metadata import
- MongoDB indexes
- User tag CRUD operations
- User activity logging
- Aggregation pipelines
- Keyword-based recommendation generation
- Recommendation storage
- MongoDB verification tests

---

## 2. Required Software

Before integrating the MongoDB component, install:

- Python 3
- MongoDB Community Server
- MongoDB Compass (recommended for viewing/debugging the database)

MongoDB Community Server must be running before using the application.

Default local MongoDB connection:

`mongodb://localhost:27017/`

---

## 3. Python Environment Setup

From the project root, create a virtual environment:

```bash
python -m venv .venv