/**
 * Published Posts Archive Module
 */
import { api } from './api.js';
import { renderPostCard } from './cards.js';

export let publishedData = [];

export async function loadPublished() {
  const grid = document.getElementById("published-grid");
  try {
    publishedData = await api.get("/api/posts/published");
    renderPublishedGrid(publishedData);
  } catch (err) {
    if (grid) {
      grid.innerHTML = `
        <div class="empty-state">
          <div class="empty-icon">⚠️</div>
          <div class="empty-title">Ошибка загрузки архива</div>
          <div class="empty-desc">${err.message}</div>
          <button class="btn btn-sm btn-primary" onclick="window.loadPublished()">Повторить</button>
        </div>
      `;
    }
  }
}

export function renderPublishedGrid(posts) {
  const grid = document.getElementById("published-grid");
  if (!grid) return;

  if (!posts || posts.length === 0) {
    grid.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">📁</div>
        <div class="empty-title">Архив опубликованных пуст</div>
        <div class="empty-desc">Когда посты выйдут в Telegram-канал, они появятся в этом разделе.</div>
      </div>
    `;
    return;
  }

  grid.innerHTML = posts.map((p, idx) => renderPostCard(p, idx, posts.length, true)).join("");
}

window.loadPublished = loadPublished;
