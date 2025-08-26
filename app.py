import os
import requests
import json
from pytube import YouTube
from moviepy.editor import *
from flask import Flask, request, send_file, jsonify, render_template
from werkzeug.utils import secure_filename
import base64

app = Flask(__name__)

# Config
UPLOAD_FOLDER = "uploads"
OUTPUT_FOLDER = "outputs"
ALLOWED_EXTENSIONS = {"txt", "mp4"}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

ELEVENLABS_API_KEY = "sk_a931e4d3030370fb262a7aae1dcfd51b4d33dfaa0b7d408b"

# -------------------- Helpers --------------------
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def generate_audio_11labs(text_file, audio_file, align_file, voice_id):
    """Generate audio + alignment from ElevenLabs TTS (JSON with base64 + timing)."""
    with open(text_file, "r", encoding="utf-8") as f:
        text = f.read()

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    data = {
        "text": text,
        "voice_settings": {"stability": 0.7, "similarity_boost": 0.7},
        # model_id / language_code optional; defaults work fine
    }

    r = requests.post(url, headers=headers, json=data)
    if r.status_code != 200:
        raise Exception(f"11Labs error: {r.text}")

    payload = r.json()

    # Save audio (returned as base64 string in JSON)
    audio_b64 = payload.get("audio_base64")
    if not audio_b64:
        raise Exception("11Labs returned no audio_base64")
    audio_bytes = base64.b64decode(audio_b64)
    with open(audio_file, "wb") as f:
        f.write(audio_bytes)

    # Save full payload (contains alignment + normalized_alignment)
    with open(align_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def overlay_text_on_video(video_file, align_file, output_file, y_offset=0):
    """
    Overlay sentence-level subtitles using ElevenLabs alignment timestamps.
    Uses character-level timing from (normalized_)alignment to time sentences.
    """
    with open(align_file, "r", encoding="utf-8") as f:
        payload = json.load(f)

    # Prefer normalized_alignment if available; fall back to alignment
    al = payload.get("normalized_alignment") or payload.get("alignment")
    if not al:
        raise Exception("No alignment data in ElevenLabs response")

    characters = al.get("characters", [])
    starts = al.get("character_start_times_seconds", [])
    ends = al.get("character_end_times_seconds", [])
    if not (characters and starts and ends) or not (len(characters) == len(starts) == len(ends)):
        raise Exception("Alignment arrays are missing or lengths mismatch")

    # Reconstruct the full text from characters returned by ElevenLabs
    full_text = "".join(characters)

    # Split into sentences (simple heuristic; adjust if you like)
    import re
    sentences = re.findall(r".+?(?:[.!?]+(?:\s+|$)|$)", full_text, flags=re.S)

    # Map each sentence to start/end times based on its character span
    spans = []
    cursor = 0
    for s in sentences:
        if not s.strip():
            cursor += len(s)
            continue
        start_idx = cursor
        end_idx = cursor + len(s) - 1

        # collect times for non-whitespace chars in this sentence
        idxs = [i for i in range(start_idx, min(end_idx + 1, len(characters))) if not characters[i].isspace()]
        stimes = [starts[i] for i in idxs if starts[i] is not None]
        etimes = [ends[i] for i in idxs if ends[i] is not None]

        if stimes and etimes:
            t0 = max(0.0, min(stimes))
            t1 = max(etimes)
            if t1 > t0:
                spans.append((s.strip(), t0, t1))
        cursor += len(s)

    video = VideoFileClip(video_file)
    clips = []
    for text, t0, t1 in spans:
        duration = max(0.001, t1 - t0)
        txt = TextClip(
            text,
            fontsize=60,
            color="white",
            size=(video.w - 120, None),   # wrap nicely
            method="caption"
        )
        pos = ("center", "center") if y_offset == 0 else ("center", video.h // 2 + y_offset)
        clips.append(txt.set_position(pos).set_start(t0).set_duration(duration))

    final = CompositeVideoClip([video, *clips])
    final.write_videofile(output_file, codec="libx264", audio_codec="aac", logger=None)

def download_youtube_video(url, output_path):
    """Download YouTube video using pytube."""
    yt = YouTube(url)
    stream = yt.streams.filter(file_extension="mp4", res="720p").first()
    if not stream:
        stream = yt.streams.get_highest_resolution()
    stream.download(filename=output_path)
    return output_path

def get_voices():
    """Fetch available ElevenLabs voices."""
    url = "https://api.elevenlabs.io/v1/voices"
    headers = {"xi-api-key": ELEVENLABS_API_KEY}
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        return []
    return response.json().get("voices", [])

# -------------------- Routes --------------------
@app.route("/")
def index():
    voices = get_voices()
    return render_template("index.html", voices=voices)

@app.route("/process", methods=["POST"])
def process():
    try:
        if "video" not in request.files or "text" not in request.files:
            return jsonify({"error": "Missing files"}), 400

        video_file = request.files["video"]
        text_file = request.files["text"]
        voice_id = request.form.get("voice_id")

        if not (allowed_file(video_file.filename) and allowed_file(text_file.filename)):
            return jsonify({"error": "Invalid file type"}), 400

        video_path = os.path.join(UPLOAD_FOLDER, secure_filename(video_file.filename))
        text_path = os.path.join(UPLOAD_FOLDER, secure_filename(text_file.filename))
        video_file.save(video_path)
        text_file.save(text_path)

        audio_path = os.path.join(OUTPUT_FOLDER, "audio.mp3")
        align_path = os.path.join(OUTPUT_FOLDER, "align.json")
        story_video = os.path.join(OUTPUT_FOLDER, "story.mp4")
        final_output = os.path.join(OUTPUT_FOLDER, "final_story.mp4")

        generate_audio_11labs(text_path, audio_path, align_path, voice_id)

        with VideoFileClip(video_path) as v, AudioFileClip(audio_path) as a:
            trimmed = v.subclip(0, min(v.duration, a.duration))
            story = trimmed.set_audio(a)
            story = story.resize(height=1920).crop(x_center=story.w/2, y_center=story.h/2, width=1080, height=1920)
            story.write_videofile(story_video, codec="libx264", audio_codec="aac", logger=None)

        overlay_text_on_video(story_video, align_path, final_output)

        return send_file(final_output, as_attachment=True)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True)
