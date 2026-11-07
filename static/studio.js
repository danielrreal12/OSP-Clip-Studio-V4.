const form = document.querySelector("#form");
const videoInput = document.querySelector("#video");
const submit = document.querySelector("#submit");
const jobCard = document.querySelector("#job");
const banner = document.querySelector("#config-banner");
const formAlert = document.querySelector("#form-alert");
const estimate = document.querySelector("#estimate");
const filename = document.querySelector("#filename");
const drop = document.querySelector("#drop");
let config = null;
let timer = null;
let currentJob = null;
let polling = false;
let outputKey = "";
let previewUrl = null;

const STAGE_LABELS = {
  probe: "Read the file",
  quality: "Exposure and black frames",
  scenes: "Shot boundaries",
  motion: "Camera movement",
  audio: "Sound",
  vision: "Watch the frames",
  speech: "Speech",
  director: "Edit decisions",
  render: "Render",
  qa: "Quality review",
};

boot();

function boot() {
  fetch("/api/config")
    .then((response) => response.json())
    .then((data) => {
      config = data;
      document.querySelector("#version").textContent = "V" + String(data.version || "6").replace(/^(\d).*/, "$1");
      document.querySelector("#upload-limit").textContent = "Up to " + data.max_upload_mb + " MB · " + Math.round(data.max_duration_sec / 60) + " minutes";
      const perMinute = data.estimate_per_minute || {};
      estimate.textContent = perMinute.estimated_usd
        ? `About $${Number(perMinute.estimated_usd).toFixed(3)} of model cost per minute of source. The invoice can differ; this uses published rate estimates.`
        : "";
      if (!data.ai_configured) {
        const message = data.message || "The AI provider is not configured.";
        banner.hidden = false;
        banner.className = "banner bad";
        banner.textContent = message;
        showFormAlert(message);
      } else {
        banner.hidden = false;
        banner.className = "banner info";
        banner.textContent = `Vision ${data.vision_model}, director ${data.director_model}, speech ${data.transcribe_model}.`;
        showFormAlert("");
      }
    })
    .catch(() => {
      banner.hidden = false;
      banner.className = "banner bad";
      banner.textContent = "The studio could not be reached.";
      showFormAlert("The studio could not be reached. Wait a few seconds and tap Create the edit again.");
    });

  const saved = sessionStorage.getItem("osp-job");
  if (saved && /^[0-9a-f]{32}$/.test(saved)) {
    currentJob = saved;
    jobCard.hidden = false;
    poll();
    timer = setInterval(poll, 1600);
  }
}

["dragover", "drop"].forEach((name) => {
  window.addEventListener(name, (event) => event.preventDefault());
});
drop.addEventListener("dragover", () => drop.classList.add("hot"));
drop.addEventListener("dragleave", () => drop.classList.remove("hot"));
drop.addEventListener("drop", (event) => {
  drop.classList.remove("hot");
  const file = event.dataTransfer.files && event.dataTransfer.files[0];
  if (!file) return;
  const transfer = new DataTransfer();
  transfer.items.add(file);
  videoInput.files = transfer.files;
  updateFile();
});
videoInput.addEventListener("change", () => {
  updateFile();
});

function updateFile() {
  const file = videoInput.files[0];
  filename.textContent = file ? file.name : "Drop a video or choose one";
  const preview = document.querySelector("#source-preview");
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = file ? URL.createObjectURL(file) : null;
  preview.hidden = !file;
  if (file) preview.src = previewUrl;
  else preview.removeAttribute("src");
  document.querySelector("#file-meta").textContent = file
    ? (file.size / 1024 / 1024).toFixed(1) + " MB · Ready to upload"
    : "MP4, MOV, WEBM · phone or Chromebook";
}

function uploadRequest(url, method, body, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(method, url);
    xhr.responseType = "json";
    xhr.timeout = 300000;
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && onProgress) onProgress(event.loaded, event.total);
    };
    xhr.onload = () => {
      const payload = xhr.response || {};
      if (xhr.status >= 200 && xhr.status < 300) resolve(payload);
      else reject(new Error(payload.message || "Upload failed. Please try again."));
    };
    xhr.onerror = () => reject(new Error("The upload connection dropped. Tap Create the edit to resume."));
    xhr.ontimeout = () => reject(new Error("Upload timed out. Tap Create the edit to resume."));
    xhr.send(body);
  });
}

