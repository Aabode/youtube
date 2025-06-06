from flask import Flask, render_template, request, jsonify, send_file
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound, VideoUnavailable
import re
import io
import time

app = Flask(__name__)

def extract_video_id(url):
    # يدعم جميع أنواع روابط يوتيوب (عادي، شورتس، مع معلمات)
    patterns = [
        r"(?:v=|\/)([0-9A-Za-z_-]{11})(?:[&?\s]|$)",  # v=ID أو /ID
        r"youtu\.be/([0-9A-Za-z_-]{11})",              # youtu.be/ID
        r"shorts/([0-9A-Za-z_-]{11})"                   # shorts/ID
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/get_transcripts", methods=["POST"])
def get_transcripts():
    url = request.json.get("url")
    print("URL RECEIVED:", url)
    video_id = extract_video_id(url)
    print("VIDEO ID:", video_id)
    if not video_id:
        print("ERROR: Invalid video ID")
        return jsonify({"error": "Invalid URL."}), 400
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        languages = [
            {"code": t.language_code, "name": t.language} for t in transcript_list._manually_created_transcripts.values()
        ]
        # إضافة الترجمات التلقائية
        for t in transcript_list._generated_transcripts.values():
            languages.append({"code": t.language_code, "name": t.language + " (auto-generated)"})
        print("LANGUAGES FOUND:", languages)
        return jsonify({"languages": languages})
    except (TranscriptsDisabled, NoTranscriptFound) as e:
        print("NO TRANSCRIPTS FOUND:", str(e))
        return jsonify({"error": "No transcripts available for this video."}), 404
    except VideoUnavailable as e:
        print("VIDEO UNAVAILABLE:", str(e))
        return jsonify({"error": "Video unavailable or invalid URL."}), 404
    except Exception as e:
        print("UNEXPECTED ERROR:", str(e))
        return jsonify({"error": "Unexpected error: " + str(e)}), 500

def fetch_transcript_with_retry(video_id, lang_code, retries=3, delay=1):
    for attempt in range(retries):
        try:
            transcript = YouTubeTranscriptApi.get_transcript(video_id, languages=[lang_code])
            text = "\n".join([item["text"] for item in transcript])
            return {"transcript": text}
        except Exception:
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                return {"error": "لا توجد ترجمة متاحة بهذه اللغة."}

@app.route("/fetch_transcript", methods=["POST"])
def fetch_transcript():
    video_id = request.json.get("video_id")
    lang_code = request.json.get("lang_code")
    result = fetch_transcript_with_retry(video_id, lang_code)
    if "transcript" in result:
        return jsonify({"transcript": result["transcript"]})
    else:
        return jsonify({"error": result["error"]}), 404

@app.route("/download_transcript", methods=["POST"])
def download_transcript():
    text = request.json.get("text", "")
    fmt = request.json.get("format", "txt")
    filename = f"transcript.{fmt}"
    buffer = io.BytesIO()
    if fmt == "srt":
        # تحويل النص إلى صيغة SRT
        transcript = request.json.get("raw", [])
        srt = ""
        for i, item in enumerate(transcript, 1):
            start = item.get("start", 0)
            duration = item.get("duration", 0)
            end = start + duration
            def srt_time(t):
                h, m = divmod(int(t // 60), 60)
                s = int(t % 60)
                ms = int((t - int(t)) * 1000)
                return f"{h:02}:{m:02}:{s:02},{ms:03}"
            srt += f"{i}\n{srt_time(start)} --> {srt_time(end)}\n{item['text']}\n\n"
        buffer.write(srt.encode("utf-8"))
    elif fmt == "vtt":
        # تحويل النص إلى صيغة VTT
        transcript = request.json.get("raw", [])
        vtt = "WEBVTT\n\n"
        for item in transcript:
            start = item.get("start", 0)
            duration = item.get("duration", 0)
            end = start + duration
            def vtt_time(t):
                h, m = divmod(int(t // 60), 60)
                s = int(t % 60)
                ms = int((t - int(t)) * 1000)
                return f"{h:02}:{m:02}:{s:02}.{ms:03}"
            vtt += f"{vtt_time(start)} --> {vtt_time(end)}\n{item['text']}\n\n"
        buffer.write(vtt.encode("utf-8"))
    else:
        buffer.write(text.encode("utf-8"))
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name=filename, mimetype="text/plain")

if __name__ == "__main__":
    app.run(debug=True) 
