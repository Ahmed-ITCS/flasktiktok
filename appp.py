import os
import re
import tempfile
from typing import List

from flask import Flask, request, render_template, send_file, abort
from werkzeug.utils import secure_filename
from moviepy.editor import (
    VideoFileClip,
    AudioFileClip,
    TextClip,
    CompositeVideoClip,
    concatenate_audioclips,
)
import requests

# ------------------ Config ------------------
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 1 GB

ELEVENLABS_API_KEY = "sk_a931e4d3030370fb262a7aae1dcfd51b4d33dfaa0b7d408b"
ELEVEN_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech"
ELEVEN_VOICES_URL = "https://api.elevenlabs.io/v1/voices"

ALLOWED_VIDEO = {"mp4"}
ALLOWED_TEXT = {"txt"}


# ------------------ Helpers ------------------
def allowed_ext(filename: str, allowed: set) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed


def fetch_voices():
    """Fetch your ElevenLabs voices (for the dropdown)."""
    headers = {"xi-api-key": ELEVENLABS_API_KEY}
    try:
        r = requests.get(ELEVEN_VOICES_URL, headers=headers, timeout=30)
        if r.status_code == 200:
            return r.json().get("voices", [])
    except Exception:
        pass
    return []


def split_into_sentences(text: str) -> List[str]:
    """
    Simple sentence splitter. Keeps punctuation. Also breaks very long sentences by commas to avoid huge clips.
    """
    # primary split by sentence punctuation
    parts = re.findall(r".+?(?:[.!?](?=\s)|$)", text, flags=re.S)
    sentences = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(p) > 220:
            # further split long ones by commas
            subparts = re.split(r"(,)", p)  # preserve commas
            chunk = ""
            for sp in subparts:
                if sp == ",":
                    chunk += sp
                    continue
                candidate = (chunk + " " + sp).strip()
                if len(candidate) > 220 and chunk:
                    sentences.append(chunk.strip())
                    chunk = sp
                else:
                    chunk = candidate
            if chunk.strip():
                sentences.append(chunk.strip())
        else:
            sentences.append(p)
    return sentences


def tts_elevenlabs(text: str, voice_id: str, out_path: str):
    """
    Generate TTS for a single sentence using ElevenLabs.
    Returns the saved file path.
    """
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
    }
    payload = {
        "text": text,
        "voice_settings": {"stability": 0.7, "similarity_boost": 0.7},
    }
    url = f"{ELEVEN_TTS_URL}/{voice_id}?optimize_streaming_latency=0"
    r = requests.post(url, headers=headers, json=payload, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"ElevenLabs TTS error: {r.status_code} {r.text[:300]}")
    with open(out_path, "wb") as f:
        f.write(r.content)
    return out_path


def build_caption_clip(text: str, start: float, duration: float, video_w: int, video_h: int):
    """
    Creates a nice readable caption TextClip for the time window.
    Requires ImageMagick installed for moviepy TextClip (method='caption').
    """
    # A semi-opaque background improves readability; using caption to wrap lines.
    txt = TextClip(
    txt=text,
    fontsize=20,
    color="white",
    method="caption",
    size=(int(video_w * 0.88), None),
    align="center",
    stroke_color="white",
    stroke_width=2,
    )

    return (
    txt.set_start(start)
       .set_duration(max(0.01, duration))
       .set_position(("center", "center"))  # 👈 puts text in the middle of the screen
    )


# ------------------ Routes ------------------
@app.route("/", methods=["GET"])
def index():
    voices = fetch_voices() if ELEVENLABS_API_KEY else []
    return render_template("index.html", voices=voices)


@app.route("/process", methods=["POST"])
def process():
    if not ELEVENLABS_API_KEY:
        abort(400, "Server missing ELEVENLABS_API_KEY")

    if "video" not in request.files or "text" not in request.files:
        abort(400, "Missing files")

    voice_id = request.form.get("voice_id", "").strip()
    if not voice_id:
        abort(400, "Missing ElevenLabs voice_id")

    video_file = request.files["video"]
    text_file = request.files["text"]

    if not allowed_ext(video_file.filename, ALLOWED_VIDEO):
        abort(400, "Video must be .mp4")
    if not allowed_ext(text_file.filename, ALLOWED_TEXT):
        abort(400, "Transcript must be .txt")

    # Work in a temp directory, then return the final file
    tmp = tempfile.mkdtemp(prefix="sync_")
    video_path = os.path.join(tmp, secure_filename(video_file.filename))
    text_path = os.path.join(tmp, secure_filename(text_file.filename))

    video_file.save(video_path)
    text_file.save(text_path)

    # Read transcript & split to sentences
    with open(text_path, "r", encoding="utf-8") as f:
        transcript = f.read().strip()
    sentences = split_into_sentences(transcript)
    if not sentences:
        abort(400, "Transcript is empty after parsing.")

    # Generate audio per sentence
    audio_segments = []
    durations = []
    for i, s in enumerate(sentences):
        seg_path = os.path.join(tmp, f"seg_{i:04d}.mp3")
        tts_elevenlabs(s, voice_id, seg_path)
        clip = AudioFileClip(seg_path)
        durations.append(clip.duration)
        audio_segments.append(clip)

    # Concatenate all sentence audios
    narration = concatenate_audioclips(audio_segments)

    # Load video and clip/extend to narration length
    video = VideoFileClip(video_path)
    # If video is longer → trim; if shorter → loop to match narration
    if video.duration >= narration.duration:
        video = video.subclip(0, narration.duration)
    else:
        # loop the video to fill full narration duration
        loops = int(narration.duration // video.duration) + 1
        from moviepy.editor import concatenate_videoclips

        video = concatenate_videoclips([VideoFileClip(video_path)] * loops).subclip(0, narration.duration)

    # Attach audio
    video = video.set_audio(narration)

    # Build caption clips
    caption_clips = []
    t = 0.0
    for s, d in zip(sentences, durations):
        caption_clips.append(build_caption_clip(s, t, d, video.w, video.h))
        t += d

    final = CompositeVideoClip([video, *caption_clips])

    # Optional: force vertical 9:16 (comment these two lines if you want original aspect)
    # final = final.resize(height=1920)
    # final = final.crop(x_center=final.w / 2, y_center=final.h / 2, width=1080, height=1920)

    out_path = os.path.join(tmp, "final_story.mp4")
    final.write_videofile(out_path, codec="libx264", audio_codec="aac", threads=4, fps=video.fps or 30, logger=None)

    # Return ready-to-download file
    return send_file(out_path, as_attachment=True, download_name="final_story.mp4", mimetype="video/mp4")


if __name__ == "__main__":
    # Run: ELEVENLABS_API_KEY=xxxx FLASK_ENV=development python app.py
    app.run(host="0.0.0.0", port=5001, debug=True)
