# Image that runs pytest on uploaded files. Build it from this folder only:
#   docker build -t ai-bug-detector-sandbox:1 sandbox
# The rest of the project, and any secret in it, is never sent to Docker.

# Official Python image, pinned by digest: a changed image cannot be used by mistake.
FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f

LABEL ai-bug-detector.sandbox.image="1"

ENV PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt /opt/sandbox/requirements.txt
RUN pip install --require-hashes --only-binary=:all: \
        -r /opt/sandbox/requirements.txt

# Owned by root and read-only for everyone else: the tested code cannot change them.
COPY --chmod=0444 pytest.ini runner.py /opt/sandbox/

# The "nobody" user. The run command sets it again and adds every other limit.
USER 65534:65534
WORKDIR /tmp

# -I: ignore PYTHON* variables and the user's site folder. -B: write no bytecode.
ENTRYPOINT ["python", "-I", "-B", "/opt/sandbox/runner.py"]
