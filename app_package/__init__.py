import os
from flask import Flask, session, redirect, url_for, request

from app_package.routes.core_routes import core_bp
from app_package.routes.po_routes import po_bp
from app_package.routes.projects_routes import projects_bp
from app_package.routes.meetings_routes import meetings_bp
from app_package.routes.expenditure_routes import expenditure_bp

def create_app():
    app = Flask(__name__)
    app.secret_key = os.getenv("FLASK_SECRET_KEY", "cathkin-estates-secret-key")

    # Register external approved blueprints if present
    try:
        from routes.analysis import analysis_bp
        from approved_vendors import approved_vendors_bp
        app.register_blueprint(analysis_bp)
        app.register_blueprint(approved_vendors_bp)
    except ImportError:
        pass

    # Register modular blueprints
    app.register_blueprint(core_bp)
    app.register_blueprint(po_bp)
    app.register_blueprint(projects_bp)
    app.register_blueprint(meetings_bp)
    app.register_blueprint(expenditure_bp)

    @app.before_request
    def require_login():
        allowed_endpoints = ['core.login', 'static']
        if request.endpoint and request.endpoint not in allowed_endpoints and 'user_id' not in session:
            return redirect(url_for('core.login', next=request.url))

    return app
