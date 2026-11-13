const helloButton = document.querySelector('#hello-button');
const helloResult = document.querySelector('#hello-result');
const healthStatus = document.querySelector('#health-status');
const healthDot = document.querySelector('#health-dot');

async function getJSON(url) {
  const response = await fetch(url, {
    headers: { Accept: 'application/json' },
    signal: AbortSignal.timeout(5000),
    cache: 'no-store',
  });
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response.json();
}

helloButton.addEventListener('click', async () => {
  helloButton.disabled = true;
  helloResult.textContent = 'Calling the backend…';
  try {
    const data = await getJSON(helloButton.dataset.url);
    helloResult.textContent = data.message;
  } catch (error) {
    helloResult.textContent = 'Could not reach the backend. Check the server and try again.';
  } finally {
    helloButton.disabled = false;
  }
});

async function checkHealth() {
  try {
    const data = await getJSON(healthStatus.dataset.url);
    if (data.status !== 'ok' || data.database !== 'connected') throw new Error('Unhealthy');
    healthStatus.textContent = 'Backend online · Database connected';
    healthDot.classList.add('online');
  } catch (error) {
    healthStatus.textContent = 'Connection failed. Check the server, then reload this page.';
    healthDot.classList.add('offline');
  }
}

checkHealth();
