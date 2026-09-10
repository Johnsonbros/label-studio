FROM python:3.12-slim
WORKDIR /app
COPY pipeline/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY pipeline/*.py /app/
COPY scripts/export_reviewed.py scripts/preannotate.py /app/
CMD ["uvicorn","service:app","--host","0.0.0.0","--port","8080","--no-access-log"]