async function uploadVideo(body) {
  const file = videoInput.files[0];
  const showProgress = (loaded, total) => {
    const percent = Math.round(loaded / total * 100);
    setMessage(percent < 100 ? "Uploading " + percent + "%" : "Checking your video…", percent);
  };
  if (file.size <= 32 * 1024 * 1024) {
    return uploadRequest("/api/jobs", "POST", body, showProgress);
  }
  const fingerprint = JSON.stringify([file.name, file.size, file.lastModified]);
  let session = null;
  try { session = JSON.parse(sessionStorage.getItem("osp-upload")); } catch {}
  if (session && session.fingerprint === fingerprint) {
    const response = await fetch("/api/uploads/" + session.id);
    if (response.ok) session.offset = (await response.json()).offset;
    else session = null;
  } else session = null;
  if (!session) {
    const response = await fetch("/api/uploads", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: file.name, size: file.size }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || "Could not start the upload.");
    session = { id: data.upload_id, offset: data.offset, fingerprint };
    sessionStorage.setItem("osp-upload", JSON.stringify(session));
  }
  let failures = 0;
  while (session.offset < file.size) {
    const offset = session.offset;
    const part = file.slice(offset, offset + 16 * 1024 * 1024);
    try {
      const data = await uploadRequest(
        "/api/uploads/" + session.id + "?offset=" + offset, "PUT", part,
        (loaded) => showProgress(offset + loaded, file.size),
      );
      session.offset = data.offset;
      failures = 0;
    } catch (error) {
      if (++failures >= 3) throw error;
      const response = await fetch("/api/uploads/" + session.id);
      if (!response.ok) throw error;
      session.offset = (await response.json()).offset;
    }
    sessionStorage.setItem("osp-upload", JSON.stringify(session));
  }
  body.delete("video");
  body.set("upload_id", session.id);
  setMessage("Checking your video…", 100);
  const result = await uploadRequest("/api/jobs", "POST", body);
  sessionStorage.removeItem("osp-upload");
  return result;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = videoInput.files[0];
  if (!config) {
    showFormAlert("Still checking the server. Wait a second and tap Create the edit again.");
    return;
  }
  if (!config.ai_configured) {
    const message = config.message || "OPENAI_API_KEY is not configured on the server. Autonomous editing cannot run until that key is set.";
    showFormAlert(message);
    formAlert.scrollIntoView({ block: "center" });
    return;
  }
  if (!file) {
    showFormAlert("Choose a video first. Tap the dashed box, pick a file, and wait until its name appears here.");
    formAlert.scrollIntoView({ block: "center" });
    return;
  }
  const maxMb = (config && config.max_upload_mb) || 800;
  if (file.size > maxMb * 1024 * 1024) {
    showFormAlert(`That file is over the ${maxMb} MB limit.`);
    return;
  }
  submit.disabled = true;
  submit.textContent = "Uploading…";
  jobCard.hidden = false;
  showFormAlert("");
  showAlert("");
  document.querySelector("#plan").hidden = true;
  document.querySelector("#compare").hidden = true;
  document.querySelector("#actions").hidden = true;
  outputKey = "";
  setMessage("Uploading…", 2);
  jobCard.scrollIntoView({ block: "start" });
  const body = new FormData(form);
  try {
    const payload = await uploadVideo(body);
    currentJob = payload.job;
    sessionStorage.setItem("osp-job", currentJob);
    submit.textContent = "Working…";
    if (timer) clearInterval(timer);
    poll();
    timer = setInterval(poll, 1600);
  } catch (error) {
    showFormAlert(error.message);
    showAlert(error.message);
    submit.disabled = false;
    submit.textContent = "Create the edit";
  }
});

async function poll() {
  if (!currentJob || polling) return;
  polling = true;
  const job = currentJob;
  try {
    const response = await fetch("/api/jobs/" + currentJob);
    const status = await response.json();
    if (!response.ok) throw new Error(status.message || "Status failed");
    if (job !== currentJob) return;
    renderStatus(status);
    if (status.state === "done" || status.state === "error") {
      clearInterval(timer);
      timer = null;
      submit.disabled = false;
      submit.textContent = "Create the edit";
      if (status.plan_ready || status.state === "done") loadPlan().catch(() => showAlert("Could not load the timeline. Refresh to try again."));
    }
  } catch (error) {
    showAlert(error.message || "The connection dropped. This page will keep checking.");
  } finally {
    polling = false;
  }
}

