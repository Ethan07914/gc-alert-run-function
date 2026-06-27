FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run the job once and exit. Cloud Run Jobs run this command to completion.
CMD ["python", "main.py"]