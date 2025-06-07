from flask import Flask, render_template, request, jsonify, send_file
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound, VideoUnavailable
from youtube_transcript_api.formatters import TextFormatter
import re
import io
import time
import requests
from requests.exceptions import RequestException
import os
import random
from dotenv import load_dotenv
import redis
from ratelimit import limits, sleep_and_retry
import json
from itertools import cycle
from datetime import datetime, timedelta

# Load environment variables
load_dotenv()

app = Flask(__name__)

# Initialize Redis for caching
redis_client = redis.Redis(
    host=os.getenv('REDIS_HOST', 'localhost'),
    port=int(os.getenv('REDIS_PORT', 6379)),
    password=os.getenv('REDIS_PASSWORD', None),
    decode_responses=True
)

# Proxy configuration
PROXY_LIST_URL = "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt"
PROXY_CACHE_KEY = "proxy_list"
PROXY_CACHE_DURATION = 3600  # 1 hour in seconds

def get_proxy_list():
    """Get proxy list from cache or fetch from URL"""
    cached_proxies = redis_client.get(PROXY_CACHE_KEY)
    if cached_proxies:
        return json.loads(cached_proxies)
    
    try:
        response = requests.get(PROXY_LIST_URL, timeout=10)
        if response.status_code == 200:
            proxies = [line.strip() for line in response.text.splitlines() if line.strip()]
            # Cache the proxy list
            redis_client.setex(PROXY_CACHE_KEY, PROXY_CACHE_DURATION, json.dumps(proxies))
            return proxies
    except Exception as e:
        print(f"Error fetching proxy list: {e}")
    
    return []

# Initialize proxy pool
proxy_list = get_proxy_list()
proxy_pool = cycle(proxy_list) if proxy_list else None

# Request headers configuration
DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9,ar-SA;q=0.8,ar;q=0.7',
    'Accept-Encoding': 'gzip, deflate, br, zstd',
    'Cache-Control': 'max-age=0',
    'Sec-Ch-Ua': '"Google Chrome";v="137", "Chromium";v="137", "Not/A)Brand";v="24"',
    'Sec-Ch-Ua-Mobile': '?0',
    'Sec-Ch-Ua-Platform': '"Windows"',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'same-origin',
    'Sec-Fetch-User': '?1',
    'Upgrade-Insecure-Requests': '1'
}

# Rate limiting configuration
CALLS = 50
RATE_LIMIT_PERIOD = 60

def get_session():
    global proxy_list, proxy_pool  # يجب أن يكون في أول الدالة
    """Create a session with configured headers and proxy"""
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    
    if proxy_pool:
        try:
            proxy = next(proxy_pool)
            session.proxies = {
                'http': f'http://{proxy}',
                'https': f'http://{proxy}'
            }
        except StopIteration:
            # If we've gone through all proxies, refresh the list
            proxy_list = get_proxy_list()
            proxy_pool = cycle(proxy_list) if proxy_list else None
    
    return session

@sleep_and_retry
@limits(calls=CALLS, period=RATE_LIMIT_PERIOD)
def get_transcript_with_retry(video_id, language_code=None):
    """Get transcript with rate limiting and retry logic"""
    try:
        session = get_session()
        if language_code:
            transcript = YouTubeTranscriptApi.get_transcript(video_id, languages=[language_code])
        else:
            transcript = YouTubeTranscriptApi.get_transcript(video_id)
        return transcript
    except Exception as e:
        if "429" in str(e):
            # If rate limited, wait and retry with exponential backoff
            wait_time = random.uniform(1, 3) * (2 ** min(3, getattr(get_transcript_with_retry, 'retry_count', 0)))
            time.sleep(wait_time)
            get_transcript_with_retry.retry_count = getattr(get_transcript_with_retry, 'retry_count', 0) + 1
            # Rotate proxy on retry
            if proxy_pool:
                next(proxy_pool)
            return get_transcript_with_retry(video_id, language_code)
        raise e

def get_cached_transcript(video_id, language_code=None):
    """Get transcript from cache or fetch and cache it"""
    cache_key = f"transcript:{video_id}:{language_code or 'default'}"
    cached_transcript = redis_client.get(cache_key)
    
    if cached_transcript:
        return cached_transcript
    
    transcript = get_transcript_with_retry(video_id, language_code)
    formatter = TextFormatter()
    formatted_transcript = formatter.format_transcript(transcript)
    
    # Cache for 24 hours
    redis_client.setex(cache_key, 86400, formatted_transcript)
    return formatted_transcript

