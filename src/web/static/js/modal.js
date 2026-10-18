/**
 * Post Edit Modal & AI Reprocessing Module
 */
import { api } from './api.js';
import { showToast, escapeHtml } from './toast.js';
import { normalizeHashtags } from './cards.js';
import { queueData, loadQueue } from './queue.js';

let currentEditingPost = null;

function formatDateTimeLocal(isoStr) {
  if (!isoStr) return "";
  const d = new Date(isoStr);
  if (isNaN(d.getTime())) return "";
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function updateAuditCounter() {
  const title = (document.getElementById("edit-title")?.value || "").trim();
  const summary = (document.getElementById("edit-summary")?.value || "").trim();
  const takeaways = (document.getElementById("edit-takeaways")?.value || "")
    .split("\n")
    .map(t => t.trim())
    .filter(Boolean);
  const hashtags = (document.getElementById("edit-hashtags")?.value || "").trim();

  const isMedia = currentEditingPost && ((currentEditingPost.media_urls && currentEditingPost.media_urls.length > 0) || Boolean(currentEditingPost.video_data));
  const maxLimit = isMedia ? 1024 : 4096;

  let text = `<b>${title}</b>\n\n${summary}`;
  if (takeaways.length > 0) {
    text += `\n\n📌 <b>Главное:</b>\n` + takeaways.map(t => `• ${t}`).join("\n");
  }
  if (hashtags) {
    text += `\n\n${hashtags}`;
  }

  const len = text.length;
  const counterEl = document.getElementById("edit-char-counter");
  const bannerEl = document.getElementById("edit-audit-banner");
  if (!counterEl || !bannerEl) return;

  if (len <= maxLimit) {
    counterEl.innerHTML = `✅ ${len} / ${maxLimit} симв. (в норме)`;
    bannerEl.style.background = "rgba(16, 185, 129, 0.1)";
    bannerEl.style.borderColor = "rgba(16, 185, 129, 0.3)";
    bannerEl.style.color = "#6ee7b7";
  } else {
    counterEl.innerHTML = `⚠️ ${len} / ${maxLimit} симв. (авто-сжатие при сохранении)`;
    bannerEl.style.background = "rgba(245, 158, 11, 0.15)";
    bannerEl.style.borderColor = "rgba(245, 158, 11, 0.4)";
    bannerEl.style.color = "#fcd34d";
  }
}

export function openEditModal(tweetId, focusSchedule = false) {
  currentEditingPost = queueData.find(p => p.tweet_id === tweetId);
  if (!currentEditingPost) return;

  const s = currentEditingPost.summary_data || {};
  document.getElementById("edit-tweet-id").value = tweetId;
  document.getElementById("edit-scheduled-at").value = formatDateTimeLocal(currentEditingPost.scheduled_at);
  document.getElementById("edit-title").value = s.title_ru || "";
  document.getElementById("edit-summary").value = s.summary_ru || currentEditingPost.text_raw || "";
  document.getElementById("edit-takeaways").value = (s.takeaways || []).join("\n");
  document.getElementById("edit-hashtags").value = normalizeHashtags(s.hashtags).join(" ");

  const aiInput = document.getElementById("edit-ai-instruction");
  if (aiInput) aiInput.value = "";

  ["edit-title", "edit-summary", "edit-takeaways", "edit-hashtags"].forEach(id => {
    const el = document.getElementById(id);
    if (el && !el._hasAuditListener) {
      el.addEventListener("input", updateAuditCounter);
      el._hasAuditListener = true;
    }
  });
  updateAuditCounter();

  // Images
  const mediaUrls = currentEditingPost.media_urls || [];
  const imageGroup = document.getElementById("image-selection-group");
  const thumbContainer = document.getElementById("image-thumbnails");

  if (mediaUrls.length > 0 && imageGroup && thumbContainer) {
    imageGroup.style.display = "block";
    const currentIdx = s.selected_image_index !== undefined ? s.selected_image_index : 0;
    thumbContainer.innerHTML = mediaUrls.map((url, i) => `
      <label style="cursor:pointer; position:relative;">
        <input type="radio" name="selected_image" value="${i}" ${i === currentIdx ? 'checked' : ''} style="position:absolute; top:4px; left:4px;">
        <img src="${escapeHtml(url)}" style="width:70px; height:70px; object-fit:cover; border-radius:6px; border:2px solid ${i === currentIdx ? 'var(--tg-primary)' : 'var(--tg-border)'}">
      </label>
    `).join("");
  } else if (imageGroup) {
    imageGroup.style.display = "none";
  }

  const modal = document.getElementById("edit-modal");
  if (modal) {
    modal.classList.add("active");
  }

  if (focusSchedule) {
    setTimeout(() => {
      const el = document.getElementById("edit-scheduled-at");
      if (el) el.focus();
    }, 60);
  }
}

export function closeEditModal() {
  const modal = document.getElementById("edit-modal");
  if (modal) modal.classList.remove("active");
  currentEditingPost = null;
}

export async function submitEditPost() {
  if (!currentEditingPost) return;
  const tweetId = document.getElementById("edit-tweet-id").value;
  const scheduledVal = document.getElementById("edit-scheduled-at").value;
  const scheduled_at = scheduledVal ? new Date(scheduledVal).toISOString() : null;
  const title_ru = document.getElementById("edit-title").value.trim();
  const summary_ru = document.getElementById("edit-summary").value.trim();
  const takeaways = document.getElementById("edit-takeaways").value.split("\n").map(t => t.trim()).filter(Boolean);
  const hashtags = normalizeHashtags(document.getElementById("edit-hashtags").value);

  const selectedImgRadio = document.querySelector('input[name="selected_image"]:checked');
  const selected_image_index = selectedImgRadio ? parseInt(selectedImgRadio.value) : null;

  try {
    await api.post(`/api/posts/${tweetId}/edit`, {
      title_ru,
      summary_ru,
      takeaways,
      hashtags,
      selected_image_index,
      scheduled_at,
    });

    showToast("Пост успешно сохранен!", "success");
    closeEditModal();
    loadQueue();
  } catch (err) {
    showToast("Ошибка сохранения: " + err.message, "error");
  }
}

export async function reprocessPostWithAI() {
  const tweetId = document.getElementById("edit-tweet-id").value;
  const instruction = (document.getElementById("edit-ai-instruction").value || "").trim();
  if (!instruction) {
    showToast("Введите указания для ИИ (например: 'будь милее', 'без жести')", "warning");
    return;
  }
  const btn = document.getElementById("btn-reprocess-ai");
  const origText = btn ? btn.innerHTML : '';
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '⏳ ИИ переделывает...';
  }

  try {
    const data = await api.post(`/api/posts/${tweetId}/reprocess`, { instruction });
    if (data && data.tweet) {
      showToast("✨ ИИ успешно переработал текст!", "success");
      const s = data.tweet.summary_data || data.tweet.ai_data || {};
      document.getElementById("edit-title").value = s.title_ru || "";
      document.getElementById("edit-summary").value = s.summary_ru || "";
      document.getElementById("edit-takeaways").value = (s.takeaways || []).join("\n");
      document.getElementById("edit-hashtags").value = normalizeHashtags(s.hashtags).join(" ");
      document.getElementById("edit-ai-instruction").value = "";
      updateAuditCounter();
      loadQueue();
    }
  } catch (err) {
    showToast("Ошибка ИИ: " + err.message, "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = origText;
    }
  }
}

