from flask import Flask, render_template, request, jsonify, send_file
import re
import io
import time
import os
import requests
from pytube import YouTube
import json

app = Flask(__name__)

# Proxy configuration
PROXY_CONFIG = {
    'http': os.getenv('HTTP_PROXY', ''),
    'https': os.getenv('HTTPS_PROXY', '')
}

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

def get_proxy_dict():
    """Get proxy dictionary for requests"""
    if PROXY_CONFIG['http']:
        return {
            'http': PROXY_CONFIG['http'],
            'https': PROXY_CONFIG['https'] or PROXY_CONFIG['http']
        }
    return None

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/get_transcripts", methods=["POST"])
def get_transcripts():
    url = request.json.get("url")
    print("URL RECEIVED:", url)
    
    if not url:
        return jsonify({"error": "Invalid URL."}), 400

    try:
        # Configure YouTube with proxy if available
        proxy = get_proxy_dict()
        yt = YouTube(
            url,
            use_oauth=False,
            allow_oauth_cache=True,
            proxies=proxy
        )
        
        # Get available captions
        captions = yt.captions
        languages = []
        
        for lang_code, caption in captions.items():
            languages.append({
                "code": lang_code,
                "name": caption.name
            })
        
        if not languages:
            return jsonify({"error": "لا توجد ترجمات متاحة لهذا الفيديو."}), 404
        
        print("LANGUAGES FOUND:", languages)
        return jsonify({"languages": languages})
            
    except Exception as e:
        print("ERROR:", str(e))
        error_msg = str(e)
        if "Video unavailable" in error_msg:
            return jsonify({"error": "الفيديو غير متاح أو محذوف."}), 404
        elif "Private video" in error_msg:
            return jsonify({"error": "هذا الفيديو خاص."}), 403
        else:
            return jsonify({"error": "حدث خطأ أثناء جلب الترجمات المتاحة."}), 500

def fetch_transcript_with_retry(url, lang_code, retries=3, initial_delay=2):
    for attempt in range(retries):
        try:
            if attempt > 0:
                delay = initial_delay * (2 ** attempt)
                print(f"Waiting {delay} seconds before retry {attempt + 1}")
                time.sleep(delay)
            
            proxy = get_proxy_dict()
            yt = YouTube(
                url,
                use_oauth=False,
                allow_oauth_cache=True,
                proxies=proxy
            )
            
            caption = yt.captions.get(lang_code)
            if not caption:
                return {"error": "لا توجد ترجمة متاحة بهذه اللغة."}
            
            # Get the transcript
            transcript = caption.generate_srt_captions()
            
            # Convert SRT to plain text
            lines = transcript.split('\n')
            text_lines = []
            for line in lines:
                if not line.strip().isdigit() and not '-->' in line and line.strip():
                    text_lines.append(line.strip())
            
            text = '\n'.join(text_lines)
            return {"transcript": text}
                
        except Exception as e:
            print(f"Error on attempt {attempt + 1}: {str(e)}")
            error_msg = str(e)
            if "Video unavailable" in error_msg:
                return {"error": "الفيديو غير متاح أو محذوف."}
            elif "Private video" in error_msg:
                return {"error": "هذا الفيديو خاص."}
            elif attempt == retries - 1:
                return {"error": "فشل في جلب النص. يرجى المحاولة مرة أخرى."}
    
    return {"error": "فشل في جلب النص بعد عدة محاولات."}

@app.route("/fetch_transcript", methods=["POST"])
def fetch_transcript():
    url = request.json.get("url")
    lang_code = request.json.get("lang_code")
    result = fetch_transcript_with_retry(url, lang_code)
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
