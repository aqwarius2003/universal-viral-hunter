/**
 * Video Player Module: Click-to-Play Lazy Facade & Single-Player Controller
 */
import { escapeHtml } from './toast.js';

let currentlyPlayingContainer = null;
let videoScrollObserver = null;

/**
 * Renders a lightweight HTML facade with zero video buffering.
 */
export function renderVideoFacade(videoData, fallbackImage = '') {
  if (!videoData) return '';

  const filename = videoData.local_path ? videoData.local_path.split(/[/\\]/).pop() : '';
  const vidSrc = filename ? `/videos/${filename}` : (videoData.video_url || '');
  if (!vidSrc) return '';

  const poster = videoData.thumbnail || fallbackImage || '';

  return `
    <div class="video-facade" 
         data-video-src="${escapeHtml(vidSrc)}" 
         data-poster="${escapeHtml(poster)}"
         onclick="window.activateVideoPlayer(this)"
         title="Нажмите, чтобы запустить видео">
      ${poster ? `
        <img class="video-facade-poster" 
             src="${escapeHtml(poster)}" 
             loading="lazy" 
             alt="Обложка видео" 
             onerror="this.style.opacity='0.2'">
      ` : `
        <div style="width:100%; height:100%; background: linear-gradient(135deg, #182533, #0e1621); display:flex; align-items:center; justify-content:center; color:var(--tg-text-muted); font-size:0.85rem;">
          ▶ Нажмите для просмотра
        </div>
      `}
      <div class="video-play-btn" aria-label="Воспроизвести">
        <svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor">
          <polygon points="6 4 20 12 6 20 6 4"></polygon>
        </svg>
      </div>
      <span class="video-badge">🎬 ВИДЕО</span>
    </div>
  `;
}

/**
 * Activates real HTML5 video on click and enforces single-player policy.
 */
export function activateVideoPlayer(facadeEl) {
  if (!facadeEl) return;
  const videoSrc = facadeEl.dataset.videoSrc;
  if (!videoSrc) return;

  // 1. Single-Player Policy: pause and reset previous player if another card was playing
  if (currentlyPlayingContainer && currentlyPlayingContainer !== facadeEl) {
    stopVideoPlayer(currentlyPlayingContainer);
  }

  // 2. Build live video player
  facadeEl.onclick = null;
  facadeEl.style.cursor = 'default';
  facadeEl.innerHTML = `
    <video controls autoplay playsinline class="video-player-active">
      <source src="${escapeHtml(videoSrc)}" type="video/mp4">
      Ваш браузер не поддерживает встроенное видео.
    </video>
  `;

  const video = facadeEl.querySelector('video');
  if (video) {
    video.play().catch(err => {
      console.warn('Autoplay prevented or video loading interrupted:', err);
    });

    // Observe this video for scroll auto-pause
    initScrollObserver();
    if (videoScrollObserver) {
      videoScrollObserver.observe(video);
    }
  }

  currentlyPlayingContainer = facadeEl;
}

/**
 * Stops video player and restores facade.
 */
export function stopVideoPlayer(containerEl) {
  if (!containerEl) return;
  const video = containerEl.querySelector('video');
  if (video) {
    video.pause();
    if (videoScrollObserver) videoScrollObserver.unobserve(video);
  }

  const videoSrc = containerEl.dataset.videoSrc || '';
  const poster = containerEl.dataset.poster || '';

  containerEl.innerHTML = `
    ${poster ? `<img class="video-facade-poster" src="${escapeHtml(poster)}" loading="lazy" alt="Обложка видео">` : ''}
    <div class="video-play-btn" aria-label="Воспроизвести">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor">
        <polygon points="6 4 20 12 6 20 6 4"></polygon>
      </svg>
    </div>
    <span class="video-badge">🎬 ВИДЕО</span>
  `;
  containerEl.onclick = () => activateVideoPlayer(containerEl);
  containerEl.style.cursor = 'pointer';

  if (currentlyPlayingContainer === containerEl) {
    currentlyPlayingContainer = null;
  }
}

/**
 * IntersectionObserver: auto-pauses playing video when scrolled out of viewport.
 */
function initScrollObserver() {
  if (videoScrollObserver || !('IntersectionObserver' in window)) return;

  videoScrollObserver = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      const video = entry.target;
      if (!entry.isIntersecting && !video.paused) {
        video.pause();
      }
    });
  }, { threshold: 0.25 });
}

window.activateVideoPlayer = activateVideoPlayer;
window.stopVideoPlayer = stopVideoPlayer;
