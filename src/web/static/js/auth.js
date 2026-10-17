/**
 * Authentication Module (Login / Logout / RBAC)
 */
import { api } from './api.js';
import { showToast } from './toast.js';

export let currentUser = null;
let lockoutTimer = null;

export async function checkAuthAndInit(onSuccess) {
  try {
    // 1. Check if first-run setup is needed
    try {
      const setup = await api.get('/api/auth/setup_status');
      if (setup && setup.needs_setup) {
        openSetupModal();
        return;
      }
    } catch (e) {
      console.warn("Setup status check skipped:", e);
    }

    // 2. Check current session
    const me = await api.get('/api/auth/me');
    if (me && me.authenticated && me.user) {
      currentUser = me.user;
      updateUserUI();
      if (onSuccess) onSuccess();
      return;
    }

    // Not authenticated
    openLoginModal();
  } catch (err) {
    console.warn("Auth check error, presenting login modal:", err);
    openLoginModal();
  }
}

export function openLoginModal() {
  const modal = document.getElementById("login-modal");
  const errBox = document.getElementById("login-error-box");
  if (errBox) errBox.style.display = "none";
  if (modal) {
    modal.classList.add("active");
    setTimeout(() => {
      document.getElementById("login-username")?.focus();
    }, 100);
  }
}

export function closeLoginModal() {
  const modal = document.getElementById("login-modal");
  if (modal) modal.classList.remove("active");
}

export async function handleLoginSubmit(e) {
  if (e) e.preventDefault();
  const errBox = document.getElementById("login-error-box");
  if (errBox) errBox.style.display = "none";

  const btn = document.getElementById("btn-login-submit");
  if (btn) btn.disabled = true;

  const username = document.getElementById("login-username")?.value.trim();
  const password = document.getElementById("login-password")?.value;

  try {
    const res = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ username, password })
    });

    const data = await res.json();
    if (res.ok) {
      closeLoginModal();
      currentUser = data.user;
      updateUserUI();
      showToast(`Добро пожаловать, ${currentUser.username}!`, "success");
      window.location.reload();
    } else if (res.status === 429) {
      if (errBox) {
        errBox.textContent = data.error || "Слишком много попыток входа.";
        errBox.style.display = "block";
      }
      startLockoutCountdown(data.error);
    } else {
      if (errBox) {
        errBox.textContent = data.error || "Неверный логин или пароль";
        errBox.style.display = "block";
      }
    }
  } catch (err) {
    if (errBox) {
      errBox.textContent = "Ошибка сети: " + err.message;
      errBox.style.display = "block";
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

function startLockoutCountdown(errMsg) {
  const match = errMsg ? errMsg.match(/(\d+)\s*сек/) : null;
  let sec = match ? parseInt(match[1]) : 60;
  const errBox = document.getElementById("login-error-box");
  if (lockoutTimer) clearInterval(lockoutTimer);
  lockoutTimer = setInterval(() => {
    sec--;
    if (sec <= 0) {
      clearInterval(lockoutTimer);
      if (errBox) errBox.style.display = "none";
    } else {
      if (errBox) errBox.textContent = `Слишком много попыток. Подождите ${sec} сек.`;
    }
  }, 1000);
}

export async function handleLogout() {
  try {
    await api.post('/api/auth/logout', {});
  } catch (e) {}
  currentUser = null;
  updateUserUI();
  openLoginModal();
  showToast("Вы вышли из системы", "info");
}

export function updateUserUI() {
  const usernameEl = document.getElementById("header-username");
  const roleEl = document.getElementById("header-role-badge");
  const profileBadge = document.getElementById("user-profile-badge");
  const settingsTabBtn = document.getElementById("tab-btn-settings");

  if (!currentUser) {
    if (profileBadge) profileBadge.style.display = "none";
    return;
  }

  if (profileBadge) profileBadge.style.display = "inline-flex";
  if (usernameEl) usernameEl.textContent = currentUser.username;
  if (roleEl) {
    roleEl.textContent = currentUser.role === "admin" ? "Админ" : "Редактор";
    roleEl.className = `user-role-tag ${currentUser.role === "admin" ? "role-admin" : "role-editor"}`;
  }

  if (settingsTabBtn) {
    settingsTabBtn.style.display = currentUser.role === "editor" ? "none" : "flex";
  }
}

function openSetupModal() {
  const modal = document.getElementById("login-modal");
  if (modal) modal.classList.add("active");
}

window.openLoginModal = openLoginModal;
window.closeLoginModal = closeLoginModal;
window.handleLoginSubmit = handleLoginSubmit;
window.handleLogout = handleLogout;
