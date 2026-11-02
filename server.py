import os, uuid, shutil, subprocess, json, threading, traceback, re
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template, abort
from werkzeug.utils import secure_filename
from openai import OpenAI

app=Flask(__name__,template_folder='templates')
ROOT=Path(os.getenv('WORK_DIR','/tmp/osp-v4')); ROOT.mkdir(parents=True,exist_ok=True)
JOBS={}; LOCK=threading.Lock()
MAX_MB=int(os.getenv('MAX_UPLOAD_MB','250'))
app.config['MAX_CONTENT_LENGTH']=MAX_MB*1024*1024
FORMATS={'.mp4','.mov','.m4v','.webm','.mkv'}

def run(cmd):
 p=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=1200)
 if p.returncode: raise RuntimeError(p.stderr[-2500:])
 return p.stdout

def probe(path):
 p=json.loads(run(['ffprobe','-v','error','-show_format','-show_streams','-of','json',str(path)]))
 return float(p['format'].get('duration',0)),any(s['codec_type']=='audio' for s in p['streams'])

def status(job,**kw):
 with LOCK: JOBS[job].update(kw)

def words_to_srt(segments,path):
 def stamp(t):
  ms=int(max(0,t)*1000); return f'{ms//3600000:02}:{ms//60000%60:02}:{ms//1000%60:02},{ms%1000:03}'
 with open(path,'w') as f:
  for i,s in enumerate(segments,1):
   text=re.sub(r'[\r\n]+',' ',str(s['text'])).strip()
   if text: f.write(f"{i}\n{stamp(s['start'])} --> {stamp(s['end'])}\n{text}\n\n")

def transcribe(client,source,folder):
 wav=folder/'voice.mp3'
 run(['ffmpeg','-y','-i',str(source),'-vn','-ac','1','-ar','16000','-b:a','48k',str(wav)])
 if wav.stat().st_size>24*1024*1024: return [],''
 with open(wav,'rb') as f: result=client.audio.transcriptions.create(model=os.getenv('TRANSCRIBE_MODEL','whisper-1'),file=f,response_format='verbose_json')
 segments=[{'start':float(s.start),'end':float(s.end),'text':s.text} for s in (result.segments or [])]
 return segments,' '.join(s['text'] for s in segments)[:5000]

def plan(client,transcript,duration,style):
 fallback={'title':'OSP V4 Edit','caption':'', 'pace':1.0,'look':style}
 if not client: return fallback
 try:
  res=client.chat.completions.create(model=os.getenv('AI_MODEL','gpt-4o-mini'),temperature=0.5,response_format={'type':'json_object'},messages=[{'role':'system','content':'You are an expert short-form video editor. Return JSON with title (short), caption (one short energetic sentence), pace (number 0.9 to 1.15), look (night/cinematic/punchy/clean). No invented claims about footage. The input contains only transcript and duration, not video frames.'},{'role':'user','content':json.dumps({'transcript':transcript,'duration':duration,'style':style})}])
  v=json.loads(res.choices[0].message.content); return {**fallback,**{k:v[k] for k in fallback if k in v}}
 except Exception: return fallback

