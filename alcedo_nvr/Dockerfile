ARG BUILD_FROM
FROM ${BUILD_FROM}

# ffmpeg: recording (stream copy), clip assembly, motion frames, image scaling
RUN apk add --no-cache ffmpeg tzdata font-dejavu

COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY rootfs /
WORKDIR /app
ENV PYTHONUNBUFFERED=1
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s \
  CMD python3 -c "import socket; socket.create_connection(('127.0.0.1', 8765), 3)"
CMD ["python3", "-m", "alcedo"]
