import json
import logging
import os
import re
import subprocess
import threading
import uuid
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename
from openai import OpenAI

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('osp-clip-studio')
app = Flask(__name__, template_folder='templates')
ROOT = Path(os.getenv('WORK_DIR', '/tmp/osp-v4'))
ROOT.mkdir(parents=True, exist_ok=True)
MAX_MB = int(os.getenv('MAX_UPLOAD_MB', '250'))
app.config['MAX_CONTENT_LENGTH'] = MAX_MB * 1024 * 1024
FORMATS = {'.mp4', '.mov', '.m4v', '.webm', '.mkv'}
LOCK = threading.RLock()
RENDER_SLOT = threading.BoundedSemaphore(1)
JOBS = {}


def job_dir(job):
    if not re.fullmatch(r'[0-9a-f]{32}', job):
        abort(404)
    return ROOT / job


def write_status(job, **changes):
    with LOCK:
        data = JOBS.get(job)
        if data is None:
            data = {'state': 'queued', 'progress': 0, 'message': 'Queued'}
        data.update(changes)
        JOBS[job] = data
        folder = job_dir(job)
        folder.mkdir(parents=True, exist_ok=True)
        temp = folder / 'status.tmp'
        temp.write_text(json.dumps(data), encoding='utf-8')
        temp.replace(folder / 'status.json')


def read_status(job):
    folder = job_dir(job)
    with LOCK:
        if job in JOBS:
            return dict(JOBS[job])
        path = folder / 'status.json'
        if not path.exists():
            abort(404)
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            abort(404)
        if data.get('state') in ('queued', 'processing'):
            data.update(state='error', progress=0,
                        message='Processing was interrupted by a server restart. Please retry.')
        JOBS[job] = data
        return dict(data)


def run(cmd, timeout=1200, cwd=None, logfile=None):
    log.info('Running %s', ' '.join(map(str, cmd[:8])))
    if logfile:
        with open(logfile, 'w', encoding='utf-8', errors='replace') as err:
            try:
                p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.DEVNULL,
                                   stderr=err, timeout=timeout, check=False)
            except subprocess.TimeoutExpired:
                raise RuntimeError('FFmpeg timed out')
        if p.returncode:
            details = Path(logfile).read_text(errors='replace')[-1800:]
            raise RuntimeError(f'FFmpeg exited {p.returncode}: {details}')
        return ''
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError('Media analysis timed out')
    if p.returncode:
        raise RuntimeError(p.stderr[-1800:])
    return p.stdout


def probe(path):
    data = json.loads(run(['ffprobe', '-v', 'error', '-show_format', '-show_streams',
                           '-of', 'json', str(path)], timeout=90))
    duration = float(data.get('format', {}).get('duration', 0))
    streams = data.get('streams', [])
    if not any(s.get('codec_type') == 'video' for s in streams):
        raise ValueError('No video stream detected')
    return duration, any(s.get('codec_type') == 'audio' for s in streams)


def words_to_srt(segments, path):
    def stamp(t):
        ms = int(max(0, float(t)) * 1000)
        return f'{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}'
    with open(path, 'w', encoding='utf-8') as f:
        for i, seg in enumerate(segments, 1):
            text = re.sub(r'[\r\n]+', ' ', str(seg['text'])).strip()
            if text:
                f.write(f"{i}\n{stamp(seg['start'])} --> {stamp(seg['end'])}\n{text}\n\n")


def transcribe(client, source, folder):
    audio_file = folder / 'voice.mp3'
    run(['ffmpeg', '-nostdin', '-y', '-threads', '1', '-i', str(source), '-vn',
         '-ac', '1', '-ar', '16000', '-b:a', '48k', str(audio_file)],
        timeout=600, logfile=folder / 'transcribe_ffmpeg.log')
    if audio_file.stat().st_size > 24 * 1024 * 1024:
        return [], ''
    with open(audio_file, 'rb') as f:
        result = client.audio.transcriptions.create(
            model=os.getenv('TRANSCRIBE_MODEL', 'whisper-1'), file=f,
            response_format='verbose_json')
    segments = [{'start': float(s.start), 'end': float(s.end), 'text': s.text}
                for s in (result.segments or [])]
    return segments, ' '.join(s['text'] for s in segments)[:5000]


def plan(client, transcript, duration, style):
    fallback = {'title': 'OSP V4 Edit', 'caption': '', 'pace': 1.0, 'look': style}
    if not client:
        return fallback
    try:
        result = client.chat.completions.create(
            model=os.getenv('AI_MODEL', 'gpt-4o-mini'), temperature=0.5,
            response_format={'type': 'json_object'}, messages=[
                {'role': 'system', 'content': 'Return JSON with title, caption, pace (0.9 to 1.15), look (night/cinematic/punchy/clean). Do not invent footage details; only transcript and duration are provided.'},
                {'role': 'user', 'content': json.dumps({'transcript': transcript, 'duration': duration, 'style': style})}
            ])
        value = json.loads(result.choices[0].message.content)
        return {**fallback, **{k: value[k] for k in fallback if k in value}}
    except Exception:
        log.exception('AI planning failed; using default look')
        return fallback


