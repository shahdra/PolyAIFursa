FROM python:3.12-slim AS builder # Base image as builder for building the application 

WORKDIR /app # Set the working directory inside the container to /app   

# Install build dependencies and clean up apt cache to reduce image size
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv # Create a virtual environment at /opt/venv to isolate Python dependencies
ENV PATH="/opt/venv/bin:$PATH"

# copy from the host machine to the container's working directory
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt

# from builder stage, create a new runtime stage to keep the final image small
FROM python:3.12-slim AS runtime

WORKDIR /app

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=builder /opt/venv /opt/venv
COPY . .

EXPOSE 8000 #Expose port 8000 for the FastAPI application to listen on. This allows external access to the service when running in a container.

CMD ["python", "app.py"]


