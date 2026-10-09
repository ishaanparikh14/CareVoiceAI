#!/usr/bin/env bash
# Create the venv and install Python deps (CPU-only torch to avoid CUDA bloat).
set -euo pipefail
cd /home/ec2-user/carevoice-server

/usr/bin/python3.11 -m venv .venv
./.venv/bin/pip install --upgrade pip wheel

# Install the CPU-only torch wheel first so the generic torch==2.3.0 line in
# requirements.txt is already satisfied (avoids pulling nvidia CUDA packages).
./.venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch==2.3.0

# Then the rest of the requirements.
./.venv/bin/pip install -r requirements.txt

echo "INSTALL_DONE"
./.venv/bin/python -c "import fastapi, uvicorn, torch, transformers, faster_whisper, asyncpg; print('imports OK', torch.__version__)"
