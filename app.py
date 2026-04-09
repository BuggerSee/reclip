import os
import uuid
import glob
import json
import time
import subprocess
import threading
import logging
from flask import Flask, request, jsonify, send_file, render_template

app = Flask(__name__)
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

MAX_DOWNLOAD_AGE_HOURS = 1
MAX_DOWNLOAD_DIR_SIZE_MB = 500

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

jobs = {}


def cleanup_old_downloads():
    now = time.time()
    cutoff = now - (MAX_DOWNLOAD_AGE_HOURS * 3600)
    removed = 0
    for f in glob.glob(os.path.join(DOWNLOAD_DIR, "*")):
        try:
            if os.path.isfile(f) and os.path.getmtime(f) < cutoff:
                os.remove(f)
                removed += 1
        except OSError:
            pass
    if removed:
        logger.info(f"Cleanup: removed {removed} old download(s)")


def enforce_dir_size_limit():
    files = []
    total = 0
    for f in glob.glob(os.path.join(DOWNLOAD_DIR, "*")):
        if os.path.isfile(f):
            size = os.path.getsize(f)
            total += size
            files.append((os.path.getmtime(f), f, size))

    max_bytes = MAX_DOWNLOAD_DIR_SIZE_MB * 1024 * 1024
    if total <= max_bytes:
        return

    files.sort()
    removed = 0
    for _, f, size in files:
        try:
            os.remove(f)
            total -= size
            removed += 1
            if total <= max_bytes:
                break
        except OSError:
            pass
    if removed:
        logger.info(f"Cleanup: removed {removed} file(s) to enforce size limit")


def run_download(job_id, url, format_choice, format_id):
    job = jobs[job_id]
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    cmd = ["yt-dlp", "--no-playlist", "-o", out_template]

    if format_choice == "audio":
        cmd += ["-x", "--audio-format", "mp3"]
    elif format_id:
        cmd += ["-f", f"{format_id}+bestaudio/best", "-S", "vcodec:h264",
                "--merge-output-format", "mp4"]
    else:
        cmd += ["-S", "vcodec:h264", "--merge-output-format", "mp4"]

    cmd.append(url)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            job["status"] = "error"
            job["error"] = result.stderr.strip().split("\n")[-1]
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            job["status"] = "error"
            job["error"] = "Download completed but no file was found"
            return

        if format_choice == "audio":
            target = [f for f in files if f.endswith(".mp3")]
            chosen = target[0] if target else files[0]
        else:
            target = [f for f in files if f.endswith(".mp4")]
            chosen = target[0] if target else files[0]

        # Re-encode to H.264 if source is VP9/AV1 (e.g. Instagram only serves VP9)
        if format_choice != "audio" and chosen.endswith(".mp4"):
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name", "-of", "csv=p=0", chosen],
                capture_output=True, text=True, timeout=30
            )
            codec = probe.stdout.strip()
            if codec and codec != "h264":
                h264_path = chosen.replace(".mp4", "_h264.mp4")
                hw_cmd = [
                    "ffmpeg", "-y", "-hwaccel", "vaapi",
                    "-hwaccel_output_format", "vaapi",
                    "-hwaccel_device", "/dev/dri/renderD128",
                    "-i", chosen,
                    "-c:v", "h264_vaapi", "-qp", "23",
                    "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart", h264_path
                ]
                sw_cmd = [
                    "ffmpeg", "-y", "-i", chosen,
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart", h264_path
                ]
                reencode = subprocess.run(hw_cmd, capture_output=True, text=True, timeout=300)
                if reencode.returncode != 0:
                    reencode = subprocess.run(sw_cmd, capture_output=True, text=True, timeout=600)
                if reencode.returncode == 0:
                    os.remove(chosen)
                    os.rename(h264_path, chosen)

        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass

        job["status"] = "done"
        job["file"] = chosen
        ext = os.path.splitext(chosen)[1]
        title = job.get("title", "").strip()
        # Sanitize title for filename
        if title:
            safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:20].strip()
            job["filename"] = f"{safe_title}{ext}" if safe_title else os.path.basename(chosen)
        else:
            job["filename"] = os.path.basename(chosen)
    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["error"] = "Download timed out (5 min limit)"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    cmd = ["yt-dlp", "--no-playlist", "-j", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = json.loads(result.stdout)

        # Build quality options — keep best H.264 format per resolution, fall back to any codec
        best_by_height = {}
        for f in info.get("formats", []):
            height = f.get("height")
            if height and f.get("vcodec", "none") != "none":
                vcodec = f.get("vcodec", "")
                is_h264 = vcodec.startswith("avc")
                tbr = f.get("tbr") or 0
                existing = best_by_height.get(height)
                if not existing:
                    best_by_height[height] = f
                else:
                    existing_is_h264 = existing.get("vcodec", "").startswith("avc")
                    if is_h264 and not existing_is_h264:
                        best_by_height[height] = f
                    elif is_h264 == existing_is_h264 and tbr > (existing.get("tbr") or 0):
                        best_by_height[height] = f

        formats = []
        for height, f in best_by_height.items():
            formats.append({
                "id": f["format_id"],
                "label": f"{height}p",
                "height": height,
            })
        formats.sort(key=lambda x: x["height"], reverse=True)

        return jsonify({
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "uploader": info.get("uploader", ""),
            "formats": formats,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching video info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    cleanup_old_downloads()
    enforce_dir_size_limit()

    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {"status": "downloading", "url": url, "title": title}

    thread = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "error": job.get("error"),
        "filename": job.get("filename"),
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    return send_file(job["file"], as_attachment=True, download_name=job["filename"])


if __name__ == "__main__":
    cleanup_old_downloads()
    enforce_dir_size_limit()

    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
