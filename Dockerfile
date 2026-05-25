# Imagen oficial de Microsoft con Python 3.11 + Playwright + Chromium preinstalados
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# Dependencias primero para aprovechar la cache de Docker
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código fuente
COPY . .

# El volumen de Railway se monta en /data
RUN mkdir -p /data

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