function renderStatus(status) {
  document.querySelector("#job-title").textContent = status.state === "done" ? "Ready" : status.state === "error" ? "Needs attention" : "Working";
  setMessage(status.message || status.state, status.progress || 0);
  const list = document.querySelector("#stages");
  list.replaceChildren();
  (status.stages || []).forEach((stage) => {
    const item = document.createElement("li");
    item.className = stage.state || "";
    item.textContent = STAGE_LABELS[stage.id] || stage.id;
    const mark = document.createElement("span");
    mark.textContent = stage.state === "done" ? "Done" : stage.state === "active" ? "Now" : "";
    item.append(mark);
    list.append(item);
  });
  const warnings = document.querySelector("#warnings");
  warnings.replaceChildren();
  (status.warnings || []).forEach((warning) => {
    const item = document.createElement("li");
    item.textContent = warning;
    warnings.append(item);
  });
  renderCost(status.cost);
  if (status.state === "error") showAlert(status.message || "The edit failed.");
  else showAlert("");
  renderOutputs(status);
}

function renderCost(cost) {
  const node = document.querySelector("#cost");
  if (!cost || (cost.estimated_usd == null && cost.actual_usd == null)) {
    node.hidden = true;
    return;
  }
  node.hidden = false;
  node.replaceChildren();
  node.append(stat("Estimated", money(cost.estimated_usd)), stat("Actual", money(cost.actual_usd)));
  const note = document.createElement("p");
  note.className = "estimate";
  note.textContent = `${cost.frames_analyzed || 0} frames · ${cost.total_tokens || 0} tokens · ${cost.transcribe_seconds || 0}s transcribed. Rates are estimates.`;
  node.append(note);
}

function stat(label, value) {
  const wrap = document.createElement("div");
  const strong = document.createElement("strong");
  strong.textContent = value;
  const span = document.createElement("span");
  span.textContent = label;
  wrap.append(strong, span);
  return wrap;
}

function money(value) {
  const number = Number(value || 0);
  return "$" + number.toFixed(number < 0.01 ? 4 : 3);
}

function renderOutputs(status) {
  const compare = document.querySelector("#compare");
  const actions = document.querySelector("#actions");
  const key = JSON.stringify([currentJob, status.outputs, status.failed_output, status.retry_available, status.state]);
  if (key === outputKey) return;
  outputKey = key;
  compare.replaceChildren();
  actions.replaceChildren();
  const outputs = status.outputs || [];
  const failed = status.failed_output;
  if (!outputs.length && !failed && !status.retry_available && status.state !== "done") {
    compare.hidden = true;
    actions.hidden = true;
    return;
  }
  compare.hidden = false;
  actions.hidden = false;
  compare.classList.toggle("two", outputs.length > 0);
  addVideo(compare, "Original", `/api/jobs/${currentJob}/media/source`);
  outputs.forEach((output) => {
    addVideo(compare, labelFor(output.orientation), output.url);
    const link = document.createElement("a");
    link.href = output.url + "?download=1";
    link.textContent = "Download " + labelFor(output.orientation);
    actions.append(link);
  });
  if (failed && !outputs.length) addVideo(compare, "Failed attempt", failed);
  if (status.retry_available) {
    const retry = button("Retry the edit", () => postJson("/api/jobs/" + currentJob + "/retry", {}));
    actions.append(retry);
  }
}

function addVideo(parent, title, src) {
  const wrap = document.createElement("div");
  const heading = document.createElement("h3");
  heading.textContent = title;
  const video = document.createElement("video");
  video.controls = true;
  video.playsInline = true;
  video.src = src;
  wrap.append(heading, video);
  parent.append(wrap);
}

async function loadPlan() {
  const response = await fetch("/api/jobs/" + currentJob + "/plan");
  if (!response.ok) return;
  const payload = await response.json();
  const root = document.querySelector("#plan");
  root.hidden = false;
  root.replaceChildren();
  const heading = document.createElement("h2");
  heading.textContent = payload.plan.title || "Edit plan";
  const summary = document.createElement("p");
  summary.className = "lede";
  summary.textContent = payload.plan.summary || "";
  const stats = document.createElement("p");
  stats.className = "estimate";
  const segments = payload.plan.segments || [];
  const seconds = segments.reduce((total, segment) => total + Number(segment.output_duration || 0), 0);
  stats.textContent = segments.length + " shots · " + seconds.toFixed(1) + "s" +
    (segments.some(segment => segment.audio && segment.audio.voiceover_start != null) ? " · Narration over work footage" : "");
  root.append(heading, summary, stats);
  const list = document.createElement("div");
  list.className = "segments";
  (payload.plan.segments || []).forEach((segment, index) => {
    list.append(segmentCard(segment, index));
  });
  root.append(list);
  const rerender = button("Re-render these cuts", () => saveCuts(list));
  rerender.classList.add("secondary");
  const redirect = button("Run the director again", () => {
    const text = document.querySelector("#instructions").value.trim() || "Create a complete highlight with the selected editing style, clip mode and framing options.";
    postJson("/api/jobs/" + currentJob + "/redirect", { instructions: text, editing_style: document.querySelector('input[name="editing_style"]:checked').value, clip_mode: document.querySelector('input[name="clip_mode"]:checked').value, subject_tracking: document.querySelector('input[name="subject_tracking"]').checked });
  });
  redirect.classList.add("secondary");
  root.append(rerender, redirect);
  const shots = document.createElement("div");
  shots.className = "shots";
  ((payload.analysis && payload.analysis.frames) || []).forEach((frame) => {
    const figure = document.createElement("figure");
    const image = document.createElement("img");
    image.alt = (frame.vision && frame.vision.summary) || "Sampled frame";
    image.src = `/api/jobs/${currentJob}/frames/${frame.index}`;
    const caption = document.createElement("figcaption");
    const vision = frame.vision || {};
    caption.textContent = `${Number(frame.timestamp).toFixed(1)}s · ${vision.activity || vision.summary || "Frame"}`;
    figure.append(image, caption);
    shots.append(figure);
  });
  if (shots.childElementCount) root.append(shots);
  const heard = document.createElement("p");
  heard.className = "lede";
  const transcript = (payload.analysis && payload.analysis.transcript) || {};
  heard.textContent = transcript.text
    ? "Heard: " + transcript.text
    : transcript.skipped_reason || "No speech transcript.";
  root.append(heard);
}

