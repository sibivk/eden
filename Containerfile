FROM python:3.11-slim

LABEL project=eden

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py scheduler.py ./
COPY templates/ templates/
COPY static/ static/

RUN mkdir -p logs data /libraries/malayalam /libraries/hindi /libraries/tamil /libraries/english

EXPOSE 5000

# Single worker — prevents duplicate scheduler jobs across workers
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--timeout", "60", "app:app"]