def process(job, source, folder, opts):
    # One render at a time per instance. A separate queue/worker is required for durable jobs.
    with RENDER_SLOT:
        try:
            write_status(job, state='processing', progress=10, message='Analyzing video')
            duration, has_audio = probe(source)
            if not 0.2 <= duration <= 600:
                raise ValueError('Video must be between 0.2 seconds and 10 minutes')
            client = OpenAI() if os.getenv('OPENAI_API_KEY') else None
            segments, transcript = [], ''
            if client and has_audio and opts['captions']:
                write_status(job, progress=20, message='Transcribing speech')
                try:
                    segments, transcript = transcribe(client, source, folder)
                except Exception as exc:
                    log.exception('Transcription failed for %s', job)
                    write_status(job, warning='Captions unavailable: ' + str(exc)[:180])
            write_status(job, progress=40, message='Planning edit')
            edit_plan = plan(client, transcript, duration, opts['style'])
            look = edit_plan.get('look', opts['style'])
            if look not in ('night', 'cinematic', 'punchy', 'clean'):
                look = opts['style']
            # Smaller output + single-thread encoder to fit modest Render instances.
            width, height = (720, 1280) if opts['vertical'] else (1280, 720)
            vf = [f'scale={width}:{height}:force_original_aspect_ratio=decrease:flags=fast_bilinear',
                  f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black', 'setsar=1']
            if look == 'night':
                vf += ['hqdn3d=2:1.5:3:2.5', 'eq=brightness=0.035:contrast=1.12:saturation=1.15:gamma=1.08']
            elif look == 'cinematic':
                vf += ['eq=contrast=1.12:saturation=0.92:gamma=1.03']
            elif look == 'punchy':
                vf += ['eq=contrast=1.17:saturation=1.2']
            else:
                vf += ['eq=contrast=1.05:saturation=1.05']
            if opts['captions'] and segments:
                words_to_srt(segments, folder / 'captions.srt')
                vf.append("subtitles=captions.srt:force_style='FontName=DejaVu Sans,FontSize=19,Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=3,Alignment=2,MarginV=65'")
            vf.append('fps=24')
            cmd = ['ffmpeg', '-nostdin', '-y', '-filter_threads', '1', '-threads', '1', '-i', str(source)]
            if opts['music'] and (folder / 'music.mp3').exists():
                cmd += ['-stream_loop', '-1', '-i', str(folder / 'music.mp3')]
                if has_audio:
                    cmd += ['-filter_complex', '[0:a]volume=1[a0];[1:a]volume=0.13[a1];[a0][a1]amix=inputs=2:duration=first:dropout_transition=2[a]', '-map', '0:v:0', '-map', '[a]']
                else:
                    cmd += ['-map', '0:v:0', '-map', '1:a:0', '-af', 'volume=0.16']
            else:
                cmd += ['-map', '0:v:0']
                if has_audio:
                    cmd += ['-map', '0:a:0', '-af', 'volume=1.0']
            cmd += ['-vf', ','.join(vf), '-c:v', 'libx264', '-threads', '1',
                    '-preset', os.getenv('ENCODE_PRESET', 'ultrafast'), '-crf', '24',
                    '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k',
                    '-movflags', '+faststart', '-t', str(min(duration, 600)),
                    str(folder / 'edited.mp4')]
            write_status(job, progress=55, message='Rendering video (this may take several minutes)')
            run(cmd, cwd=str(folder), timeout=1800, logfile=folder / 'render_ffmpeg.log')
            if not (folder / 'edited.mp4').exists():
                raise RuntimeError('Encoder did not produce an output file')
            write_status(job, state='done', progress=100, message='Ready',
                         title=edit_plan.get('title'), caption=edit_plan.get('caption'),
                         download=f'/download/{job}')
            log.info('Completed job %s', job)
        except Exception as exc:
            log.exception('Job %s failed', job)
            write_status(job, state='error', message=str(exc)[-700:], progress=0)


@app.route('/')
def home():
    return render_template('index.html', ai=bool(os.getenv('OPENAI_API_KEY')), max_mb=MAX_MB)


@app.route('/health')
def health():
    return jsonify(ok=True, version='4.1', ai=bool(os.getenv('OPENAI_API_KEY')))


@app.route('/api/create', methods=['POST'])
def create():
    f = request.files.get('video')
    if not f or Path(secure_filename(f.filename or '')).suffix.lower() not in FORMATS:
        return jsonify(error='Upload MP4, MOV, M4V, WEBM or MKV video'), 400
    job = uuid.uuid4().hex
    folder = job_dir(job)
    folder.mkdir(parents=True)
    ext = Path(secure_filename(f.filename)).suffix.lower()
    source = folder / ('source' + ext)
    f.save(source)
    opts = {'style': request.form.get('style', 'night'),
            'captions': request.form.get('captions', 'true') == 'true',
            'vertical': request.form.get('vertical', 'true') == 'true',
            'music': False}
    if opts['style'] not in ('night', 'cinematic', 'punchy', 'clean'):
        opts['style'] = 'clean'
    music = request.files.get('music')
    if music and music.filename and Path(secure_filename(music.filename)).suffix.lower() == '.mp3':
        music.save(folder / 'music.mp3')
        opts['music'] = True
    write_status(job, state='queued', progress=0, message='Queued')
    threading.Thread(target=process, args=(job, source, folder, opts), daemon=True).start()
    log.info('Accepted job %s', job)
    return jsonify(job=job), 202


@app.route('/api/status/<job>')
def get_status(job):
    return jsonify(read_status(job))


@app.route('/download/<job>')
def download(job):
    if read_status(job).get('state') != 'done':
        abort(404)
    file = job_dir(job) / 'edited.mp4'
    if not file.exists():
        abort(404)
    return send_file(file, as_attachment=True, download_name='OSP_Clip_Studio_V4.mp4', mimetype='video/mp4')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '10000')))
