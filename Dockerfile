FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .

# Instalar dependencias Python (incluye playwright==1.60.0)
RUN pip install --no-cache-dir -r requirements.txt

# Instalar dependencias del sistema para Chromium y luego el browser
# playwright install-deps resuelve automáticamente los paquetes apt necesarios
RUN playwright install-deps chromium && playwright install chromium

COPY . .

RUN mkdir -p /data

EXPOSE 8000

# Shell form para que $PORT sea expandido por el shell
CMD sh -c "python -m uvicorn api.main:app --host 0.0.0.0 --port $PORT"
