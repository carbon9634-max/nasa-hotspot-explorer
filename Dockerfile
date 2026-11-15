# Cloud Run's Python buildpacks fail on this Streamlit app.
# With this file in the project root, `gcloud run deploy --source .`
# builds the image in Cloud Build. Docker is not required on a laptop.
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py harmonize.py app_data.csv ./
COPY .streamlit/ .streamlit/

EXPOSE 8080

CMD streamlit run app.py --server.port=$PORT --server.address=0.0.0.0 --server.headless=true
