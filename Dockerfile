FROM python:3.12-slim

# ffmpeg 用于抽音轨；faster-whisper 做转写
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir faster-whisper==1.2.1

# 模型缓存 / HOME 都指向挂载卷，避免每次重启重下模型
ENV HF_HOME=/cache \
    XDG_CACHE_HOME=/cache \
    HOME=/cache \
    PYTHONUNBUFFERED=1

COPY subtitle_batch.py /app/subtitle_batch.py
COPY entrypoint.sh    /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
