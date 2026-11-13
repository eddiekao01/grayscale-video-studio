const form = document.querySelector('#upload-form');
const input = document.querySelector('#clip');
const button = document.querySelector('#convert');
const progress = document.querySelector('#progress');
const state = document.querySelector('#state');
const detail = document.querySelector('#detail');
const error = document.querySelector('#error');
const original = document.querySelector('#original');
const result = document.querySelector('#result');
const download = document.querySelector('#download');
const openVideo = document.querySelector('#open-video');
const reloadPreview = document.querySelector('#reload-preview');
const originalPlayer = createCanvasPlayer(original, 'Original');
const resultPlayer = createCanvasPlayer(result, 'Grayscale');
let originalURL, active = false, validSelection = false, pollTimer;

// Update the status label, message, and measured or indeterminate progress bar.
function update(label, message, value) {
  state.textContent = label;
  detail.textContent = message;
  if (value === null) progress.removeAttribute('value');
  else progress.value = value;
}
// Re-enable the controls after a complete or failed job.
function unlock() {
  active = false;
  button.disabled = !validSelection;
  input.disabled = false;
}
// Display an error while preserving the original video for retry.
function fail(message) {
  clearTimeout(pollTimer);
  error.textContent = message;
  error.hidden = false;
  update('Failed', 'Choose another video or retry the conversion.', 0);
  unlock();
}
// Reset the previous output preview before starting another video.
function clearResult() {
  resultPlayer.clear();
  download.hidden = openVideo.hidden = reloadPreview.hidden = true;
  document.querySelector('#result-empty').hidden = false;
}
// Render persistent queue progress and expose the final video only after merging.
function renderJob(job) {
  if (job.status === 'failed') return fail(job.error || 'Video processing failed.');
  if (job.status === 'completed') {
    resultPlayer.load(job.result_url);
    download.href = openVideo.href = job.result_url;
    download.hidden = openVideo.hidden = reloadPreview.hidden = false;
    document.querySelector('#result-empty').hidden = true;
    update('Completed', 'All clips processed and merged. Your video is ready.', 100);
    unlock();
    return;
  }
  if (job.status === 'processing') {
    update('Transforming', `Processed ${job.completed_segments}/${job.expected_segments} clips. Completed clips are saved while the rest finish.`, Math.floor(job.completed_segments / job.expected_segments * 100));
  } else if (job.status === 'merging' || job.status === 'merge_pending') {
    update('Merging', 'All clips succeeded. Joining them in their original order…', null);
  } else if (job.status === 'splitting') {
    update('Splitting', 'Preparing and validating clips under 10 seconds…', null);
  } else {
    update('Queued', 'Upload saved. Waiting for a video worker…', null);
  }
  pollTimer = setTimeout(() => pollJob(job.status_url), 700);
}
// Poll durable job status and tolerate a few temporary connection failures.
async function pollJob(url, failures = 0) {
  try {
    const response = await fetch(url, {cache: 'no-store', signal: AbortSignal.timeout(10000)});
    if (!response.ok) throw new Error(`Status request failed (HTTP ${response.status}).`);
    const job = await response.json();
    error.hidden = true;
    renderJob(job);
  } catch (exception) {
    if (failures < 4) {
      detail.textContent = 'Connection interrupted. Reconnecting; server processing continues…';
      pollTimer = setTimeout(() => pollJob(url, failures + 1), 1500);
    } else {
      fail(`Cannot retrieve progress. Server processing may continue. Status: ${url}`);
    }
  }
}
// Show the source preview without applying the service’s per-clip limits.
input.addEventListener('change', () => {
  validSelection = false;
  error.hidden = true;
  clearResult();
  if (originalURL) URL.revokeObjectURL(originalURL);
  originalPlayer.clear();
  document.querySelector('#original-empty').hidden = false;
  button.disabled = true;
  const file = input.files[0];
  if (!file) return update('Ready', 'Choose a video to get started.', 0);
  if (file.size === 0) return fail('Choose a non-empty video file.');
  originalURL = URL.createObjectURL(file);
  originalPlayer.load(originalURL);
  document.querySelector('#original-empty').hidden = true;
  update('Ready', 'Reading video metadata…', 0);
});
// Accept long videos and explain that the server will split them automatically.
original.addEventListener('loadedmetadata', () => {
  if (!Number.isFinite(original.duration) || original.duration <= 0) return fail('The video duration could not be read.');
  if (!input.files.length) return;
  validSelection = true;
  button.disabled = false;
  update('Ready', `${input.files[0].name} · ${original.duration.toFixed(2)} seconds · clips are processed independently, then merged.`, 0);
});
// Explain when the browser cannot preview the selected video.
original.addEventListener('error', () => {
  if (input.files.length && original.hasAttribute('src')) fail('This browser cannot preview the selected video. Choose another video.');
});
// Upload once and hand progress reporting over to the persistent job status endpoint.
form.addEventListener('submit', event => {
  event.preventDefault();
  if (active || button.disabled) return;
  active = true;
  button.disabled = input.disabled = true;
  error.hidden = true;
  clearResult();
  const body = new FormData();
  body.append('clip', input.files[0]);
  const xhr = new XMLHttpRequest();
  xhr.open('POST', form.action);
  xhr.responseType = 'json';
  xhr.timeout = 30000;
  xhr.setRequestHeader('X-CSRFToken', form.querySelector('[name=csrfmiddlewaretoken]').value);
  // Show actual upload progress, separate from segment completion progress.
  xhr.upload.onprogress = event => {
    if (event.lengthComputable) update('Uploading', 'Sending video to the server…', Math.round(event.loaded / event.total * 100));
  };
  // Mark bytes uploaded while waiting for the server to accept the job.
  xhr.upload.onload = () => update('Uploaded', 'Upload complete. Creating the processing job…', null);
  // Start status polling after the server returns a durable job identifier.
  xhr.onload = () => {
    if (xhr.status === 202) {
      history.replaceState(null, '', `?video=${encodeURIComponent(xhr.response.video_id)}`);
      renderJob(xhr.response);
    }
    else fail(xhr.response?.error || `Upload failed (HTTP ${xhr.status}).`);
  };
  // Report a network interruption during upload.
  xhr.onerror = () => fail('Connection lost during upload. Please retry.');
  // Report a client upload timeout independently of video duration.
  xhr.ontimeout = () => fail('Upload timed out. Please retry.');
  // Report an interrupted upload without leaving the controls disabled.
  xhr.onabort = () => fail('Upload cancelled. Please retry.');
  update('Uploading', 'Sending video to the server…', 0);
  xhr.send(body);
});