def extract_video_id(url):
    """Extract video ID from various YouTube URL formats"""
    patterns = [
        r'(?:v=|\/)([0-9A-Za-z_-]{11}).*',
        r'(?:embed\/)([0-9A-Za-z_-]{11})',
        r'(?:watch\?v=)([0-9A-Za-z_-]{11})'
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
    
    # Add initial delay before first request
    random_delay()
    
    try:
        # Create a custom session with proxy support
        session = get_session()
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        languages = [
            {"code": t.language_code, "name": t.language} for t in transcript_list._manually_created_transcripts.values()
        ]
        # إضافة الترجمات التلقائية
        for t in transcript_list._generated_transcripts.values():
            languages.append({"code": t.language_code, "name": t.language + " (auto-generated)"})
        print("LANGUAGES FOUND:", languages)
        return jsonify({"languages": languages})
    except RequestException as e:
        print("PROXY ERROR:", str(e))
        return jsonify({"error": "فشل الاتصال بالخادم. يرجى التحقق من إعدادات البروكسي."}), 500
    except (TranscriptsDisabled, NoTranscriptFound) as e:
        print("NO TRANSCRIPTS FOUND:", str(e))
        return jsonify({"error": "No transcripts available for this video."}), 404
    except VideoUnavailable as e:
        print("VIDEO UNAVAILABLE:", str(e))
        return jsonify({"error": "Video unavailable or invalid URL."}), 404
    except Exception as e:
        print("UNEXPECTED ERROR:", str(e))
        if "429" in str(e):
            return jsonify({"error": "تم تجاوز حد الطلبات المسموح به. يرجى المحاولة بعد قليل."}), 429
        return jsonify({"error": "Unexpected error: " + str(e)}), 500

def fetch_transcript_with_retry(video_id, lang_code, retries=5, initial_delay=2):
    for attempt in range(retries):
        try:
            # Add random delay before each attempt
            if attempt > 0:
                delay = initial_delay * (2 ** attempt) + random.uniform(1, 3)
                print(f"Waiting {delay:.2f} seconds before retry {attempt + 1}")
                time.sleep(delay)
            
            # Create a custom session with proxy support
            session = get_session()
            transcript = YouTubeTranscriptApi.get_transcript(
                video_id,
                languages=[lang_code]
            )
            text = "\n".join([item["text"] for item in transcript])
            return {"transcript": text}
        except RequestException as e:
            print(f"Proxy error on attempt {attempt + 1}: {str(e)}")
            if attempt < retries - 1:
                continue
            return {"error": "فشل الاتصال بالخادم. يرجى التحقق من إعدادات البروكسي."}
        except Exception as e:
            print(f"Error on attempt {attempt + 1}: {str(e)}")
            if "429" in str(e):
                if attempt < retries - 1:
                    print("Rate limit detected, waiting longer before next attempt...")
                    continue
                return {"error": "تم تجاوز حد الطلبات المسموح به. يرجى المحاولة بعد قليل."}
            if attempt < retries - 1:
                continue
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

@app.route('/get_transcript', methods=['POST'])
def get_transcript():
    try:
        data = request.get_json()
        video_url = data.get('video_url')
        language_code = data.get('language_code')
        
        if not video_url:
            return jsonify({'error': 'Please provide a YouTube video URL'}), 400
        
        video_id = extract_video_id(video_url)
        if not video_id:
            return jsonify({'error': 'Invalid YouTube URL'}), 400
        
        # Try to get from cache first
        try:
            transcript = get_cached_transcript(video_id, language_code)
            return jsonify({'transcript': transcript})
        except TranscriptsDisabled:
            return jsonify({'error': 'Transcripts are disabled for this video'}), 400
        except NoTranscriptFound:
            return jsonify({'error': 'No transcript found for this video'}), 404
        except Exception as e:
            if "429" in str(e):
                return jsonify({'error': 'Rate limit exceeded. Please try again in a few minutes.'}), 429
            return jsonify({'error': str(e)}), 500
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def random_delay():
    time.sleep(random.uniform(1, 3))

if __name__ == "__main__":
    app.run(debug=True) 
