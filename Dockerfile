FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
WORKDIR /app
COPY requirements-cloud.txt ./
RUN pip install --no-cache-dir -r requirements-cloud.txt \
    && useradd --uid 10001 --create-home monitor
COPY traveltime/ ./traveltime/
USER monitor
EXPOSE 8080
CMD ["sh", "-c", "exec gunicorn --bind :$PORT --workers 1 --threads 2 --timeout 110 --access-logfile - 'traveltime.cloud:create_app()'"]