let pendingConfirmCallback = null;

export function showConfirmDialog({ title, message, confirmText = 'Удалить', onConfirm }) {
  const modal = document.getElementById("confirm-modal");
  const titleEl = document.getElementById("confirm-modal-title");
  const msgEl = document.getElementById("confirm-modal-message");
  const btnConfirm = document.getElementById("btn-confirm-action");

  if (titleEl && title) titleEl.innerHTML = title;
  if (msgEl && message) msgEl.innerHTML = message;
  if (btnConfirm) {
    btnConfirm.innerText = confirmText;
    pendingConfirmCallback = onConfirm;
    btnConfirm.onclick = () => {
      const cb = pendingConfirmCallback;
      closeConfirmModal();
      if (typeof cb === 'function') {
        cb();
      }
    };
  }

  if (modal) modal.classList.add("active");
}

export function closeConfirmModal() {
  const modal = document.getElementById("confirm-modal");
  if (modal) modal.classList.remove("active");
  pendingConfirmCallback = null;
}

window.openEditModal = openEditModal;
window.closeEditModal = closeEditModal;
window.submitEditPost = submitEditPost;
window.reprocessPostWithAI = reprocessPostWithAI;
window.showConfirmDialog = showConfirmDialog;
window.closeConfirmModal = closeConfirmModal;
