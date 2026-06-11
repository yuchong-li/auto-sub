# subtitle-translator

A small, self-hostable tool that batch-generates translated subtitles for your videos. It scans a directory, transcribes every video that doesn't have subtitles yet, translates the transcript with an LLM, and writes a `.zh.srt` next to the video.

Pipeline: `ffmpeg (extract audio) → faster-whisper (transcribe) → LLM (translate) → subtitle file`.

Videos that already have a subtitle are skipped, so you can stop and restart any time and it resumes where it left off.

## Quick start

```bash
cp .env.example .env        # then fill in your LLM endpoint, model, and key
docker compose up -d --build

docker compose logs -f      # watch progress
docker compose down         # stop
```

It runs once on container start, then re-scans daily at `SCAN_HOUR`. Each run only fills in videos that are still missing a subtitle.

## Configuration

Everything lives in `.env`:

| Variable | What it does |
|---|---|
| `VIDEO_DIR` | Directory to scan for videos |
| `WHISPER_MODEL` | faster-whisper model, e.g. `large-v3-turbo`, `medium`, `small` |
| `SOURCE_LANG` | Language of the audio, e.g. `ja`, `en` |
| `WHISPER_DEVICE` | `cpu` or `cuda` |
| `WHISPER_THREADS` | CPU threads for transcription (`0` = all cores) |
| `WHISPER_COMPUTE_TYPE` | e.g. `int8` (CPU), `float16` (GPU) |
| `LITELLM_URL` | Any OpenAI-compatible `/chat/completions` endpoint |
| `LLM_MODEL` | Model name to request from that endpoint |
| `TRANSLATE_KEY` | API key / token for the endpoint |
| `BATCH_SIZE` | Subtitle lines sent per translation request |
| `SCAN_HOUR` | Hour of day (0–23) to re-scan |
| `CPU_SHARES` | Relative Docker CPU weight; lower = yields more under contention |
| `PUID` / `PGID` | UID/GID the container runs as; output subtitles are owned by them |
| `LLM_NETWORK` | External Docker network to join in order to reach a gateway (see notes) |

## Notes

- **Translation backend** is any OpenAI-compatible Chat Completions endpoint — a local gateway (LiteLLM, vLLM, Ollama, …) or a cloud API. Point `LITELLM_URL`/`LLM_MODEL`/`TRANSLATE_KEY` at whatever you use; pick a local one if you want everything to stay offline.
- **Transcription** can run on CPU or GPU; device, threads, and CPU weight are all configurable, so tune resource usage to your machine.
- The whisper model is cached in `./cache` and downloaded once on first run.
- **Networking**: by default the container joins an external Docker network (`LLM_NETWORK`) so it can reach a gateway running on the same host by service name. If your endpoint is a public URL (or otherwise directly reachable), just delete the `networks:` blocks from `docker-compose.yml`.
- Output is written as `<video>.zh.srt` next to each video. Translations target **Simplified Chinese** by default — this is set by the prompt in `subtitle_batch.py`; edit it (and the output suffix) for other target languages.
- AppleDouble junk files (`._*`) are ignored automatically.

## Run without Docker

The translator is a single dependency-light script. You can run it directly:

```bash
pip install faster-whisper        # ffmpeg must be on PATH
python subtitle_batch.py --dir /path/to/videos
```

All settings can be passed as flags (`--model`, `--lang`, `--threads`, `--litellm-url`, `--llm-model`, `--batch`, …) or via the same environment variables used by the container.

## License

[MIT](LICENSE)
