FROM python:3.15.2-slim
# Versions match uv.lock. This is a test double, never a model; see app.py.
RUN pip install --no-cache-dir fastapi==0.162.0 uvicorn==0.54.0
COPY app.py /app/app.py
WORKDIR /app
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
