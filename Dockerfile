FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Read-only scanner by default. Mount ./data and ./reports to keep results.
CMD ["python", "-m", "arb", "scan", "--interval", "60"]
