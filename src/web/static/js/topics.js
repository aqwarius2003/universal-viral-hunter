/**
 * Topics & Filters Management Module
 */
import { api } from './api.js';
import { showToast, escapeHtml } from './toast.js';

export let allTopicsList = [];

export async function loadTopics() {
  const container = document.getElementById("topics-list-container");
  if (!container) return;
  try {
    allTopicsList = await api.get("/api/topics");
    renderTopics(allTopicsList);
  } catch (err) {
    console.error("Failed to load topics:", err);
  }
}

export function renderTopics(topics) {
  const container = document.getElementById("topics-list-container");
  if (!container) return;
  if (!topics || topics.length === 0) {
    container.innerHTML = `
      <div style="padding:14px; background:rgba(0,0,0,0.2); border-radius:8px; color:var(--tg-text-muted); font-size:0.85rem;">
        Топики еще не созданы. Нажмите «+ Добавить топик» для создания целевых тематик.
      </div>
    `;
    return;
  }

  container.innerHTML = topics.map(t => {
    const mediaLabel = {
      'any': 'Любой контент',
      'video': '🎬 Только видео',
      'photo': '🖼 Только фото',
      'photo_video': '🎬+🖼 Видео/фото'
    }[t.media_type] || t.media_type;

    return `
      <div style="display:flex; justify-content:space-between; align-items:center; padding:12px 16px; background:rgba(0,0,0,0.25); border:1px solid var(--tg-border); border-radius:8px;">
        <div>
          <div style="display:flex; align-items:center; gap:8px;">
            <strong style="font-size:0.95rem; color:#fff;">${escapeHtml(t.name)}</strong>
            <span class="status-badge ${t.is_active ? 'status-processed' : 'status-pending'}" style="font-size:0.7rem; padding:2px 8px;">
              ${t.is_active ? 'Активен' : 'Отключен'}
            </span>
            <span style="font-size:0.75rem; background:rgba(255,255,255,0.08); padding:2px 8px; border-radius:4px; color:var(--tg-text-muted);">
              ${mediaLabel}
            </span>
          </div>
          <div style="font-size:0.8rem; color:var(--tg-text-muted); margin-top:4px;">
            <span style="color:#aaa;">Запросы:</span> <code>${escapeHtml(t.search_queries)}</code>
            ${t.telegram_channel_id ? ` · <span style="color:var(--tg-primary);">Канал: ${escapeHtml(t.telegram_channel_id)}</span>` : ''}
          </div>
        </div>
        <div style="display:flex; gap:6px; flex-shrink:0;">
          <button type="button" class="btn btn-sm" onclick="window.openTopicModal(${t.id})">✏️ Изменить</button>
          <button type="button" class="btn btn-sm btn-danger" onclick="window.deleteTopic(${t.id})">🗑</button>
        </div>
      </div>
    `;
  }).join("");
}

export async function openTopicModal(topicId = null) {
  const form = document.getElementById("topic-form");
  if (form) form.reset();
  const idEl = document.getElementById("topic-id");
  if (idEl) idEl.value = "";
  const titleEl = document.getElementById("topic-modal-title");
  if (titleEl) titleEl.textContent = "Новый топик";
  const activeEl = document.getElementById("topic-is-active");
  if (activeEl) activeEl.checked = true;

  if (topicId !== null && topicId !== undefined) {
    let t = allTopicsList.find(x => String(x.id) === String(topicId));
    if (!t) {
      try {
        const fetched = await api.get(`/api/topics/${topicId}`);
        if (fetched && !fetched.error) t = fetched;
      } catch (e) {
        console.warn("Could not fetch topic:", e);
      }
    }
    if (t) {
      if (idEl) idEl.value = t.id;
      if (titleEl) titleEl.textContent = `Редактирование: ${t.name}`;
      const nameEl = document.getElementById("topic-name");
      if (nameEl) nameEl.value = t.name || "";
      const queriesEl = document.getElementById("topic-queries");
      if (queriesEl) queriesEl.value = t.search_queries || "";
      const mediaEl = document.getElementById("topic-media-type");
      if (mediaEl) mediaEl.value = t.media_type || "any";
      const chanEl = document.getElementById("topic-channel-id");
      if (chanEl) chanEl.value = t.telegram_channel_id || "";
      const sysEl = document.getElementById("topic-system-prompt");
      if (sysEl) sysEl.value = t.system_prompt || "";
      const tmplEl = document.getElementById("topic-prompt-template");
      if (tmplEl) tmplEl.value = t.prompt_template || "";
      if (activeEl) activeEl.checked = t.is_active === 1 || t.is_active === true;
    }
  }

  const modal = document.getElementById("topic-modal");
  if (modal) {
    modal.classList.add("active");
    modal.classList.add("open");
  }
}

export function closeTopicModal() {
  const modal = document.getElementById("topic-modal");
  if (modal) {
    modal.classList.remove("active");
    modal.classList.remove("open");
  }
}

export async function handleTopicSubmit(e) {
  e.preventDefault();
  const topicId = document.getElementById("topic-id")?.value;
  const data = {
    name: document.getElementById("topic-name")?.value.trim() || "",
    search_queries: document.getElementById("topic-queries")?.value.trim() || "",
    media_type: document.getElementById("topic-media-type")?.value || "any",
    telegram_channel_id: document.getElementById("topic-channel-id")?.value.trim() || "",
    system_prompt: document.getElementById("topic-system-prompt")?.value.trim() || "",
    prompt_template: document.getElementById("topic-prompt-template")?.value.trim() || "",
    is_active: document.getElementById("topic-is-active")?.checked ? 1 : 0
  };

  try {
    if (topicId) {
      await api.put(`/api/topics/${topicId}`, data);
      showToast("Топик обновлен!", "success");
    } else {
      await api.post("/api/topics", data);
      showToast("Топик успешно создан!", "success");
    }
    closeTopicModal();
    loadTopics();
  } catch (err) {
    showToast("Ошибка: " + err.message, "error");
  }
}

export async function deleteTopic(topicId) {
  let t = allTopicsList.find(x => String(x.id) === String(topicId));
  if (!t) {
    try {
      const fetched = await api.get(`/api/topics/${topicId}`);
      if (fetched && !fetched.error) t = fetched;
    } catch (e) {}
  }
  const topicName = t ? t.name : `топик #${topicId}`;

  const doDelete = async () => {
    try {
      await api.delete(`/api/topics/${topicId}`);
      showToast("Топик успешно удален", "info");
      loadTopics();
    } catch (err) {
      showToast("Ошибка удаления: " + err.message, "error");
    }
  };

  if (typeof window.showConfirmDialog === 'function') {
    window.showConfirmDialog({
      title: "🗑️ Удаление топика",
      message: `Вы действительно хотите удалить тематический топик <b>«${escapeHtml(topicName)}»</b>? Поисковые запросы по этой теме будут остановлены.`,
      confirmText: "Да, удалить",
      onConfirm: doDelete
    });
  } else if (confirm(`Вы действительно хотите удалить тематический топик «${topicName}»?`)) {
    await doDelete();
  }
}

window.loadTopics = loadTopics;
window.openTopicModal = openTopicModal;
window.closeTopicModal = closeTopicModal;
window.handleTopicSubmit = handleTopicSubmit;
window.deleteTopic = deleteTopic;
