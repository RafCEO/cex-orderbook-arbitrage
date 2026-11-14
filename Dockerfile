FROM python:3.12-slim

RUN useradd --create-home --shell /usr/sbin/nologin appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY core/ core/
COPY connectors/ connectors/
COPY engine/ engine/
COPY storage/ storage/
COPY cli/ cli/
COPY config.yaml .
COPY main.py .

USER appuser

CMD ["python", "main.py"]
