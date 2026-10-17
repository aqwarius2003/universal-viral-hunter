/**
 * Queue Management Module
 */
import { api } from './api.js';
import { showToast } from './toast.js';
import { renderPostCard } from './cards.js';

export let queueData = [];

export async function loadQueue() {
  const grid = document.getElementById("queue-grid");
  try {
    queueData = await api.get("/api/posts/queue");
    renderQueueGrid(queueData);
    
    const badge = document.getElementById("queue-badge");
    if (badge) badge.innerText = queueData ? queueData.length : 0;
  } catch (err) {
    if (grid) {
      grid.innerHTML = `
        <div class="empty-state">
          <div class="empty-icon">⚠️</div>
          <div class="empty-title">Ошибка загрузки очереди</div>
          <div class="empty-desc">${err.message}</div>
          <button class="btn btn-sm btn-primary" onclick="window.loadQueue()">Повторить</button>
        </div>
      `;
    }
  }
}

export function renderQueueGrid(posts) {
  const grid = document.getElementById("queue-grid");
  if (!grid) return;

  if (!posts || posts.length === 0) {
    grid.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">📭</div>
        <div class="empty-title">Очередь публикаций пуста</div>
        <div class="empty-desc">Нажмите «Собрать твиты (Scout)» в шапке, чтобы запустить поиск новых виральных постов.</div>
      </div>
    `;
    return;
  }

  grid.innerHTML = posts.map((p, idx) => renderPostCard(p, idx, posts.length, false)).join("");
}

export async function movePost(tweetId, direction) {
  const idx = queueData.findIndex(p => p.tweet_id === tweetId);
  if (idx < 0) return;
  const targetIdx = idx + direction;
  if (targetIdx < 0 || targetIdx >= queueData.length) return;

  // Optimistic UI update
  const item = queueData.splice(idx, 1)[0];
  queueData.splice(targetIdx, 0, item);
  renderQueueGrid(queueData);

  try {
    const tweetIds = queueData.map(p => p.tweet_id);
    await api.post("/api/posts/reorder", { tweet_ids: tweetIds });
    showToast("Порядок очереди обновлен", "info");
  } catch (err) {
    showToast("Не удалось сохранить порядок: " + err.message, "error");
    loadQueue();
  }
}

export async function publishNow(tweetId) {
  if (!confirm("Опубликовать этот пост в Telegram прямо сейчас?")) return;
  try {
    await api.post(`/api/posts/${tweetId}/publish`, {});
    showToast("Пост успешно отправлен в Telegram!", "success");
    loadQueue();
  } catch (err) {
    showToast("Ошибка при публикации: " + err.message, "error");
  }
}

export function deletePost(tweetId) {
  const post = queueData.find(p => p.tweet_id === tweetId);
  const title = post ? (post.summary_data?.title_ru || post.text_raw?.slice(0, 50)) : '';
  const isVideo = Boolean(post && post.video_data);

  if (typeof window.showConfirmDialog === 'function') {
    window.showConfirmDialog({
      title: isVideo ? "🗑️ Удаление видеоролика" : "🗑️ Удаление публикации",
      message: `Вы действительно хотите удалить ${isVideo ? 'видеоролик' : 'публикацию'} <b>«${escapeHtml(title || 'Без названия')}»</b> из очереди? Это действие нельзя отменить.`,
      confirmText: "Да, удалить",
      onConfirm: async () => {
        try {
          await api.delete(`/api/posts/${tweetId}`);
          showToast("Пост удален из очереди", "info");
          loadQueue();
        } catch (err) {
          showToast("Ошибка при удалении: " + err.message, "error");
        }
      }
    });
  } else if (confirm("Удалить этот пост из очереди?")) {
    api.delete(`/api/posts/${tweetId}`).then(() => {
      showToast("Пост удален из очереди", "info");
      loadQueue();
    }).catch(err => showToast("Ошибка: " + err.message, "error"));
  }
}

export async function regenerateSinglePost(tweetId, btnEl) {
  if (btnEl) {
    btnEl.disabled = true;
    btnEl.innerHTML = '⏳...';
  }
  try {
    await api.post(`/api/posts/${tweetId}/reprocess`, { instruction: "Сделай заголовок ярче и перепиши текст" });
    showToast("Пост успешно переработан ИИ!", "success");
    loadQueue();
  } catch (err) {
    showToast("Ошибка ИИ: " + err.message, "error");
    if (btnEl) {
      btnEl.disabled = false;
      btnEl.innerHTML = '✨ ИИ';
    }
  }
}

window.loadQueue = loadQueue;
window.movePost = movePost;
window.publishNow = publishNow;
window.deletePost = deletePost;
window.regenerateSinglePost = regenerateSinglePost;
