import os
import re
import nltk
import tempfile
import asyncio
import edge_tts
import numpy as np
from moviepy.editor import *
from flask import Flask, request, send_file, jsonify
from werkzeug.utils import secure_filename
from flask import render_template

nltk.download("punkt_tab")

app = Flask(__name__)

# Config
UPLOAD_FOLDER = "uploads"
OUTPUT_FOLDER = "outputs"
ALLOWED_EXTENSIONS = {"txt", "mp4"}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# Helpers
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def split_text_file(input_file, output_file, max_words=4):
    """Split text file into lines of max_words (for subtitles)."""
    with open(input_file, "r", encoding="utf-8") as f:
        text = f.read()

    words = text.split()
    chunks = [" ".join(words[i:i + max_words]) for i in range(0, len(words), max_words)]

    with open(output_file, "w", encoding="utf-8") as f:
        f.write("\n".join(chunks))

async def generate_audio(text_file, audio_file):
    """Generate TTS audio from full text (not split)."""
    with open(text_file, "r", encoding="utf-8") as f:
        text = f.read()

    tts = edge_tts.Communicate(text, voice="en-US-AriaNeural")
    await tts.save(audio_file)

def overlay_text_on_video(video_file, text_file, output_file, y_offset=0):
    """Overlay text lines as subtitles on video, centered (can shift vertically with y_offset)."""
    with open(text_file, "r", encoding="utf-8") as f:
        lines = f.readlines()

    video = VideoFileClip(video_file)
    duration_per_line = video.duration / len(lines)

    clips = []
    for i, line in enumerate(lines):
        txt = TextClip(
            line.strip(),
            fontsize=60,
            color="white",
            size=(video.w - 100, None),  # wraps text if too long
            method="caption"
        )

        # Center horizontally, shift vertically by y_offset
        txt = txt.set_position(("center", "center")).set_duration(duration_per_line).set_start(i * duration_per_line)

        if y_offset != 0:
            txt = txt.set_position(("center", video.h // 2 + y_offset))

        clips.append(txt)

    final = CompositeVideoClip([video, *clips])
    final.write_videofile(output_file, codec="libx264", audio_codec="aac", logger=None)

@app.route("/") 
def index():
     return render_template("index.html")
@app.route("/process", methods=["POST"])
def process():
    try:
        # Upload files
        if "video" not in request.files or "text" not in request.files:
            return jsonify({"error": "Missing files"}), 400

        video_file = request.files["video"]
        text_file = request.files["text"]

        if not (allowed_file(video_file.filename) and allowed_file(text_file.filename)):
            return jsonify({"error": "Invalid file type"}), 400

        video_path = os.path.join(UPLOAD_FOLDER, secure_filename(video_file.filename))
        text_path = os.path.join(UPLOAD_FOLDER, secure_filename(text_file.filename))
        video_file.save(video_path)
        text_file.save(text_path)

        # File paths
        processed_text_path = os.path.join(OUTPUT_FOLDER, "split_text.txt")  # For subtitles
        audio_path = os.path.join(OUTPUT_FOLDER, "audio.mp3")                # From original text
        story_video = os.path.join(OUTPUT_FOLDER, "story.mp4")               # Trimmed + audio
        final_output = os.path.join(OUTPUT_FOLDER, "final_story.mp4")        # With subtitles

        # 1) Split text into short lines (for subtitles only)
        split_text_file(text_path, processed_text_path, max_words=4)

        # 2) Generate TTS from the ORIGINAL file (not split one)
        asyncio.run(generate_audio(text_path, audio_path))

        # 3) Sync: trim video to match audio, then set audio
        with VideoFileClip(video_path) as v, AudioFileClip(audio_path) as a:
            trimmed = v.subclip(0, min(v.duration, a.duration))
            story = trimmed.set_audio(a)

    # Force 9:16 format (TikTok/Shorts)
            target_w, target_h = 1080, 1920  # Full HD vertical
            story = story.resize(height=target_h)  # Match height
            story = story.crop(x_center=story.w/2, y_center=story.h/2, width=target_w, height=target_h)

            story.write_videofile(story_video, codec="libx264", audio_codec="aac", logger=None)

        # 4) Overlay split text as subtitles
        overlay_text_on_video(story_video, processed_text_path, final_output, y_offset=0)


        return send_file(final_output, as_attachment=True)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True)
