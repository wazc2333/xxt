FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ /app/backend/
COPY playback_simulator/__init__.py playback_simulator/simulator.py playback_simulator/chaoxing_client.py /app/playback_simulator/
RUN useradd --system --uid 10001 --create-home xxt && mkdir /data && chown xxt:xxt /data
USER 10001:10001
EXPOSE 8080
VOLUME ["/data"]
CMD ["python", "-m", "backend.server", "--host", "0.0.0.0", "--port", "8080", "--public-origin", "http://localhost:8080", "--data-dir", "/data"]