def process(job,source,folder,opts):
 try:
  status(job,state='processing',progress=10,message='Analyzing video')
  duration,audio=probe(source)
  if duration<0.2 or duration>600: raise ValueError('Video must be between 0.2 seconds and 10 minutes')
  client=OpenAI() if os.getenv('OPENAI_API_KEY') else None
  segments=[]; transcript=''
  if client and audio and opts['captions']:
   status(job,progress=20,message='Transcribing speech with AI')
   try: segments,transcript=transcribe(client,source,folder)
   except Exception as e: status(job,warning='Captions unavailable: '+str(e)[:180])
  status(job,progress=40,message='Planning edit')
  p=plan(client,transcript,duration,opts['style'])
  look=p.get('look',opts['style']); look=look if look in ('night','cinematic','punchy','clean') else opts['style']
  # Enhance with FFmpeg filters. No fake frame-aware scene detection.
  vf=['scale=1080:1920:force_original_aspect_ratio=decrease','pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black','setsar=1'] if opts['vertical'] else ['scale=1280:720:force_original_aspect_ratio=decrease','pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black','setsar=1']
  if look=='night': vf+=['hqdn3d=2:1.5:3:2.5','eq=brightness=0.035:contrast=1.12:saturation=1.15:gamma=1.08','unsharp=3:3:0.5']
  elif look=='cinematic': vf+=['eq=contrast=1.12:saturation=0.92:gamma=1.03','unsharp=3:3:0.4']
  elif look=='punchy': vf+=['eq=contrast=1.17:saturation=1.2','unsharp=3:3:0.6']
  else: vf+=['eq=contrast=1.05:saturation=1.05']
  if opts['captions'] and segments:
   srt=folder/'captions.srt'; words_to_srt(segments,srt)
   # Relative filename; FFmpeg cwd is the job directory.
   vf.append("subtitles=captions.srt:force_style='FontName=DejaVu Sans,FontSize=19,Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=3,Alignment=2,MarginV=95'")
  vf.append('fps=30')
  status(job,progress=55,message='Rendering enhanced video')
  out=folder/'edited.mp4'
  cmd=['ffmpeg','-y','-i',str(source)]
  if opts['music'] and (folder/'music.mp3').exists():
   cmd+=['-stream_loop','-1','-i',str(folder/'music.mp3')]
   if audio: cmd+=['-filter_complex','[0:a]loudnorm=I=-16:TP=-1.5:LRA=11[a0];[1:a]volume=0.13[a1];[a0][a1]amix=inputs=2:duration=first:dropout_transition=2[a]','-map','0:v:0','-map','[a]']
   else: cmd+=['-map','0:v:0','-map','1:a:0','-af','volume=0.16']
  else:
   cmd+=['-map','0:v:0']
   if audio: cmd+=['-map','0:a:0','-af','loudnorm=I=-16:TP=-1.5:LRA=11']
  cmd+=['-vf',','.join(vf),'-c:v','libx264','-preset',os.getenv('ENCODE_PRESET','veryfast'),'-crf','21','-pix_fmt','yuv420p','-c:a','aac','-b:a','160k','-movflags','+faststart','-t',str(min(duration,600)),'-shortest',str(out)]
  run_cwd=subprocess.run(cmd,cwd=str(folder),capture_output=True,text=True,timeout=1800)
  if run_cwd.returncode: raise RuntimeError(run_cwd.stderr[-2200:])
  status(job,state='done',progress=100,message='Ready',title=p.get('title'),caption=p.get('caption'),download=f'/download/{job}')
 except Exception as e:
  status(job,state='error',message=str(e)[:700],progress=0)

@app.route('/')
def home(): return render_template('index.html',ai=bool(os.getenv('OPENAI_API_KEY')),max_mb=MAX_MB)

@app.route('/health')
def health(): return jsonify(ok=True,version='4.0',ai=bool(os.getenv('OPENAI_API_KEY')))

@app.route('/api/create',methods=['POST'])
def create():
 f=request.files.get('video')
 if not f or Path(secure_filename(f.filename)).suffix.lower() not in FORMATS: return jsonify(error='Upload MP4, MOV, M4V, WEBM or MKV video'),400
 job=uuid.uuid4().hex; folder=ROOT/job; folder.mkdir()
 ext=Path(secure_filename(f.filename)).suffix.lower(); src=folder/('source'+ext); f.save(src)
 opts={'style':request.form.get('style','night'),'captions':request.form.get('captions','true')=='true','vertical':request.form.get('vertical','true')=='true','music':False}
 music=request.files.get('music')
 if music and music.filename and Path(secure_filename(music.filename)).suffix.lower() in ('.mp3',): music.save(folder/'music.mp3'); opts['music']=True
 JOBS[job]={'state':'queued','progress':0,'message':'Queued'}
 threading.Thread(target=process,args=(job,src,folder,opts),daemon=True).start()
 return jsonify(job=job),202

@app.route('/api/status/<job>')
def get_status(job):
 if job not in JOBS: abort(404)
 return jsonify(JOBS[job])

@app.route('/download/<job>')
def download(job):
 if job not in JOBS or JOBS[job]['state']!='done': abort(404)
 return send_file(ROOT/job/'edited.mp4',as_attachment=True,download_name='OSP_Clip_Studio_V4.mp4',mimetype='video/mp4')

if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.getenv('PORT','10000')))
