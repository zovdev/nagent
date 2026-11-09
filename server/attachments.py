"""Attachments (§10 roadmap): images, video, audio, files.

Storage: the workspace `attachments/` folder (jail-rooted). The SERVER writes
uploads there directly — this is user input, not an agent tool call, so no
permission gate applies. Quotas are an accounts-mode concept (§10) — solo
keeps a hard size cap only.

Feeding the model — the capability matrix, driven by EXPLICIT declarations
only (catalog API fields / manual override; never guessed from a name):

| kind  | capability | how it is sent |
|---|---|---|
| image | vision     | OpenAI `image_url` data-URL part |
| image | no vision  | `[изображение: name]` placeholder (§10) |
| video | video      | `video_url` data-URL part (Qwen-VL / Gemini-compat style) |
| video | no video   | ffmpeg keyframes as image parts + note; no ffmpeg → placeholder |
| audio | audio      | OpenAI `input_audio` data part |
| audio | no audio   | `[аудио: name]` placeholder |
| file  | —          | inline text preview (≤ PREVIEW_BYTES, utf-8) or a path note |

ffmpeg is optional: when absent the video fallback degrades to a placeholder
instead of failing the turn. Audio→ASR transcription needs a configured
speech model and is deliberately NOT faked here.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import uuid

from .jail import WorkspaceJail

ATTACHMENTS_DIR = "attachments"
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024     # solo-mode hard cap (§10)
PREVIEW_BYTES = 4096
VIDEO_MAX_FRAMES = 8

_EXT_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
    ".avi": "video/x-msvideo", ".mkv": "video/x-matroska", ".flv": "video/x-flv",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".flac": "audio/flac", ".m4a": "audio/mp4", ".aac": "audio/aac",
}


def sniff_mime(name: str, declared: str | None, head: bytes) -> str:
    """Minimal magic-byte sniffing with an ext/declared-type fallback."""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"GIF8"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF
                                   and (head[1] & 0xE0) == 0xE0):
        return "audio/mpeg"
    ext = os.path.splitext(name or "")[1].lower()
    if ext in _EXT_MIME:
        return _EXT_MIME[ext]
    if declared and "/" in declared:
        return declared
    return "application/octet-stream"


def attachment_kind(mime: str) -> str:
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    return "file"


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


class AttachmentStore:
    def __init__(self, jail: WorkspaceJail):
        self.jail = jail
        self.dir = os.path.join(jail.root, ATTACHMENTS_DIR)
        os.makedirs(self.dir, exist_ok=True)

    def save(self, name: str, content: bytes, declared_mime: str | None) -> dict:
        if len(content) > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"вложение больше лимита "
                             f"({len(content)} > {MAX_ATTACHMENT_BYTES} байт)")
        mime = sniff_mime(name, declared_mime, content[:16])
        safe = (os.path.basename(name or "file") or "file").replace(
            os.sep, "_")
        stem, ext = os.path.splitext(safe)
        att_id = uuid.uuid4().hex[:12]
        rel = f"{ATTACHMENTS_DIR}/{att_id}{ext}"
        full = self.jail.resolve(rel)          # canonical + inside-jail check
        with open(full, "wb") as f:
            f.write(content)
        return {"id": att_id, "name": safe, "path": rel, "mime": mime,
                "size": len(content), "kind": attachment_kind(mime)}

    def load(self, att: dict) -> bytes | None:
        try:
            full = self.jail.check_read(att["path"])
            with open(full, "rb") as f:
                return f.read()
        except (KeyError, OSError):
            return None

    def video_frames(self, att: dict) -> list[bytes] | None:
        """ffmpeg keyframes (1 fps, capped) as JPEG bytes; None = unavailable."""
        if not ffmpeg_available():
            return None
        try:
            full = self.jail.check_read(att["path"])
        except Exception:                              # noqa: BLE001
            return None
        out_dir = os.path.join(self.dir, f"{att['id']}_frames")
        os.makedirs(out_dir, exist_ok=True)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", full,
               "-vf", f"fps=1,scale=640:-1", "-frames:v", str(VIDEO_MAX_FRAMES),
               "-q:v", "5", os.path.join(out_dir, "f_%02d.jpg")]
        try:
            subprocess.run(cmd, check=True, timeout=60,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (subprocess.SubprocessError, OSError):
            return None
        frames = []
        for fname in sorted(os.listdir(out_dir))[:VIDEO_MAX_FRAMES]:
            path = os.path.join(out_dir, fname)
            try:
                with open(path, "rb") as f:
                    frames.append(f.read())
            except OSError:
                continue
        return frames or None


# ------------------------------------------------------------------ rendering

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def attachment_parts(text: str, attachments: list[dict] | None,
                     store: AttachmentStore, caps: dict | None) -> list[dict]:
    """(text + attachments) -> OpenAI multimodal content parts.

    `caps` is the model's EXPLICIT capability dict from the catalog/override
    (absent key = the API never declared it = NOT capable; no name guessing).
    Parts must be returned non-empty: a plain text part always leads.
    """
    parts: list[dict] = [{"type": "text", "text": text or ""}]
    caps = caps or {}
    for att in attachments or []:
        kind = att.get("kind") or attachment_kind(att.get("mime", ""))
        name = att.get("name", "file")
        data = store.load(att) if store else None

        if data is None:
            parts.append({"type": "text",
                          "text": f"[{kind}: {name} — не читается]"})
            continue

        if kind == "image":
            if caps.get("vision"):
                parts.append({"type": "image_url", "image_url": {
                    "url": f"data:{att['mime']};base64,{_b64(data)}"}})
            else:
                # §10: no vision capability declared → placeholder (no VLM
                # call is invented here; the user can wire one explicitly)
                parts.append({"type": "text",
                              "text": f"[изображение: {name}, {att['size']} байт]"})
        elif kind == "video":
            if caps.get("video"):
                parts.append({"type": "video_url", "video_url": {
                    "url": f"data:{att['mime']};base64,{_b64(data)}"}})
            else:
                frames = store.video_frames(att) if store else None
                if frames:
                    parts.append({"type": "text",
                                  "text": f"[видео {name} подано кадрами: "
                                          f"{len(frames)} × 1 fps]"})
                    for jpeg in frames:
                        parts.append({"type": "image_url", "image_url": {
                            "url": f"data:image/jpeg;base64,{_b64(jpeg)}"}})
                else:
                    parts.append({"type": "text",
                                  "text": f"[видео: {name}, {att['size']} байт — "
                                          f"модель без видео-ввода, ffmpeg недоступен]"})
        elif kind == "audio":
            if caps.get("audio"):
                fmt = "wav" if "wav" in (att.get("mime") or "") else "mp3"
                parts.append({"type": "input_audio", "input_audio": {
                    "data": _b64(data), "format": fmt}})
            else:
                parts.append({"type": "text",
                              "text": f"[аудио: {name}, {att['size']} байт — "
                                      f"модель без аудио-ввода]"})
        else:  # generic file: text preview inline, binary → path note
            if b"\x00" in data[:1024]:
                parts.append({"type": "text",
                              "text": f"[файл: {name}, {att['size']} байт — "
                                      f"бинарный; лежит в рабочей папке: {att['path']}"})
            else:
                decoded = data[:PREVIEW_BYTES].decode("utf-8", "replace")
                parts.append({"type": "text",
                              "text": f"[файл: {name}]\n```\n{decoded}\n```"
                                      + ("…" if len(data) > PREVIEW_BYTES else "")})
    return parts


def describe_attachment(att: dict) -> str:
    return f"{att.get('name')} ({att.get('kind')}, {att.get('size')} байт)"


def dumps_attachments(atts: list[dict] | None) -> str:
    return json.dumps(atts or [], ensure_ascii=False)
