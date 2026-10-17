/**
 * Main Application Entrypoint & Orchestrator
 */
import { api } from './api.js';
import { showToast } from './toast.js';
import { loadQueue } from './queue.js';
import { loadPublished } from './published.js';
import { loadTopics } from './topics.js';
import { loadSettings } from './settings.js';
import { checkAuthAndInit, handleLogout } from './auth.js';

let currentTab = 'queue';
let currentViewMode = localStorage.getItem('vh_view_mode') || 'grid';

export function setViewMode(mode) {
  currentViewMode = mode;
  localStorage.setItem('vh_view_mode', mode);

  const grids = document.querySelectorAll('.posts-grid');
  grids.forEach(g => {
    g.classList.remove('mode-grid', 'mode-feed');
    g.classList.add(`mode-${mode}`);
  });

  const btnGrid = document.getElementById('btn-view-grid');
  const btnFeed = document.getElementById('btn-view-feed');
  if (btnGrid) btnGrid.classList.toggle('active', mode === 'grid');
  if (btnFeed) btnFeed.classList.toggle('active', mode === 'feed');
}

export function switchTab(tabName) {
  currentTab = tabName;
  document.querySelectorAll('.tab-btn').forEach(b => {
    b.classList.toggle('active', b.id === `tab-btn-${tabName}`);
  });

  document.querySelectorAll('.tab-content').forEach(s => {
    s.classList.toggle('active', s.id === `tab-${tabName}`);
  });

  if (tabName === 'queue') loadQueue();
  else if (tabName === 'published') loadPublished();
  else if (tabName === 'topics') loadTopics();
  else if (tabName === 'settings') loadSettings();
}

export async function triggerPipeline() {
  const btn = document.getElementById('btn-scout-trigger');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '⏳ Поиск твитов...';
  }
  try {
    await api.post('/api/scout/trigger', {});
    showToast("Сбор твитов (Scout) запущен в фоне!", "success");
    setTimeout(loadQueue, 3000);
  } catch (err) {
    showToast("Ошибка запуска Scout: " + err.message, "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = `
        <svg class="btn-icon" width="13" height="13" viewBox="0 0 24 24" fill="currentColor">
          <polygon points="5 3 19 12 5 21 5 3"></polygon>
        </svg>
        Собрать твиты (Scout)
      `;
    }
  }
}

async function updateStatuses() {
  try {
    const statusData = await api.get('/api/status');
    const aiPill = document.getElementById('ai-status-pill');
    const aiText = document.getElementById('ai-status-text');
    if (aiPill && aiText && statusData) {
      const isOk = statusData.ai_status === 'ok' || statusData.gemini_status === 'ok';
      aiPill.className = `status-pill ${isOk ? 'status-pill-ok' : 'status-pill-error'}`;
      aiText.innerText = isOk ? 'ИИ: ОК' : 'ИИ: Ошибка';
    }
  } catch (e) {}

  try {
    const pipeData = await api.get('/api/pipeline/status');
    const pipePill = document.getElementById('pipeline-status-pill');
    const pipeText = document.getElementById('pipeline-status-text');
    if (pipePill && pipeText && pipeData) {
      const isRunning = pipeData.status === 'RUNNING';
      pipePill.className = `status-pill ${isRunning ? 'status-pill-pending' : 'status-pill-ok'}`;
      pipeText.innerText = isRunning ? 'Pipeline: Сбор...' : 'Pipeline: Свободен';
    }
  } catch (e) {}

  // Clock
  const clockEl = document.getElementById('current-server-time');
  if (clockEl) {
    const now = new Date();
    clockEl.innerText = now.toLocaleTimeString('ru-RU');
  }
}

// Window bindings
window.setViewMode = setViewMode;
window.switchTab = switchTab;
window.triggerPipeline = triggerPipeline;

// DOM Ready initialization
document.addEventListener('DOMContentLoaded', () => {
  setViewMode(currentViewMode);
  
  // Authenticate first, then load data and start background refresh
  checkAuthAndInit(() => {
    loadQueue();
    updateStatuses();
    setInterval(updateStatuses, 10000);
  });
});
