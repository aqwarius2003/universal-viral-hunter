/**
 * Unified Card Renderer Module
 */
import { escapeHtml } from './toast.js';
import { renderVideoFacade } from './video-player.js';

export function normalizeHashtags(hashtags) {
  if (!hashtags) return [];
  if (Array.isArray(hashtags)) {
    return hashtags.map(h => String(h).trim()).filter(Boolean);
  }
  if (typeof hashtags === 'string') {
    return hashtags
      .split(/[\s,]+/)
      .map(h => h.trim())
      .filter(h => h.length > 0)
      .map(h => h.startsWith('#') ? h : `#${h}`);
  }
  return [];
}

export function formatDisplayDateTime(isoStr) {
  if (!isoStr) return "";
  const d = new Date(isoStr);
  if (isNaN(d.getTime())) return isoStr;
  return d.toLocaleString("ru-RU", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit"
  });
}

export function renderMetricsBarHtml(p, s) {
  const views = p.impressions_count || 0;
  const retweets = p.retweets_count || 0;
  const likes = p.likes_count || 0;
  const bookmarks = p.bookmarks_count || 0;
  const score = (p.viral_score !== undefined && p.viral_score !== null) ? Number(p.viral_score).toFixed(1) : "0.0";

  return `
    <div class="metrics-row">
      <span class="metric-item" title="Просмотры">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg>
        ${Number(views).toLocaleString('ru-RU')}
      </span>
      <span class="metric-item" title="Ретвиты">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="17 1 21 5 17 9"></polyline><path d="M3 11V9a4 4 0 0 1 4-4h14"></path><polyline points="7 23 3 19 7 15"></polyline><path d="M21 13v2a4 4 0 0 1-4 4H3"></path></svg>
        ${Number(retweets).toLocaleString('ru-RU')}
      </span>
      <span class="metric-item" title="Лайки">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"></path></svg>
        ${Number(likes).toLocaleString('ru-RU')}
      </span>
      <span class="metric-item" title="Закладки">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"></path></svg>
        ${Number(bookmarks).toLocaleString('ru-RU')}
      </span>
      <span class="metric-item metric-score" title="Viral Score">
        🔥 ${score}
      </span>
    </div>
  `;
}

/**
 * Main Card Template Generator
 */
