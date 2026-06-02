FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY loan_portal.py .
COPY loan_policies.json .
COPY portal/ ./portal/
COPY sample_docs/ ./sample_docs/

# Uploads folder for temporary file storage
RUN mkdir -p uploads

ENV PYTHONUNBUFFERED=1

EXPOSE 5001

CMD ["python", "loan_portal.py"]
