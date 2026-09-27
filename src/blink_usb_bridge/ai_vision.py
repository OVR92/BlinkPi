"""AI descriptions of motion clips.

For each new clip we pull a handful of frames with ffmpeg (either N
evenly spaced frames or one every K seconds), send them to a vision
model with a short prompt, and store the answer as JSON in
<project_dir>/analysis/. The clip browser shows the description under
the clip, and the notifier can include it in the new-clip email/webhook.

Provider
--------
Any OpenAI-compatible chat-completions endpoint: OpenAI, OpenRouter, Groq,
Google's OpenAI-compatible endpoint, a local Ollama or LM Studio, ...
Configure `base_url`, `model` and (if the service needs one) the API key,
which lives in secrets.yaml as `ai_api_key`. Plain HTTP, no SDK needed.

Cost control: `ai.max_per_hour` caps how many clips are analysed per
hour (a sliding window kept in analysis/_budget.json); frames are
downscaled to 768 px wide; the request asks for a compact answer.

Home Assistant users may prefer LLM Vision (https://llmvision.org),
which can analyse the same clips from the SMB share with any provider
it supports - the webhook payload carries the clip's path for that.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config as cfg

log = logging.getLogger(__name__)

MAX_FRAMES = 20
FRAME_WIDTH = 768

DEFAULT_PROMPT = (
    "These are frames from a motion clip recorded by a home security camera "
    "named \"{camera}\" at {when}. Describe in two or three plain sentences what "
    "happens: who or what is present (people, vehicles, animals, deliveries), "
    "what they do, and anything unusual. Do not speculate about identities."
)

SYSTEM = (
    "You summarise home security camera clips for the homeowner. Be concrete and brief. "
    "Reply with a JSON object with keys: description (string, 2-3 sentences), "
    "alert (boolean: true if a person or vehicle is present or something looks unusual), "
    "tags (array of up to five short lowercase keywords such as person, car, cat, delivery, night). "
    "Return only the JSON."
)

# Presets for common services. Model names are deliberately not hard-coded;
# the setup page fetches them from <base_url>/models.
PRESETS = [
    {"id": "openai", "label": "OpenAI", "base_url": "https://api.openai.com/v1", "needs_key": True},
    {"id": "openrouter", "label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "needs_key": True},
    {"id": "groq", "label": "Groq", "base_url": "https://api.groq.com/openai/v1", "needs_key": True},
    {"id": "gemini", "label": "Google Gemini (OpenAI-compatible)", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "needs_key": True},
    {"id": "ollama", "label": "Ollama (local)", "base_url": "http://127.0.0.1:11434/v1", "needs_key": False},
    {"id": "lmstudio", "label": "LM Studio (local)", "base_url": "http://127.0.0.1:1234/v1", "needs_key": False},
    {"id": "custom", "label": "Custom endpoint", "base_url": "", "needs_key": False},
]


def analysis_path(c: cfg.Config, rel_path: str) -> Path:
    key = hashlib.sha256(rel_path.encode()).hexdigest()
    return c.analysis_dir / f"{key}.json"


def load_analysis(c: cfg.Config, rel_path: str) -> Optional[dict]:
    p = analysis_path(c, rel_path)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except (OSError, json.JSONDecodeError):
        return None


# ──────────────────────────── frames ────────────────────────────

def frame_timestamps(duration: float, frames: int, interval: float) -> list[float]:
    """Where to sample. interval > 0 wins over a frame count."""
    duration = max(duration, 0.2)
    if interval and interval > 0:
        ts = []
        t = min(0.5, duration / 2)
        while t < duration and len(ts) < MAX_FRAMES:
            ts.append(round(t, 2))
            t += interval
        return ts or [0.0]
    n = max(1, min(int(frames or 4), MAX_FRAMES))
    return [round(duration * (i + 0.5) / n, 2) for i in range(n)]


def extract_frames(clip: Path, out_dir: Path, timestamps: list[float]) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    frames: list[Path] = []
    for i, t in enumerate(timestamps):
        dest = out_dir / f"frame_{i:02d}.jpg"
        cmd = [
            "ffmpeg", "-v", "error", "-ss", str(t), "-i", str(clip),
            "-vframes", "1", "-vf", f"scale={FRAME_WIDTH}:-2", "-q:v", "4",
            "-f", "image2", str(dest), "-y",
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=30)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            log.warning("frame extraction failed at %.1fs: %s", t, e)
            continue
        if r.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
            frames.append(dest)
    return frames


# ──────────────────────────── budget ────────────────────────────

def _budget_file(c: cfg.Config) -> Path:
    return c.analysis_dir / "_budget.json"


def budget_allows(c: cfg.Config, now: Optional[float] = None) -> bool:
    limit = int(c.ai.max_per_hour or 0)
    if limit <= 0:
        return True
    now = now or time.time()
    stamps = [s for s in _load_budget(c) if now - s < 3600]
    _save_budget(c, stamps)
    return len(stamps) < limit


def budget_record(c: cfg.Config, now: Optional[float] = None) -> None:
    now = now or time.time()
    stamps = [s for s in _load_budget(c) if now - s < 3600]
    stamps.append(now)
    _save_budget(c, stamps)


def _load_budget(c: cfg.Config) -> list[float]:
    try:
        return [float(x) for x in json.loads(_budget_file(c).read_text())]
    except (OSError, ValueError, TypeError):
        return []


def _save_budget(c: cfg.Config, stamps: list[float]) -> None:
    try:
        c.analysis_dir.mkdir(parents=True, exist_ok=True)
        _budget_file(c).write_text(json.dumps(stamps))
    except OSError as e:
        log.warning("could not write AI budget file: %s", e)


# ──────────────────────────── answer parsing ────────────────────────────

def _parse_answer(text: str) -> dict:
    """Pull the JSON object out of the reply; fall back to plain text."""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict) and obj.get("description"):
                return {
                    "description": str(obj.get("description", "")).strip(),
                    "alert": bool(obj.get("alert", False)),
                    "tags": [str(t).lower()[:24] for t in (obj.get("tags") or [])][:5],
                }
        except json.JSONDecodeError:
            pass
    return {"description": text.strip()[:600], "alert": False, "tags": []}


def _b64(path: Path) -> str:
    return base64.standard_b64encode(path.read_bytes()).decode("ascii")


# ──────────────────────────── providers ────────────────────────────

def openai_headers(api_key: str) -> dict:
    h = {"Content-Type": "application/json", "User-Agent": "BlinkPi",
         # OpenRouter attributes usage to the app name; harmless elsewhere.
         "X-Title": "BlinkPi"}
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
    return h


def _http_json(method: str, url: str, headers: dict, body: Optional[dict], timeout: int) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")[:2000]
    except (urllib.error.URLError, OSError, ValueError) as e:
        return 0, f"{type(e).__name__}: {e}"


def _describe(c: cfg.Config, frames: list[Path], prompt: str) -> tuple[Optional[dict], str]:
    base = (c.ai.base_url or "").rstrip("/")
    if not base:
        return None, "no base_url configured"
    api_key = c.secrets.get("ai_api_key", "")
    model = c.ai.model
    if not model:
        return None, "no model configured"

    content: list[dict] = [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_b64(f)}"}}
        for f in frames
    ]
    content.append({"type": "text", "text": prompt})
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": content},
        ],
    }
    status, text = _http_json("POST", f"{base}/chat/completions", openai_headers(api_key), body, timeout=120)
    if status == 0:
        return None, f"could not reach {base}: {text}"
    if status == 401 or status == 403:
        return None, f"API key rejected by {base} (HTTP {status})"
    if status == 429:
        return None, "rate limited by the provider; will not retry this clip"
    if status >= 400:
        return None, f"provider error HTTP {status}: {text[:300]}"
    try:
        data = json.loads(text)
        message = data["choices"][0]["message"]
        answer = message.get("content")
        if isinstance(answer, list):  # some providers return content parts
            answer = "".join(p.get("text", "") for p in answer if isinstance(p, dict))
        answer = str(answer or "")
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        return None, f"unexpected response from provider: {e}: {text[:200]}"
    if not answer.strip():
        return None, "provider returned an empty answer"
    result = _parse_answer(answer)
    usage = data.get("usage") or {}
    result["usage"] = {
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
    }
    result["model"] = model
    return result, "ok"


def list_models(base_url: str = "", api_key: str = "") -> tuple[list[str], str]:
    """Model ids offered by the endpoint (GET <base_url>/models), for the setup page."""
    base = (base_url or "").rstrip("/")
    if not base:
        return [], "base URL is empty"
    status, text = _http_json("GET", f"{base}/models", openai_headers(api_key), None, timeout=30)
    if status == 0:
        return [], f"could not reach {base}: {text}"
    if status >= 400:
        return [], f"HTTP {status}: {text[:200]}"
    try:
        data = json.loads(text)
        items = data.get("data") if isinstance(data, dict) else data
        ids = sorted({str(m.get("id") or m.get("name")) for m in items if isinstance(m, dict)})
        return [i for i in ids if i and i != "None"], "ok"
    except (json.JSONDecodeError, AttributeError, TypeError) as e:
        return [], f"unexpected /models response: {e}"


def describe_frames(c: cfg.Config, frames: list[Path], camera: str, when: str) -> tuple[Optional[dict], str]:
    """Ask the configured provider. Returns (result, message); result is None on failure."""
    if not frames:
        return None, "no frames extracted"
    prompt = (c.ai.prompt or DEFAULT_PROMPT).format(camera=camera, when=when, frames=len(frames))
    return _describe(c, frames, prompt)


# ──────────────────────────── entry point ────────────────────────────

def analyze_clip(
    c: cfg.Config, clip_path: Path, rel_path: str, camera: str,
    duration: float, when_local: datetime, *, force: bool = False,
) -> tuple[Optional[dict], str]:
    """Analyse one clip and persist the result. Returns (result, message)."""
    if not force:
        if not c.ai.enabled:
            return None, "AI analysis disabled"
        if c.ai.cameras and camera not in c.ai.cameras:
            return None, f"camera {camera} not selected for analysis"
        existing = load_analysis(c, rel_path)
        if existing:
            return existing, "already analysed"
        if not budget_allows(c):
            return None, f"hourly AI budget of {c.ai.max_per_hour} clips reached"

    timestamps = frame_timestamps(duration, c.ai.frames, c.ai.frame_interval_seconds)
    with tempfile.TemporaryDirectory(prefix="bub-frames-") as tmp:
        frames = extract_frames(clip_path, Path(tmp), timestamps)
        if not frames:
            return None, "ffmpeg could not extract any frames"
        budget_record(c)
        when = when_local.strftime("%A %Y-%m-%d %H:%M")
        result, msg = describe_frames(c, frames, camera, when)
    if result is None:
        return None, msg

    record = {
        "rel_path": rel_path, "camera": camera, "timestamp": when_local.isoformat(timespec="seconds"),
        "duration": duration, "frames": len(frames), "frame_times": timestamps[:len(frames)],
        "created": datetime.now().isoformat(timespec="seconds"), **result,
    }
    try:
        c.analysis_dir.mkdir(parents=True, exist_ok=True)
        p = analysis_path(c, rel_path)
        tmpf = p.with_suffix(".tmp")
        tmpf.write_text(json.dumps(record, indent=2), encoding="utf-8")
        tmpf.replace(p)
    except OSError as e:
        log.warning("could not save analysis: %s", e)
    log.info("AI [%s]: %s", camera, record["description"][:160])
    return record, "ok"
