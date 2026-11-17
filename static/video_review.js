(() => {
  "use strict";

  const byId = id => document.getElementById(id);
  const video = byId("local-video");
  const filePicker = byId("local-video-file");
  const startButton = byId("start-video-analysis");
  const stopButton = byId("stop-video-analysis");
  const sampleCanvas = document.createElement("canvas");
  const sampleContext = sampleCanvas.getContext("2d");
  const preview = byId("video-detection-preview");
  const previewContext = preview.getContext("2d");
  const activeTraining = new Set(["preparing", "running", "stopping"]);
  let file;
  let videoUrl;
  let sessionId;
  let activeRun;
  let trainingActive = false;
  let detectorEnabled = true;
  let detectorStatus = "Checking detector";
  let queueCount = 0;
  let queueKnown = false;

  async function videoApi(path, options = {}) {
    const response = await fetch(path, { credentials: "same-origin", cache: "no-store", ...options });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(body.detail || body.message || "Request failed (" + response.status + ")");
      error.status = response.status;
      throw error;
    }
    return body;
  }

  function showMessage(text, isError = false) {
    const message = byId("video-analysis-message");
    message.textContent = text;
    message.classList.toggle("error", isError);
  }

  function videoReady() {
    return Boolean(file && !video.error && video.readyState >= HTMLMediaElement.HAVE_METADATA &&
      video.videoWidth > 0 && video.videoHeight > 0);
  }

  function updateControls() {
    const ready = videoReady();
    startButton.disabled = !ready || Boolean(activeRun) || trainingActive || !detectorEnabled;
    stopButton.disabled = !activeRun;
    filePicker.disabled = Boolean(activeRun);
    byId("local-video-state").textContent = ready ? "Video ready" : file?.name || "No file selected";
    let note = "Select a local video file to enable analysis.";
    if (trainingActive) note = "Analysis is available when model training finishes.";
    else if (!detectorEnabled) note = "Turn on object detection in Settings to enable analysis.";
    else if (ready) note = "Choose a speed and sampling interval.";
    else if (video.error || (file && video.readyState >= HTMLMediaElement.HAVE_METADATA)) {
      note = "Choose a playable MP4 (H.264) or WebM file.";
    } else if (file) note = "Waiting for the browser to read the selected video.";
    byId("video-speed-note").textContent = note;
    byId("video-engine-state").lastChild.textContent = " " +
      (activeRun ? "Analyzing local video" : detectorStatus);
  }

  async function checkDetector() {
    try {
      const status = await videoApi("/api/admin/status");
      trainingActive = activeTraining.has(status.services?.training);
      detectorEnabled = status.services?.object_detection !== "stopped";
      detectorStatus = trainingActive ? "Training active · video paused" :
        !detectorEnabled ? "Object detection is off" : "Object detection enabled";
      if (activeRun && (trainingActive || !detectorEnabled)) {
        stopAnalysis(
          trainingActive ? "Analysis paused while model training is active." :
            "Object detection was turned off. Enable it in Settings and try again.",
          "Paused", true
        );
      }
    } catch {
      detectorStatus = "Detector status unavailable";
    }
    updateControls();
  }

  function updateQueue() {
    byId("video-review-queue-count").textContent = queueKnown ? queueCount.toLocaleString() : "Unavailable";
  }

  async function checkQueue() {
    try {
      const samples = await videoApi("/api/admin/ml/samples?limit=1");
      queueCount = Number(samples.reviewable_total || 0);
      queueKnown = true;
    } catch {
      queueKnown = false;
    }
    updateQueue();
  }

  function formatTime(seconds) {
    if (!Number.isFinite(seconds)) return "—";
    const total = Math.floor(seconds);
    const pad = value => String(value).padStart(2, "0");
    const minutes = Math.floor(total / 60);
    return minutes < 60 ? minutes + ":" + pad(total % 60) :
      Math.floor(minutes / 60) + ":" + pad(minutes % 60) + ":" + pad(total % 60);
  }

  function updateProgress() {
    const duration = video.duration;
    const current = video.currentTime;
    byId("video-time-label").textContent = formatTime(current) + " / " + formatTime(duration);
    byId("video-progress-bar").value = Number.isFinite(duration) && duration > 0 ?
      Math.min(100, current / duration * 100) : 0;
  }

  function drawPreview(objects, frameSize) {
    preview.width = frameSize[0];
    preview.height = frameSize[1];
    const ctx = previewContext;
    ctx.drawImage(sampleCanvas, 0, 0, preview.width, preview.height);
    const line = Math.max(2, Math.round(preview.width / 420));
    ctx.lineWidth = line;
    ctx.font = "600 " + Math.max(13, Math.round(preview.width / 46)) + "px system-ui,sans-serif";
    for (const item of objects) {
      const [x1, y1, x2, y2] = item.box;
      const label = item.label + " · " + (Number(item.score) * 100).toFixed(0) + "%";
      ctx.strokeStyle = ctx.fillStyle = "#00e0c0";
      ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
      ctx.fillRect(x1, Math.max(0, y1 - line - 22), ctx.measureText(label).width + 12, 24);
      ctx.fillStyle = "#062b31";
      ctx.fillText(label, x1 + 6, Math.max(17, y1 - line - 5));
    }
    byId("video-preview-placeholder").classList.add("hidden");
  }

  function updateCounts(run) {
    byId("video-frames-count").textContent = run.frames.toLocaleString();
    byId("video-objects-count").textContent = run.objects.toLocaleString();
    byId("video-samples-count").textContent = run.saved.toLocaleString();
  }

  function stopAnalysis(message, progress = "Paused", isError = false) {
    const run = activeRun;
    if (run) {
      activeRun = null;
      window.clearInterval(run.timer);
      run.controller.abort();
    }
    video.pause();
    updateProgress();
    updateControls();
    byId("video-progress-state").textContent = progress;
    showMessage(message, isError);
  }

  function finishAnalysis(run) {
    const message = run.saved ?
      "Video finished. " + run.saved.toLocaleString() + " crops are available in Review detections." :
      "Video finished. No crops were saved.";
    stopAnalysis(message, "Complete");
  }

  async function analyzeFrame(run) {
    const sourceTime = video.currentTime;
    run.lastSampleTime = sourceTime;
    run.busy = true;
    video.pause();
    try {
      const scale = Math.min(1, 1280 / Math.max(video.videoWidth, video.videoHeight));
      sampleCanvas.width = Math.max(1, Math.round(video.videoWidth * scale));
      sampleCanvas.height = Math.max(1, Math.round(video.videoHeight * scale));
      sampleContext.drawImage(video, 0, 0, sampleCanvas.width, sampleCanvas.height);
      const blob = await new Promise(resolve => sampleCanvas.toBlob(resolve, "image/jpeg", 0.82));
      if (run !== activeRun) return;
      if (!blob) throw new Error("Could not prepare this frame.");
      const signal = run.controller.signal;
      if (!sessionId) {
        const session = await videoApi("/api/admin/video-review/sessions", {
          method: "POST", signal,
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ source_type: "local_video", file_name: file.name })
        });
        if (run !== activeRun) return;
        sessionId = session.session_id;
      }
      const saveSamples = byId("save-video-samples").checked;
      const result = await videoApi("/api/admin/video-review/sessions/" + encodeURIComponent(sessionId) + "/frame", {
        method: "POST", signal, body: blob,
        headers: { "Content-Type": "image/jpeg", "X-Video-Time": String(sourceTime), "X-Save-Samples": saveSamples ? "1" : "0" }
      });
      if (run !== activeRun) return;
      const saved = Number(result.samples_saved || 0);
      run.frames += 1;
      run.objects += result.objects.length;
      run.saved += saved;
      if (queueKnown) queueCount += saved;
      updateCounts(run);
      updateQueue();
      byId("video-frame-time").textContent = "Video " + formatTime(sourceTime);
      drawPreview(result.objects, result.frame_size);
      if (result.sample_limit_reached) {
        byId("save-video-samples").checked = false;
        showMessage("Sample storage is full. Analysis will continue with crop saving turned off.");
      } else {
        showMessage(result.objects.length + " detected at " + formatTime(sourceTime) +
          (saveSamples ? " · " + saved + " crops saved" : " · crop saving off"));
      }
      if (video.ended) finishAnalysis(run);
      else await video.play();
    } catch (error) {
      if (run === activeRun) {
        if (error.status === 404) sessionId = null;
        stopAnalysis(error.message, "Analysis paused", true);
      }
    } finally {
      run.busy = false;
    }
  }

  function tick(run) {
    if (run !== activeRun) return;
    updateProgress();
    if (run.busy) return;
    if (video.ended) {
      finishAnalysis(run);
      return;
    }
    if (video.paused || video.seeking || video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) return;
    const interval = Number(byId("video-sample-interval").value);
    if (run.lastSampleTime === null || video.currentTime < run.lastSampleTime ||
        video.currentTime - run.lastSampleTime >= interval) {
      void analyzeFrame(run);
    }
  }

  async function startAnalysis() {
    if (startButton.disabled || !videoReady() || activeRun) return;
    const run = {
      controller: new AbortController(), lastSampleTime: null, busy: false,
      frames: 0, objects: 0, saved: 0, timer: 0
    };
    activeRun = run;
    updateCounts(run);
    video.currentTime = 0;
    video.playbackRate = Number(byId("local-video-speed").value);
    updateControls();
    byId("video-progress-state").textContent = "Analyzing at " + video.playbackRate + "×";
    showMessage("Waiting for the first sampled frame…");
    try {
      await video.play();
      if (run !== activeRun) return;
      run.timer = window.setInterval(() => tick(run), 100);
      tick(run);
    } catch (error) {
      if (run === activeRun) stopAnalysis("Could not play this video. " + error.message, "Playback failed", true);
    }
  }

  filePicker.addEventListener("change", () => {
    file = filePicker.files[0];
    sessionId = null;
    video.pause();
    if (videoUrl) URL.revokeObjectURL(videoUrl);
    videoUrl = file ? URL.createObjectURL(file) : null;
    if (videoUrl) video.src = videoUrl;
    else video.removeAttribute("src");
    video.load();
    updateCounts({ frames: 0, objects: 0, saved: 0 });
    byId("video-preview-placeholder").classList.remove("hidden");
    preview.width = preview.width;
    byId("video-frame-time").textContent = "No frame yet";
    byId("video-progress-state").textContent = file ? "Reading video" : "Waiting for video";
    showMessage(file ? "Reading " + file.name + "…" : "Choose a local video.");
    updateProgress();
    updateControls();
  });
  function onVideoReady() {
    updateProgress();
    updateControls();
    if (!file || activeRun || byId("video-progress-state").textContent !== "Reading video") return;
    if (!videoReady()) {
      byId("video-progress-state").textContent = "Unsupported video";
      showMessage("This file has no playable video track. Try MP4 (H.264) or WebM.", true);
      return;
    }
    byId("video-progress-state").textContent = "Ready";
    showMessage("Choose Start fast analysis to box objects. Enable crop saving to send them to Review detections.");
  }
  video.addEventListener("loadedmetadata", onVideoReady);
  video.addEventListener("canplay", onVideoReady);
  video.addEventListener("error", () => {
    if (file) {
      stopAnalysis("This video could not be decoded. Try MP4 (H.264) or WebM.", "Playback failed", true);
    }
  });
  video.addEventListener("timeupdate", updateProgress);
  video.addEventListener("ended", () => {
    if (activeRun && !activeRun.busy) finishAnalysis(activeRun);
  });
  byId("local-video-speed").addEventListener("change", event => {
    video.playbackRate = Number(event.target.value);
    if (activeRun) byId("video-progress-state").textContent = "Analyzing at " + video.playbackRate + "×";
  });
  startButton.addEventListener("click", startAnalysis);
  stopButton.addEventListener("click", () => stopAnalysis("Analysis stopped. Saved crops remain in Review detections."));

  const themeToggle = byId("theme-toggle");
  function syncTheme() {
    const light = document.body.classList.contains("theme-light");
    themeToggle.setAttribute("aria-pressed", String(light));
    byId("theme-toggle-label").textContent = light ? "Dark mode" : "Light mode";
  }
  document.body.classList.toggle("theme-light", localStorage.getItem("vaos-theme") === "light");
  themeToggle.addEventListener("click", () => {
    const light = document.body.classList.toggle("theme-light");
    localStorage.setItem("vaos-theme", light ? "light" : "dark");
    syncTheme();
  });
  syncTheme();
  void checkDetector();
  void checkQueue();
  window.setInterval(checkDetector, 15000);
})();
