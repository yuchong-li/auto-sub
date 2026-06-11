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
import queue
import re
import subprocess
import sys
import threading
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
    data = "\n".join(parts) + "\n"
    # 原子写：先写同目录临时文件再 os.replace，绝不会留半截 .zh.srt 被误判为完成
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(suffix=".srt.tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ---------- 主流程（转写/翻译流水线）----------

def transcribe_video(model, video, args):
    """CPU 阶段：抽音 + 转写，返回 cues（用完即删临时音频）。"""
    tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    try:
        log(f"[转写] 抽音+识别: {video}")
        extract_audio(video, tmp_wav, args.max_seconds)
        cues = transcribe(model, tmp_wav, args.lang)
        log(f"[转写] 识别到 {len(cues)} 句: {video}")
        return cues
    finally:
        try:
            os.remove(tmp_wav)
        except OSError:
            pass


def translate_video(video, cues, args):
    """GPU 阶段：翻译 + 写 srt。"""
    t0 = time.time()
    log(f"[翻译] 开始（{len(cues)} 句）: {video}")
    translate_cues(cues, args.litellm_url, args.api_key, args.llm_model, args.batch)
    write_srt(cues, srt_path_for(video))
    log(f"[翻译] 完成 -> {srt_path_for(video)}  (翻译用时 {int(time.time()-t0)} 秒)")


def run_pipeline(videos, model, args):
    """转写(CPU)与翻译(GPU)解耦：CPU 转完一个立刻转下一个，
    翻译线程并行处理已转好的，避免转写时 CPU 干等 GPU。"""
    q = queue.Queue(maxsize=2)            # 缓冲尽量小：崩溃时最多丢这么多"转好待翻"的工作
    stats = {"ok": 0, "skip": 0, "empty": 0, "fail": 0}
    lock = threading.Lock()

    def bump(key):
        with lock:
            stats[key] += 1

    def producer():                        # CPU：逐个转写，喂给队列
        for v in videos:
            if os.path.exists(srt_path_for(v)) and not args.force:
                log(f"跳过（已有字幕）: {v}")
                bump("skip")
                continue
            try:
                cues = transcribe_video(model, v, args)
            except Exception as e:
                log(f"  !! 转写失败: {v}\n     {e}")
                bump("fail")
                continue
            if not cues:
                log(f"  未识别到语音，跳过: {v}")
                bump("empty")
                continue
            q.put((v, cues))
        q.put(None)                        # 结束信号

    def consumer():                        # GPU：从队列取，翻译并写 srt
        while True:
            item = q.get()
            if item is None:
                break
            v, cues = item
            try:
                translate_video(v, cues, args)
                bump("ok")
            except Exception as e:
                log(f"  !! 翻译失败: {v}\n     {e}")
                bump("fail")

    t_cons = threading.Thread(target=consumer, name="translate", daemon=True)
    t_prod = threading.Thread(target=producer, name="transcribe", daemon=True)
    t_cons.start()
    t_prod.start()
    t_prod.join()
    t_cons.join()
    return stats


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

    stats = run_pipeline(videos, model, args)
    log(f"\n全部结束: {stats}")


if __name__ == "__main__":
    main()
