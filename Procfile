web: gunicorn wsgi:app --workers 2 --timeout 60 --bind 0.0.0.0:$PORT
worker: python -m worker.scheduler
release: flask db upgrade
