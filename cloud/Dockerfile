# Paper Factory Cloud Solver - Dockerfile
# Base image with Python 3.11
FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    gfortran \
    libopenblas-dev \
    liblapack-dev \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install additional open-source solvers
RUN pip install --no-cache-dir \
    pyscipopt \
    cylp \
    mip

# Copy solver API code
COPY solver_api.py .
COPY solver_runner.py .

# Create directories for job storage
RUN mkdir -p /tmp/jobs /tmp/results

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PORT=8080

# Expose port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Run the FastAPI server
CMD ["uvicorn", "solver_api:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
