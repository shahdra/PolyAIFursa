FROM python:3.12-slim-trixie AS builder

WORKDIR /app

# Install system dependencies needed to build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends libgl1-mesa-dev libglib2.0-0 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY torch-requirements.txt requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r torch-requirements.txt && pip install --no-cache-dir -r requirements.txt

FROM python:3.12-slim-trixie AS runtime

WORKDIR /app

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Runtime system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends libgl1-mesa-dev libglib2.0-0 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY . .

EXPOSE 8080

CMD ["python", "app.py"]

