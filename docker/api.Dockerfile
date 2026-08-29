FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg clamav && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    "fastapi==0.141.1" "uvicorn==0.35.0" "h3==4.3.1" \
    "jsonschema[format]==4.25.1" "numpy==2.2.3" "openai==3.6.0" \
    "python-dotenv==1.2.3" "boto3==1.40.31" \
    "psycopg[binary,pool]==3.2.10" "python-multipart==0.0.20"

COPY contracts/ contracts/
COPY migrations/ migrations/
COPY src/__init__.py src/__init__.py
COPY src/api/ src/api/
COPY src/data/ src/data/
COPY src/features/ src/features/
COPY src/models/ src/models/

EXPOSE 8000
CMD ["uvicorn", "src.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
