// Format a media timestamp for the accessible playback controls.
function mediaTime(seconds) {
  if (!Number.isFinite(seconds)) return '0:00';
  return `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;
}

// Paint decoded frames on a normal canvas rather than an embedded video surface.
function createCanvasPlayer(video, label) {
  const wrapper = document.createElement('div');
  wrapper.className = 'preview-player';
  wrapper.hidden = true;
  video.before(wrapper);
  wrapper.append(video);
  video.hidden = false;
  video.controls = false;
  video.className = 'preview-decoder';
  video.tabIndex = -1;
  video.setAttribute('aria-hidden', 'true');
  const canvas = document.createElement('canvas');
  canvas.className = 'preview-canvas';
  canvas.setAttribute('role', 'img');
  canvas.setAttribute('aria-label', `${label} video frame`);
  const controls = document.createElement('div');
  controls.className = 'preview-controls';
  const play = document.createElement('button');
  play.type = 'button';
  play.textContent = 'Play';
  play.setAttribute('aria-label', `Play ${label.toLowerCase()}`);
  const seek = document.createElement('input');
  seek.type = 'range';
  seek.min = '0';
  seek.max = '0';
  seek.step = '0.01';
  seek.value = '0';
  seek.setAttribute('aria-label', `Seek ${label.toLowerCase()}`);
  const clock = document.createElement('span');
  clock.className = 'preview-clock';
  const mute = document.createElement('button');
  mute.type = 'button';
  mute.textContent = 'Mute';
  mute.setAttribute('aria-label', `Mute ${label.toLowerCase()}`);
  controls.append(play, seek, clock, mute);
  wrapper.append(canvas, controls);
  const context = canvas.getContext('2d', {alpha: false});
  let animation, lastTime = -1;

  // Copy a decoded frame and preserve it when playback pauses or ends.
  function paint() {
    if (video.readyState < 2 || !video.videoWidth) return;
    if (canvas.width !== video.videoWidth || canvas.height !== video.videoHeight) {
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
    }
    try {
      context.drawImage(video, 0, 0, canvas.width, canvas.height);
      canvas.dataset.renderedTime = String(video.currentTime);
      canvas.dataset.frameCount = String(Number(canvas.dataset.frameCount || 0) + 1);
    } catch (exception) {
      clock.textContent = 'Loading frame…';
    }
  }
  // Reflect actual decoder time and playback state in the custom controls.
  function syncControls() {
    play.textContent = video.paused ? 'Play' : 'Pause';
    play.setAttribute('aria-label', `${video.paused ? 'Play' : 'Pause'} ${label.toLowerCase()}`);
    seek.max = Number.isFinite(video.duration) ? String(video.duration) : '0';
    seek.value = String(video.currentTime);
    clock.textContent = `${mediaTime(video.currentTime)} / ${mediaTime(video.duration)}`;
    mute.textContent = video.muted ? 'Unmute' : 'Mute';
    mute.setAttribute('aria-label', `${video.muted ? 'Unmute' : 'Mute'} ${label.toLowerCase()}`);
  }
  // Refresh only changed frames while playing, without a native composited surface.
  function tick() {
    if (video.currentTime !== lastTime) {
      paint();
      syncControls();
      lastTime = video.currentTime;
    }
    if (!video.paused && !video.ended) animation = requestAnimationFrame(tick);
  }
  // Start or pause playback and surface autoplay/decoder failures locally.
  async function togglePlayback() {
    if (!video.paused) return video.pause();
    document.querySelectorAll('video').forEach(other => {
      // Pause the other preview to avoid competing audio and unnecessary decoding.
      if (other !== video) other.pause();
    });
    try {
      if (video.ended) video.currentTime = 0;
      await video.play();
    } catch (exception) {
      clock.textContent = 'Playback unavailable';
    }
  }
  play.addEventListener('click', togglePlayback);
  // Seek using the decoder, then repaint when the requested frame becomes available.
  seek.addEventListener('input', () => {
    video.currentTime = Number(seek.value);
    syncControls();
  });
  // Toggle audio independently of the canvas drawing path.
  mute.addEventListener('click', () => {
    video.muted = !video.muted;
    syncControls();
  });
  // Initialize the time range as soon as media metadata is ready.
  video.addEventListener('loadedmetadata', syncControls);
  // Decode a still frame explicitly before the user starts playback.
  video.addEventListener('loadeddata', () => {
    if (video.paused && video.currentTime === 0 && video.duration > 0) {
      video.currentTime = Math.min(0.01, video.duration / 2);
    }
  });
  ['loadeddata', 'seeked', 'pause', 'ended'].forEach(event => {
    // Repaint still frames after load, seeking, pause, or the end of playback.
    video.addEventListener(event, () => {
      paint();
      syncControls();
    });
  });
  // Start one frame loop per player whenever playback resumes.
  video.addEventListener('play', () => {
    cancelAnimationFrame(animation);
    lastTime = -1;
    tick();
  });
  // Stop unused frame loops while keeping the last painted image visible.
  video.addEventListener('pause', () => cancelAnimationFrame(animation));
  // Release playback work when a user switches to another browser tab.
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) video.pause();
    else paint();
  });
  syncControls();
  return {
    // Attach a local blob or completed server asset to this preview.
    load(source) {
      wrapper.hidden = false;
      lastTime = -1;
      video.src = source;
      video.load();
    },
    // Release the decoder and hide a preview that is no longer in use.
    clear() {
      video.pause();
      video.removeAttribute('src');
      video.load();
      wrapper.hidden = true;
      context.clearRect(0, 0, canvas.width, canvas.height);
    },
  };
}
