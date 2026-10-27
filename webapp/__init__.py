"""
INF2003 Group Project - Team 37
webapp : Flask front end over MySQL (moviedb) and MongoDB (movie_app)
"""

import os
import secrets

import mysql.connector
from flask import Flask, render_template
from pymongo.errors import PyMongoError

from .db import close_mysql, ensure_mongo_indexes


def create_app():
    app = Flask(__name__)
    # Only used to sign the "current user" cookie. Set FLASK_SECRET_KEY in
    # .env to keep users selected across restarts.
    app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(16)

    from .routes import bp
    app.register_blueprint(bp)
    app.teardown_appcontext(close_mysql)

    @app.errorhandler(mysql.connector.Error)
    def mysql_down(err):
        return render_template(
            "error.html", title="Cannot reach MySQL",
            message=f"{err}",
            hint="Check that the MySQL80 service is running and that MYSQL_HOST, MYSQL_PORT, "
                 "MYSQL_USER and MYSQL_PASSWORD in .env are right (port 3307 if you "
                 "installed MySQL next to MariaDB). The database must be loaded with "
                 "database/mysql/01 to 07 first."), 503

    @app.errorhandler(PyMongoError)
    def mongo_down(err):
        return render_template(
            "error.html", title="Cannot reach MongoDB",
            message=f"{err}",
            hint="Check that the MongoDB service is running (Compass should connect to "
                 "mongodb://localhost:27017) and that database/mongodb/setup_mongodb.py "
                 "has been run."), 503

    with app.app_context():
        try:
            ensure_mongo_indexes()
        except PyMongoError as err:
            app.logger.warning("MongoDB not reachable at start-up: %s", err)

    return app
