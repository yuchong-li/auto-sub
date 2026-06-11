[English](README.md) | 中文

# auto-sub

一个可自托管的小工具，批量给视频生成**简体中文**字幕。它扫描一个目录，对还没字幕的视频转写音频、用 LLM 翻成中文，并把 `<视频名>.zh.srt` 写到视频同目录。

> **输出语言：仅简体中文**（`*.zh.srt`）。**源**语言可配置（`SOURCE_LANG`）；**目标**语言由 `subtitle_batch.py` 里的翻译 prompt 固定为中文——要翻成别的语言，改那段 prompt 和 `.zh.srt` 后缀即可。

链路：`ffmpeg 抽音 → faster-whisper 转写 → LLM 翻成中文 → .zh.srt`。

已有字幕的视频自动跳过，可随时停/重启续跑。

## 快速开始

```bash
cp .env.example .env        # 首次：填好 LLM 接口地址、模型、key
docker compose up -d --build

docker compose logs -f      # 看进度
docker compose down         # 停
```

容器启动即扫一遍，之后每天 `SCAN_HOUR` 点再扫；每轮只补还没字幕的视频。

## 配置

全部都在 `.env` 里：

| 变量 | 作用 |
|---|---|
| `VIDEO_DIR` | 要扫描的视频目录 |
| `WHISPER_MODEL` | faster-whisper 模型，如 `large-v3-turbo`、`medium`、`small` |
| `SOURCE_LANG` | 音频源语言，如 `ja`、`en` |
| `WHISPER_DEVICE` | `cpu` 或 `cuda` |
| `WHISPER_THREADS` | 转写用的 CPU 线程数（`0` = 全部核心） |
| `WHISPER_COMPUTE_TYPE` | 如 `int8`（CPU）、`float16`（GPU） |
| `LITELLM_URL` | 任意 OpenAI 兼容的 `/chat/completions` 接口 |
| `LLM_MODEL` | 向该接口请求的模型名 |
| `TRANSLATE_KEY` | 接口的 API key / token |
| `BATCH_SIZE` | 每次翻译请求发送的字幕句数 |
| `SCAN_HOUR` | 每天几点（0–23）重新扫描 |
| `CPU_SHARES` | Docker 相对 CPU 权重，越低越容易在争抢时让出 |
| `PUID` / `PGID` | 容器运行身份的 UID/GID，生成的字幕归属该用户 |
| `LLM_NETWORK` | 为连到网关而加入的外部 Docker 网络（见说明） |

## 说明

- **只输出简体中文**（`.zh.srt`）。要翻成别的语言，改 `subtitle_batch.py` 里的翻译 prompt 和输出后缀。
- **翻译后端**是任意 OpenAI 兼容的 Chat Completions 接口——本地网关（LiteLLM、vLLM、Ollama……）或云 API 都行。把 `LITELLM_URL`/`LLM_MODEL`/`TRANSLATE_KEY` 指向你用的那个；想全程不出网就用本地的。
- **转写**可跑在 CPU 或 GPU；设备、线程、CPU 权重都可配，按机器调资源占用。
- whisper 模型缓存在 `./cache`，首次启动下载一次，之后复用。
- **网络**：容器默认加入一个外部 Docker 网络（`LLM_NETWORK`），以便用服务名连到同机网关。如果你的接口是公网 URL（或本就可直达），把 `docker-compose.yml` 里的 `networks:` 段删掉即可。
- 自动忽略 macOS 的 `._*` 垃圾文件。

## 不用 Docker 直接跑

翻译器就是个依赖很少的单文件脚本，可直接运行：

```bash
pip install faster-whisper        # 需要系统里有 ffmpeg
python subtitle_batch.py --dir /path/to/videos
```

所有选项都能用命令行参数传（`--model`、`--lang`、`--threads`、`--litellm-url`、`--llm-model`、`--batch`……），或用与容器相同的环境变量。

## 许可证

[MIT](LICENSE)
