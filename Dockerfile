# El analizador, para que corra en un servidor y no en la maquina de nadie.
# Se usa la imagen oficial de Playwright porque ya trae Chromium y todas las librerias
# del sistema que hacen falta para abrirlo: armarlo a mano es una tarde perdida.
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

WORKDIR /app

# git hace falta para clonar los repos que nos pasan
USER root
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY caronte.py entender.py plan.py costo.py worker.py ./

# Nunca corre como root: esto abre codigo de terceros que no controlamos.
RUN useradd -m barquero && chown -R barquero /app
USER barquero

ENV PYTHONUNBUFFERED=1
CMD ["python", "worker.py"]
