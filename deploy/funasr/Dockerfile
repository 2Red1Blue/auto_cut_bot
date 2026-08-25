FROM python:3.13.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 10001 funasr
WORKDIR /service
COPY deploy/funasr/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir -r requirements.lock
COPY deploy/funasr/service.py ./service.py
USER funasr
EXPOSE 8765
CMD ["python", "service.py"]
