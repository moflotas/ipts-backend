"""Flask application factory"""

import os

from flask import Flask
import flask_migrate
from flask_migrate import Migrate

from innopoints.views import api, oauth
from innopoints.models import db, login_manager


def create_app(config='config/prod.py'):
    """Create Flask application with given configuration"""
    app = Flask(__name__, static_folder=None)
    app.secret_key = os.urandom(16)
    app.config.from_pyfile(config)

    db.init_app(app)
    Migrate(app, db)

    with app.app_context():
        if not os.path.exists('migrations'):
            flask_migrate.init()
        flask_migrate.migrate()
        flask_migrate.upgrade()

    login_manager.init_app(app)

    app.register_blueprint(api)
    oauth.init_app(app)

    return app
