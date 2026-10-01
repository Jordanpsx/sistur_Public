from flask import Blueprint

bp = Blueprint("logs", __name__)

from app.blueprints.logs import routes  # noqa: E402, F401
