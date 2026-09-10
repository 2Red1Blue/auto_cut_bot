FROM python:3.13.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 10001 funasr
WORKDIR /service
COPY deploy/funasr/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir -r requirements.lock
# The service imports only the Kernel's pure Python runtime modules.  Copy them
# directly into the image rather than asking pip to create an isolated PEP 517
# build environment, which would make a local/offline desktop build download
# hatchling after the locked requirements have already been installed.
COPY packages/autocut-kernel/src/autocut_kernel ./autocut_kernel
COPY deploy/funasr/service.py ./service.py
USER funasr
EXPOSE 8765
CMD ["python", "service.py"]