function segmentCard(segment, index) {
  const card = document.createElement("article");
  card.className = "segment";
  card.dataset.index = String(index);
  const header = document.createElement("header");
  const keep = document.createElement("label");
  const box = document.createElement("input");
  box.type = "checkbox";
  box.className = "keep";
  box.checked = segment.keep !== false;
  keep.append(box, document.createTextNode(" Keep"));
  const score = document.createElement("span");
  score.textContent = "Importance " + Number(segment.importance || 0).toFixed(2);
  header.append(keep, score);
  const reason = document.createElement("p");
  reason.className = "reason";
  reason.textContent = segment.reason || "";
  const voice = segment.audio || {};
  if (voice.voiceover_start != null) {
    const note = document.createElement("p");
    note.className = "estimate";
    note.textContent = "Narration from " + Number(voice.voiceover_start).toFixed(1) + "–" + Number(voice.voiceover_end).toFixed(1) + "s of the original";
    card.append(note);
  }
  const times = document.createElement("div");
  times.className = "times";
  times.append(timeField("In", "start", segment.source_start), timeField("Out", "end", segment.source_end));
  card.append(header, reason, times);
  return card;
}

function timeField(label, cls, value) {
  const wrap = document.createElement("label");
  wrap.textContent = label;
  const input = document.createElement("input");
  input.type = "number";
  input.step = "0.1";
  input.min = "0";
  input.className = cls;
  input.value = Number(value || 0).toFixed(2);
  wrap.append(input);
  return wrap;
}

function saveCuts(list) {
  const segments = [...list.querySelectorAll(".segment")].map((card) => ({
    keep: card.querySelector(".keep").checked,
    source_start: Number(card.querySelector(".start").value),
    source_end: Number(card.querySelector(".end").value),
  }));
  postJson("/api/jobs/" + currentJob + "/plan", { segments });
}

async function postJson(url, payload) {
  showAlert("");
  try {
    const response = await fetch(url, {
      method: url.endsWith("/plan") ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || "The request failed.");
    setMessage("Queued…", 4);
    submit.disabled = true;
    submit.textContent = "Working…";
    document.querySelector("#plan").hidden = true;
    if (timer) clearInterval(timer);
    poll();
    timer = setInterval(poll, 1600);
  } catch (error) {
    showAlert(error.message || "Could not reach the studio. Please try again.");
  }
}

function button(text, onClick) {
  const node = document.createElement("button");
  node.type = "button";
  node.textContent = text;
  node.addEventListener("click", onClick);
  return node;
}

function setMessage(text, progress) {
  document.querySelector(".bar").setAttribute("aria-valuenow", Math.round(Math.max(0, Math.min(100, progress || 0))));
  document.querySelector("#job-message").textContent = text;
  document.querySelector("#fill").style.width = Math.max(0, Math.min(100, progress || 0)) + "%";
}

function showAlert(text) {
  const node = document.querySelector("#alert");
  node.hidden = !text;
  node.textContent = text || "";
}

function showFormAlert(text) {
  formAlert.hidden = !text;
  formAlert.textContent = text || "";
}

function labelFor(orientation) {
  if (/^clip-[1-3]$/.test(orientation)) return "Highlight " + orientation.slice(-1);
  if (orientation === "landscape") return "landscape";
  if (orientation === "portrait") return "portrait";
  return orientation;
}
