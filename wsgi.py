"""Entry point for gunicorn on Railway and for `flask --app wsgi`."""

from pine_tracker.web import create_app

app = create_app()
