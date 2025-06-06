from flask import Flask, render_template, request, jsonify, send_file
import re
import io
import time
import os
import yt_dlp
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

def get_ydl_opts(proxy=None):
    """Get yt-dlp options with proxy support"""
    opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
        'skip_download': True,
        'ignoreerrors': True,  # تجاهل الأخطاء غير الحرجة
        'no_check_certificate': True,  # تجاهل مشاكل الشهادات
        'geo_bypass': True,  # تجاوز القيود الجغرافية
        'geo_verification_proxy': proxy if proxy else None,
        'socket_timeout': 30,  # زيادة وقت الانتظار
        'retries': 10,  # زيادة عدد المحاولات
    }
    if proxy:
        opts['proxy'] = proxy
    return opts

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
        proxy = PROXY_CONFIG['http'] if PROXY_CONFIG['http'] else None
        
        with yt_dlp.YoutubeDL(get_ydl_opts(proxy)) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
                if not info:
                    return jsonify({"error": "لم يتم العثور على الفيديو أو غير متاح."}), 404
                
                captions = info.get('subtitles', {})
                languages = []
                
                for lang_code, lang_data in captions.items():
                    if lang_data:
                        format_data = next(iter(lang_data.values()))
                        languages.append({
                            "code": lang_code,
                            "name": format_data.get('name', lang_code)
                        })
                
                if not languages:
                    return jsonify({"error": "لا توجد ترجمات متاحة لهذا الفيديو."}), 404
                
                print("LANGUAGES FOUND:", languages)
                return jsonify({"languages": languages})
                
            except yt_dlp.utils.DownloadError as e:
                error_msg = str(e)
                if "This content isn't available" in error_msg:
                    return jsonify({"error": "الفيديو غير متاح أو محذوف."}), 404
                elif "Video unavailable" in error_msg:
                    return jsonify({"error": "الفيديو غير متاح في منطقتك."}), 404
                else:
                    return jsonify({"error": f"حدث خطأ أثناء جلب معلومات الفيديو: {error_msg}"}), 500
            
    except Exception as e:
        print("ERROR:", str(e))
        return jsonify({"error": "حدث خطأ أثناء جلب الترجمات المتاحة."}), 500

def fetch_transcript_with_retry(url, lang_code, retries=3, initial_delay=2):
    for attempt in range(retries):
        try:
            if attempt > 0:
                delay = initial_delay * (2 ** attempt)
                print(f"Waiting {delay} seconds before retry {attempt + 1}")
                time.sleep(delay)
            
            proxy = PROXY_CONFIG['http'] if PROXY_CONFIG['http'] else None
            
            opts = get_ydl_opts(proxy)
            opts.update({
                'writesubtitles': True,
                'writeautomaticsub': True,
                'subtitleslangs': [lang_code],
                'skip_download': True,
            })
            
            with yt_dlp.YoutubeDL(opts) as ydl:
                try:
                    info = ydl.extract_info(url, download=False)
                    if not info:
                        return {"error": "لم يتم العثور على الفيديو أو غير متاح."}
                    
                    captions = info.get('subtitles', {}).get(lang_code, {})
                    
                    if not captions:
                        return {"error": "لا توجد ترجمة متاحة بهذه اللغة."}
                    
                    format_data = next(iter(captions.values()))
                    transcript_url = format_data.get('url')
                    
                    if not transcript_url:
                        return {"error": "لا يمكن الوصول إلى النص."}
                    
                    import requests
                    response = requests.get(
                        transcript_url,
                        proxies={'http': proxy, 'https': proxy} if proxy else None,
                        timeout=30
                    )
                    response.raise_for_status()
                    transcript_data = response.json()
                    
                    text = "\n".join([item['text'] for item in transcript_data['events'] if 'text' in item])
                    return {"transcript": text}
                    
                except yt_dlp.utils.DownloadError as e:
                    error_msg = str(e)
                    if "This content isn't available" in error_msg:
                        return {"error": "الفيديو غير متاح أو محذوف."}
                    elif "Video unavailable" in error_msg:
                        return {"error": "الفيديو غير متاح في منطقتك."}
                    else:
                        print(f"Download error: {error_msg}")
                        if attempt == retries - 1:
                            return {"error": "فشل في جلب النص. يرجى المحاولة مرة أخرى."}
                
        except Exception as e:
            print(f"Error on attempt {attempt + 1}: {str(e)}")
            if attempt == retries - 1:
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
