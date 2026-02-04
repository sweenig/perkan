# PerKan (Simple Flask Kanban)

A minimal web-based Kanban board powered by Flask and a single JSON data file.

## Python

1. python -m venv venv
2. venv\Scripts\activate (Windows) or source venv/bin/activate
3. pip install --break-system-packages -r requirements.txt
4. python app.py

Open http://127.0.0.1:5000

## Docker

- Run with the local `data` directory (recommended — run from the `perkan` folder):

`docker run -p 5000:5000 -v "$(pwd)/data:/app/data" perkan`

- Docker Compose (recommended):

From the `perkan` directory:

`docker compose up --build -d`

The service runs Gunicorn with conservative settings (2 workers, 4 threads, 60s timeout).