// Explain native playback failures without misreporting a successful conversion as failed.
result.addEventListener('error', () => {
  if (!result.hasAttribute('src')) return;
  error.hidden = false;
  error.textContent = 'Conversion completed, but this browser could not play the preview. Download the video or open it in another browser.';
});
// Report preview buffering separately from completed server processing.
result.addEventListener('waiting', () => {
  if (result.hasAttribute('src')) detail.textContent = 'Conversion completed. Buffering video playback…';
});
// Restore the completion message once the preview starts playing.
result.addEventListener('playing', () => {
  error.hidden = true;
  detail.textContent = 'Your grayscale video is playing. Conversion completed successfully.';
});

// Reset the native video decoder without repeating any transformation service calls.
reloadPreview.addEventListener('click', () => {
  error.hidden = true;
  resultPlayer.load(download.href);
  detail.textContent = 'Preview reloaded. Press Play grayscale to continue.';
});

// Restore saved source and result previews without uploading or transforming again.
async function restoreJob(videoId) {
  active = true;
  button.disabled = input.disabled = true;
  try {
    const response = await fetch(`/api/videos/${encodeURIComponent(videoId)}/`, {cache: 'no-store'});
    if (!response.ok) throw new Error('This saved video could not be loaded.');
    const job = await response.json();
    originalPlayer.load(job.source_url);
    document.querySelector('#original-empty').hidden = true;
    renderJob(job);
  } catch (exception) {
    fail(exception.message);
  }
}
const savedVideo = new URLSearchParams(location.search).get('video');
if (savedVideo) restoreJob(savedVideo);

// Keep completion status accurate after pausing or finishing the result preview.
['pause', 'ended'].forEach(event => {
  // Describe the preview state independently from successful processing.
  result.addEventListener(event, () => {
    if (result.hasAttribute('src')) detail.textContent = 'Conversion completed. Use the preview controls to play or seek.';
  });
});
