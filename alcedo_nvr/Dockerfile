ARG BUILD_FROM
FROM ${BUILD_FROM}

# ffmpeg: recording (stream copy), clip assembly, motion frames, image scaling
RUN apk add --no-cache ffmpeg tzdata

COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY rootfs /
WORKDIR /app
ENV PYTHONUNBUFFERED=1
CMD ["python3", "-m", "alcedo"]
