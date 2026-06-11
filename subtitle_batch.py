#!/usr/bin/env python3
"""
日文视频 -> 中文字幕（全本地）
ffmpeg 抽音 -> faster-whisper 转写(日文,CPU) -> LiteLLM/gemma 翻译 -> *.zh.srt

字幕写到每个视频所在目录、与视频平级，命名 <视频名>.zh.srt。
已存在 .zh.srt 的视频默认跳过（--force 覆盖）。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

VIDEO_EXTS = {".mp4", ".mkv", ".ts", ".mov", ".avi", ".webm", ".m4v", ".flv", ".wmv"}

# ---------- 工具 ----------

def log(msg):
    print(msg, flush=True)


def fmt_ts(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def find_videos(root: str):
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name.startswith("._"):          # macOS AppleDouble 残留，跳过
                continue
            if os.path.splitext(name)[1].lower() in VIDEO_EXTS:
                out.append(os.path.join(dirpath, name))
    out.sort()
    return out


def srt_path_for(video: str) -> str:
    base, _ext = os.path.splitext(video)
    return base + ".zh.srt"


# ---------- 音频 + 转写 ----------

def extract_audio(video: str, wav: str, max_seconds: int = 0):
    cmd = ["ffmpeg", "-y", "-i", video, "-vn", "-ac", "1", "-ar", "16000"]
    if max_seconds:
        cmd += ["-t", str(max_seconds)]
    cmd += ["-f", "wav", "-loglevel", "error", wav]
    subprocess.run(cmd, check=True)


def transcribe(model, wav: str, lang: str):
    segments, info = model.transcribe(
        wav,
        language=lang,
        beam_size=5,
        vad_filter=True,                       # 去静音，减少幻觉
        condition_on_previous_text=False,      # 降低长片重复打环风险
    )
    cues = []
    for seg in segments:
        text = seg.text.strip()
        if text:
            cues.append({"start": seg.start, "end": seg.end, "ja": text})
    return cues


# ---------- 翻译 ----------

def llm_call(url, key, llm_model, prompt, max_tokens):
    body = json.dumps({
        "model": llm_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.load(resp)
    return data["choices"][0]["message"]["content"].strip()


PROMPT_HEAD = (
    "你是字幕翻译。把下面带编号的日文台词逐行翻成简体中文。\n"
    "要求：每行输出一句，保持原编号，格式严格为「编号: 中文」；不要合并或拆分行，"
    "不要输出日文原文，不要任何解释或多余内容。\n\n"
)

LINE_RE = re.compile(r"^\s*(\d+)\s*[:：.、)]\s*(.*)$")


def parse_numbered(text, expected_ids):
    out = {}
    for line in text.splitlines():
        m = LINE_RE.match(line)
        if m:
            idx = int(m.group(1))
            if idx in expected_ids:
                out[idx] = m.group(2).strip()
    return out


def translate_cues(cues, url, key, llm_model, batch_size):
    n = len(cues)
    for start in range(0, n, batch_size):
        chunk = cues[start:start + batch_size]
        ids = [start + i + 1 for i in range(len(chunk))]
        numbered = "\n".join(f"{i}: {c['ja']}" for i, c in zip(ids, chunk))
        prompt = PROMPT_HEAD + numbered
        try:
            resp = llm_call(url, key, llm_model, prompt, max_tokens=2048)
            got = parse_numbered(resp, set(ids))
        except Exception as e:
            log(f"    [批 {ids[0]}-{ids[-1]}] 整批失败 ({e})，逐句重试")
            got = {}
        # 回填；缺失的逐句兜底
        for i, c in zip(ids, chunk):
            if i in got and got[i]:
                c["zh"] = got[i]
            else:
                c["zh"] = translate_one(c["ja"], url, key, llm_model)
        done = min(start + batch_size, n)
        log(f"    翻译 {done}/{n}")


def translate_one(ja, url, key, llm_model):
    prompt = ("把下面这句日文翻成简体中文，只输出译文，不要解释：\n" + ja)
    try:
        out = llm_call(url, key, llm_model, prompt, max_tokens=512)
        # 去掉模型可能加的编号/引号
        out = re.sub(r"^\s*\d+\s*[:：.、)]\s*", "", out).strip().strip('「」"\'')
        return out or ja
    except Exception as e:
        log(f"      逐句翻译失败 ({e})，保留日文")
        return ja


# ---------- 写 srt ----------

def write_srt(cues, path):
    parts = []
    for i, c in enumerate(cues, 1):
        parts.append(f"{i}\n{fmt_ts(c['start'])} --> {fmt_ts(c['end'])}\n{c.get('zh', c['ja'])}\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts) + "\n")


# ---------- 主流程 ----------

def process_one(model, video, args):
    out_srt = srt_path_for(video)
    if os.path.exists(out_srt) and not args.force:
        log(f"跳过（已有字幕）: {video}")
        return "skip"
    log(f"\n=== 处理: {video}")
    t0 = time.time()
    tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    try:
        log("  [1/3] 抽取音频…")
        extract_audio(video, tmp_wav, args.max_seconds)
        log("  [2/3] 转写日文（CPU，可能较久）…")
        cues = transcribe(model, tmp_wav, args.lang)
        if not cues:
            log("  未识别到任何语音，跳过。")
            return "empty"
        log(f"        识别到 {len(cues)} 句")
        log("  [3/3] 翻译为中文（gemma via LiteLLM）…")
        translate_cues(cues, args.litellm_url, args.api_key, args.llm_model, args.batch)
        write_srt(cues, out_srt)
        log(f"  完成 -> {out_srt}  (用时 {int(time.time()-t0)} 秒)")
        return "ok"
    finally:
        try:
            os.remove(tmp_wav)
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*", help="指定视频文件；留空则扫描 --dir")
    env = os.environ.get
    ap.add_argument("--dir", default=env("VIDEO_DIR", "/data/the-video"))
    ap.add_argument("--model", default=env("WHISPER_MODEL", "large-v3-turbo"), help="whisper 模型")
    ap.add_argument("--lang", default=env("SOURCE_LANG", "ja"))
    ap.add_argument("--device", default=env("WHISPER_DEVICE", "cpu"))
    ap.add_argument("--compute-type", default=env("WHISPER_COMPUTE_TYPE", "int8"))
    ap.add_argument("--threads", type=int, default=int(env("WHISPER_THREADS", "0")), help="whisper CPU 线程数(0=全部)")
    ap.add_argument("--litellm-url", default=env("LITELLM_URL", "http://localhost:4000/v1/chat/completions"))
    ap.add_argument("--api-key", default=env("TRANSLATE_KEY", "sk-MBfNTbEoXK6nDTCTATBBeg"))
    ap.add_argument("--llm-model", default=env("LLM_MODEL", "gemma-4-E2B-it-fast"))
    ap.add_argument("--batch", type=int, default=int(env("BATCH_SIZE", "30")), help="每批翻译的句数")
    ap.add_argument("--limit", type=int, default=0, help="最多处理几个视频(0=全部)")
    ap.add_argument("--max-seconds", type=int, default=0, help="只转写前N秒(0=整片，用于快速测试)")
    ap.add_argument("--force", action="store_true", help="已有字幕也重做")
    args = ap.parse_args()

    if args.paths:
        videos = [p for p in args.paths if not os.path.basename(p).startswith("._")]
    else:
        videos = find_videos(args.dir)
    if args.limit:
        # 优先挑还没字幕的来跑
        pending = [v for v in videos if not (os.path.exists(srt_path_for(v)) and not args.force)]
        videos = pending[:args.limit]

    log(f"待处理视频: {len(videos)} 个")
    if not videos:
        return

    log(f"加载 whisper 模型: {args.model} ({args.device}/{args.compute_type}) …首次会下载")
    from faster_whisper import WhisperModel
    model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type,
                         cpu_threads=args.threads)

    stats = {"ok": 0, "skip": 0, "empty": 0, "fail": 0}
    for v in videos:
        try:
            stats[process_one(model, v, args)] += 1
        except Exception as e:
            log(f"  !! 失败: {v}\n     {e}")
            stats["fail"] += 1
    log(f"\n全部结束: {stats}")


if __name__ == "__main__":
    main()
