FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY quarter_hour /app/quarter_hour
RUN python -m compileall -q quarter_hour
STOPSIGNAL SIGINT
CMD ["python", "-m", "quarter_hour.cloud"]