export function renderPostCard(p, idx, total, isArchive = false) {
  const s = p.summary_data || {};
  const title = escapeHtml(s.title_ru || p.text_raw?.slice(0, 70) || "Без заголовка");
  const summary = escapeHtml(s.summary_ru || p.text_raw || "");
  const takeaways = (s.takeaways || []).map(t => `<li>${escapeHtml(t)}</li>`).join("");
  const hashtagsList = normalizeHashtags(s.hashtags);
  const hashtagsHtml = hashtagsList.map(h => `<span class="hashtag-tag">${escapeHtml(h)}</span>`).join("");
  
  const cleanHandle = (p.author_handle || "").replace(/^@/, "");
  const rawAuthorName = (p.author_name || cleanHandle || "Unknown").trim();
  const authorDisplay = cleanHandle && rawAuthorName.toLowerCase() === cleanHandle.toLowerCase()
    ? `@${escapeHtml(cleanHandle)}`
    : escapeHtml(rawAuthorName);
  
  const xProfileUrl = `https://x.com/${escapeHtml(cleanHandle)}`;
  const tweetUrl = escapeHtml(p.tweet_url || `https://x.com/i/web/status/${p.tweet_id}`);

  // Image resolution
  const mediaUrls = p.media_urls || [];
  const selectedIdx = s.selected_image_index;
  let photoUrl = null;
  if (selectedIdx !== null && selectedIdx !== undefined && selectedIdx >= 0 && selectedIdx < mediaUrls.length) {
    photoUrl = mediaUrls[selectedIdx];
  } else if (mediaUrls.length > 0) {
    photoUrl = mediaUrls[0];
  }

  // Header status badge
  let statusBadgeHtml = '';
  if (isArchive) {
    const formattedPostedDate = formatDisplayDateTime(p.posted_at) || formatDisplayDateTime(p.inserted_at);
    statusBadgeHtml = `<span class="status-badge status-posted">${formattedPostedDate ? `Опубликован: ${formattedPostedDate}` : "Опубликован"}</span>`;
  } else if (p.status === 'PENDING_APPROVAL') {
    statusBadgeHtml = `<span class="status-badge status-pending" onclick="window.openEditModal('${p.tweet_id}')" title="Ожидает согласования">⏱ На согласовании</span>`;
  } else if (p.scheduled_at) {
    const formattedScheduled = formatDisplayDateTime(p.scheduled_at);
    statusBadgeHtml = `<span class="status-badge status-scheduled" onclick="window.openEditModal('${p.tweet_id}', true)" title="Нажмите для переноса">${formattedScheduled}</span>`;
  } else {
    statusBadgeHtml = `<span class="status-badge status-processed" onclick="window.openEditModal('${p.tweet_id}', true)" title="Нажмите для планирования">Не запланирован</span>`;
  }

  // Media HTML & Hover Overlay
  let mediaHtml = '';
  const hasVideo = Boolean(p.video_data && (p.video_data.local_path || p.video_data.video_url));

  const hoverOverlayHtml = `
    <div class="card-hover-overlay">
      <div style="font-weight:700; margin-bottom:4px; color:#fff;">${title}</div>
      <div style="color:#cbd5e1; font-size:0.78rem; margin-bottom:6px;">${authorDisplay}</div>
      <div style="color:#e2e8f0; font-size:0.8rem; line-height:1.4;">${summary.slice(0, 160)}${summary.length > 160 ? '...' : ''}</div>
    </div>
  `;

  if (hasVideo) {
    mediaHtml = `
      <div class="card-media-wrapper">
        ${renderVideoFacade(p.video_data, photoUrl)}
        ${hoverOverlayHtml}
      </div>
    `;
  } else if (photoUrl) {
    mediaHtml = `
      <div class="card-media-wrapper">
        <img src="${escapeHtml(photoUrl)}" alt="Post image" class="tg-post-image" loading="lazy" onerror="this.style.display='none'">
        ${hoverOverlayHtml}
      </div>
    `;
  }

  // Author line
  const authorLineHtml = cleanHandle && cleanHandle.toLowerCase() !== "unknown"
    ? `<div class="author-line"><svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z"></path></svg><a href="${xProfileUrl}" target="_blank" rel="noopener">${authorDisplay}</a></div>`
    : `<div class="author-line"><span>${authorDisplay}</span></div>`;

  // Controls for Queue
  const controlsHtml = isArchive ? '' : `
    <div class="card-controls">
      <div class="reorder-group">
        <button class="btn btn-sm" onclick="window.movePost('${p.tweet_id}', -1)" ${idx === 0 ? 'disabled style="opacity:0.35"' : ''} title="Поднять выше">
          ▲
        </button>
        <button class="btn btn-sm" onclick="window.movePost('${p.tweet_id}', 1)" ${idx === total - 1 ? 'disabled style="opacity:0.35"' : ''} title="Опустить ниже">
          ▼
        </button>
        <button class="btn btn-sm" onclick="window.openEditModal('${p.tweet_id}')" title="Редактировать">
          ✏️ Правка
        </button>
        <button class="btn btn-sm" onclick="window.regenerateSinglePost('${p.tweet_id}', this)" title="Переделать через ИИ">
          ✨ ИИ
        </button>
      </div>

      <div style="display:flex; gap:6px;">
        <button class="btn btn-sm btn-danger" onclick="window.deletePost('${p.tweet_id}', event)" title="Отменить (Shift+клик для быстрого удаления)">
          ✕
        </button>
        <button class="btn btn-sm btn-success" onclick="window.publishNow('${p.tweet_id}')">
          🚀 В канал
        </button>
      </div>
    </div>
  `;

  return `
    <div class="tg-card" data-id="${p.tweet_id}">
      <div class="tg-card-header">
        <span class="order-badge">#${idx + 1} ${isArchive ? 'в архиве' : 'в очереди'}</span>
        ${statusBadgeHtml}
      </div>

      ${mediaHtml}

      <div class="tg-post-body">
        ${authorLineHtml}
        <div class="post-title">${title}</div>
        ${renderMetricsBarHtml(p, s)}
        
        <div class="post-summary-text">${summary}</div>
        ${takeaways ? `<ul class="takeaways-list">${takeaways}</ul>` : ''}
        ${hashtagsHtml ? `<div class="hashtags-row">${hashtagsHtml}</div>` : ''}

        <a href="${tweetUrl}" target="_blank" rel="noopener" class="author-line" style="margin-top:auto; font-size:0.75rem;">
          ↗ Читать оригинал в X
        </a>
      </div>

      ${controlsHtml}
    </div>
  `;
}
