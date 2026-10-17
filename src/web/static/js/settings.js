/**
 * System Settings Module
 */
import { api } from './api.js';
import { showToast } from './toast.js';
import { loadTopics } from './topics.js';

export async function loadSettings() {
  try {
    const data = await api.get("/api/settings");
    if (!data) return;

    // Timezone
    const savedTimezone = data.app_timezone || "";
    const timezoneSelect = document.getElementById("setting-timezone");
    const timezoneOffset = document.getElementById("setting-timezone-offset");
    if (timezoneSelect && [...timezoneSelect.options].some(option => option.value === savedTimezone)) {
      timezoneSelect.value = savedTimezone;
      if (timezoneOffset) timezoneOffset.value = "";
    } else if (timezoneOffset) {
      timezoneOffset.value = savedTimezone;
      if (timezoneSelect) timezoneSelect.value = "";
    }

    // Parameters
    const setVal = (id, val) => {
      const el = document.getElementById(id);
      if (el && val !== undefined && val !== null) el.value = val;
    };
    const setCheck = (id, checked) => {
      const el = document.getElementById(id);
      if (el) el.checked = Boolean(checked);
    };

    setVal("setting-min-score", data.min_viral_score ?? 75.0);
    setVal("setting-super-score", data.super_viral_threshold ?? 100.0);
    setVal("setting-max-posts", data.max_posts_per_run ?? 1);
    setVal("setting-interval", data.scheduler_interval_minutes ?? 120);
    setVal("setting-channel", data.telegram_channel_id ?? "");
    setVal("setting-model", data.ai_model_name ?? "gemini-3.5-flash-lite, gemini-3.1-flash-lite");

    // Video Manager settings
    setVal("setting-video-ttl", data.video_ttl_days ?? 2);
    setVal("setting-video-quality", data.video_max_quality ?? 720);
    setVal("setting-video-size", data.video_max_size_mb ?? 50);

    // AI & Search
    setVal("setting-x-queries", data.x_search_queries ?? "");
    setVal("setting-x-relevance", data.x_relevance_prompt ?? "");
    setVal("setting-editor-sys-prompt", data.editor_system_prompt ?? "");
    setVal("setting-editor-template", data.editor_prompt_template ?? "");

    // API Keys
    setVal("setting-gemini-key", data.ai_api_key ?? "");
    setVal("setting-bot-token", data.telegram_bot_token ?? "");
    setVal("tw-login-user", data.twitter_username ?? "");
    setVal("tw-login-email", data.twitter_email ?? "");
    setVal("tw-login-proxy", data.twitter_proxy_url ?? "");

    document.querySelectorAll(".model-btn").forEach(btn => {
      btn.classList.toggle("active", (data.ai_model_name || '').includes(btn.dataset.model));
    });

    loadTopics();
  } catch (err) {
    console.error("Failed to load settings:", err);
  }
}

export function selectModel(modelName) {
  const el = document.getElementById("setting-model");
  if (el) el.value = modelName;
  document.querySelectorAll(".model-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.model === modelName);
  });
  showToast(`Выбрана модель: ${modelName}`, "info");
}

export async function saveSettings(e) {
  if (e) e.preventDefault();
  const form = document.getElementById("settings-form");
  if (!form) return;
  const formData = new FormData(form);
  const data = {};

  formData.forEach((value, key) => {
    data[key] = value;
  });

  // Timezone resolution
  const tzSel = document.getElementById("setting-timezone")?.value;
  const tzOff = document.getElementById("setting-timezone-offset")?.value;
  data.app_timezone = tzOff ? tzOff.trim() : (tzSel || "");

  try {
    await api.post("/api/settings", data);
    showToast("Настройки успешно сохранены!", "success");
  } catch (err) {
    showToast("Ошибка сохранения настроек: " + err.message, "error");
  }
}

window.loadSettings = loadSettings;
window.saveSettings = saveSettings;
window.selectModel = selectModel;
