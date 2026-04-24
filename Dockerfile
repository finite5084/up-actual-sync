FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY up_bank_to_actual.py .

# PYTHONUNBUFFERED ensures log output appears immediately in docker logs
ENV PYTHONUNBUFFERED=1

# Run as a non-root user — principle of least privilege
RUN useradd --create-home --shell /bin/bash appuser
USER appuser

# Default: continuous polling loop
CMD ["python", "up_bank_to_actual.py"]
