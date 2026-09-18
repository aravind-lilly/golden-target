# syntax=docker/dockerfile:1
#
# Local smoke-test container for your Golden Target submission.
#
# This is a SIMPLIFIED environment for your own convenience — it matches the evaluation agent's
# base image (Ubuntu 22.04) and installs a single Python version, but it is not a byte-for-byte
# replica of the agent's own container (which is organizer-only and not shared). It exists to
# catch the most common "works on my machine" failures — OS-level library differences, Python-
# version-specific behavior — before you rely on POST /sandbox/test against the real agent.
#
# Build:
#   docker build -t my-goldentarget-tool .
# Run against the practice pack (adjust the mount path to wherever your local copy lives):
#   docker run --rm -v $(pwd)/../practice:/pack my-goldentarget-tool python3 solve.py /pack

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Install ONE Python version — whatever you declared as "runtime_version" in
# goldentarget_config.json. This example installs 3.11; swap for 3.12/3.13 to match your own
# declaration if different.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.11 \
        python3.11-venv \
        python3-pip \
        ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# NOTE: this Dockerfile installs from public PyPI for your own local testing convenience. The
# real evaluation agent installs from Lilly's JFrog Artifactory instead (see README.md, "Python
# packages — JFrog only") — a package/version available here on public PyPI is not guaranteed to
# exist in JFrog. This container catches environment-shaped bugs; it does not guarantee your
# dependencies will resolve identically at grading time.
RUN pip3 install --no-cache-dir -r requirements.txt

COPY . .

ENTRYPOINT ["python3.11"]
CMD ["solve.py", "/pack"]
