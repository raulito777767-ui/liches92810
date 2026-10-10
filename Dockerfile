# Solo necesario si el build nativo de Python en Render no trae gcc.
# gcc vive únicamente en la etapa de compilación: la imagen final no lo incluye.
FROM python:3.11-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev make \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY engine.c Makefile ./
RUN make

FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=build /src/mackengine /app/mackengine
# book.bi[n] copia book.bin solo si existe (si no, no falla)
COPY bot.py book.bi[n] ./
CMD ["python", "bot.py"]
