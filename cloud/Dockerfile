# Paper Factory Cloud Solver - Dockerfile
# Base image with Python 3.11
FROM python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93

# Install the single runtime utility used by the local healthcheck. Python
# solver dependencies are wheels; compilers and git do not belong in runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

# Copy solver API code
COPY solver_api.py .
COPY solver_runner.py .
COPY solver_exec_wrapper.py .
COPY runtime_capabilities.py .
COPY runtime_capabilities.json .
COPY smoke_test.py .

# The API stays root so it can stage root-owned read-only inputs. Submitted
# scripts are always dropped to this unprivileged identity before execution.
RUN groupadd --gid 10001 solver && \
    useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin solver

# Create directories for job storage
RUN mkdir -p /tmp/solver-control/jobs /tmp/solver-control/results

# Fail the image build if its declared Python runtime or solver packages drift.
RUN python smoke_test.py

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PORT=8080
ENV SOLVER_RUN_UID=10001
ENV SOLVER_RUN_GID=10001

# Expose port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Run the FastAPI server
CMD ["uvicorn", "solver_api:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
