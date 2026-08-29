FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    "fastapi==0.141.1" "uvicorn==0.35.0" "h3==4.3.1" "jsonschema[format]==4.25.1" \
    "openai==3.6.0" "python-dotenv==1.2.3"

COPY contracts/ contracts/
COPY src/__init__.py src/__init__.py
COPY src/api/ src/api/

EXPOSE 8000
CMD ["uvicorn", "src.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
