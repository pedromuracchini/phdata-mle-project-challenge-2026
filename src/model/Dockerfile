FROM python:3.10-slim

WORKDIR /app

# Copy requirements from project root
COPY requirements.txt .

# Copy the create_model script and data
COPY create_model.py .
COPY src/data ./data

RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "create_model.py"]