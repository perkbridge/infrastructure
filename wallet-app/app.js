const REALM = "local-wallet";
const CLIENT_ID = "wallet-app";
const PUBLIC_ORIGIN = `${window.location.protocol}//localhost${window.location.port ? `:${window.location.port}` : ""}`;
const AUTH_BASE = `${PUBLIC_ORIGIN}/auth/realms/${REALM}/protocol/openid-connect`;
const REDIRECT_URI = `${PUBLIC_ORIGIN}/`;
const TOKEN_KEY = "local-wallet.tokens";

const $ = (selector) => document.querySelector(selector);
let wallet = null;
let walletCollection = [];
let searchTimer = null;

const queryTheme = new URLSearchParams(window.location.search).get("theme");
const initialTheme = ["light", "dark"].includes(queryTheme) ? queryTheme : (localStorage.getItem("perkbridge.theme") || "dark");
document.documentElement.dataset.theme = initialTheme;
localStorage.setItem("perkbridge.theme", initialTheme);

function renderTheme() {
  const theme = document.documentElement.dataset.theme;
  document.querySelectorAll(".theme-toggle").forEach((button) => { button.textContent = theme === "dark" ? "Light mode" : "Dark mode"; });
}

document.querySelectorAll(".theme-toggle").forEach((button) => button.addEventListener("click", () => {
  const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("perkbridge.theme", theme);
  renderTheme();
}));
renderTheme();

function base64url(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomString(length = 48) {
  return base64url(crypto.getRandomValues(new Uint8Array(length)));
}

async function beginAuth(register = false) {
  const verifier = randomString(64);
  const challenge = base64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  const state = randomString(24);
  sessionStorage.setItem("oauth.verifier", verifier);
  sessionStorage.setItem("oauth.state", state);
  const params = new URLSearchParams({ client_id: CLIENT_ID, redirect_uri: REDIRECT_URI, response_type: "code", scope: "openid profile email", state, code_challenge: challenge, code_challenge_method: "S256" });
  const endpoint = register ? "registrations" : "auth";
  window.location.assign(`${AUTH_BASE}/${endpoint}?${params}`);
}

async function tokenRequest(parameters) {
  const response = await fetch(`${AUTH_BASE}/token`, { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body: new URLSearchParams(parameters) });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error_description || "Authentication failed");
  sessionStorage.setItem(TOKEN_KEY, JSON.stringify(payload));
  return payload;
}

async function finishAuth() {
  const params = new URLSearchParams(window.location.search);
  const code = params.get("code");
  if (!code) return;
  if (params.get("state") !== sessionStorage.getItem("oauth.state")) throw new Error("Login state did not match");
  await tokenRequest({ grant_type: "authorization_code", client_id: CLIENT_ID, code, redirect_uri: REDIRECT_URI, code_verifier: sessionStorage.getItem("oauth.verifier") || "" });
  sessionStorage.removeItem("oauth.verifier"); sessionStorage.removeItem("oauth.state");
  history.replaceState({}, "", "/");
}

function tokens() {
  try { return JSON.parse(sessionStorage.getItem(TOKEN_KEY) || "null"); } catch { return null; }
}

function claims(token) {
  try { return JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/"))); } catch { return {}; }
}

async function accessToken() {
  let value = tokens();
  if (!value?.access_token) throw new Error("Please sign in again");
  if ((claims(value.access_token).exp || 0) < Date.now() / 1000 + 30) {
    if (!value.refresh_token) throw new Error("Your session expired");
    value = await tokenRequest({ grant_type: "refresh_token", client_id: CLIENT_ID, refresh_token: value.refresh_token });
  }
  return value.access_token;
}

async function api(path = "/me", options = {}) {
  const response = await fetch(`/api${path}`, { ...options, headers: { "content-type": "application/json", authorization: `Bearer ${await accessToken()}`, ...(options.headers || {}) } });
  const payload = await response.json().catch(() => ({}));
  if (response.status === 401) { sessionStorage.removeItem(TOKEN_KEY); showLanding(); throw new Error("Your session expired. Please sign in again."); }
  if (!response.ok) throw new Error(payload.error || `Wallet API returned HTTP ${response.status}`);
  return payload;
}

function short(value, head = 8, tail = 7) { return value && value.length > head + tail + 3 ? `${value.slice(0, head)}…${value.slice(-tail)}` : value || "—"; }
function initials(value) { return String(value || "?").slice(0, 2).toUpperCase(); }
function age(timestamp) { if (!timestamp) return "Recently"; const seconds = Math.max(0, Math.floor(Date.now() / 1000 - timestamp)); if (seconds < 60) return "Just now"; if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`; if (seconds < 86400) return `${Math.floor(seconds / 3600)} hr ago`; return new Date(timestamp * 1000).toLocaleDateString(); }

function showToast(message) { const toast = $("#toast"); toast.textContent = message; toast.classList.add("show"); clearTimeout(toast.timer); toast.timer = setTimeout(() => toast.classList.remove("show"), 3600); }
function showError(message) { const panel = $("#app-error"); panel.textContent = message; panel.hidden = !message; }
async function copyAddress() { if (!wallet) return; await navigator.clipboard.writeText(wallet.address); showToast("Wallet address copied"); }

function showLanding() { $("#landing").hidden = false; $("#dashboard").hidden = true; }

function showDashboard() {
  const identity = claims(tokens().access_token);
  const username = identity.preferred_username || "user";
  const avatar = initials(username);
  $("#landing").hidden = true; $("#dashboard").hidden = false;
  $("#identity-name").textContent = `@${username}`; $("#greeting-name").textContent = username;
  $("#profile-username").textContent = `@${username}`; $("#identity-avatar").textContent = avatar; $("#profile-avatar").textContent = avatar;
  const hour = new Date().getHours(); $("#greeting-time").textContent = hour < 12 ? "morning" : hour < 18 ? "afternoon" : "evening";
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]);
}

function formatSol(value) {
  return Number(value || 0).toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 9 });
}

function renderActivity(items) {
  $("#activity-list").innerHTML = (items || []).map((item) => {
    const failed = item.status === "failed";
    const timestamp = item.blockTime ? new Date(item.blockTime * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "Pending time";
    return `<div class="activity-row"><span class="activity-icon">${failed ? "!" : "⇄"}</span><div class="activity-copy"><strong>${escapeHtml(short(item.signature, 15, 11))}</strong><small>Slot ${Number(item.slot).toLocaleString()}${item.memo ? ` · ${escapeHtml(item.memo)}` : ""}</small></div><div class="activity-state"><span class="status-chip ${failed ? "failed" : ""}">${escapeHtml(item.status)}</span></div><time class="activity-time">${escapeHtml(timestamp)}<br>${escapeHtml(age(item.blockTime))}</time></div>`;
  }).join("") || `<div class="empty"><strong>No transactions yet</strong><span>Fund this wallet or receive a transfer to begin.</span></div>`;
}

function renderWalletList() {
  $("#wallet-count").textContent = walletCollection.length;
  $("#wallet-limit-count").textContent = walletCollection.length;
  $("#create-wallet").disabled = walletCollection.length >= 10;
  $("#create-wallet-top").disabled = walletCollection.length >= 10;
  $("#wallet-list").innerHTML = walletCollection.map((item, index) => `<button class="wallet-item ${wallet?.id === item.id ? "active" : ""}" type="button" data-wallet-id="${escapeHtml(item.id)}"><span class="wallet-item-icon">${index + 1}</span><span class="wallet-item-copy"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(short(item.address, 7, 5))}</small></span><span class="wallet-item-balance">${escapeHtml(formatSol(item.balance))} SOL</span></button>`).join("");
  document.querySelectorAll("[data-wallet-id]").forEach((button) => button.addEventListener("click", () => selectWallet(button.dataset.walletId)));
}

function activeWalletKey() {
  const identity = claims(tokens()?.access_token || "");
  return `perkbridge.activeWallet.${identity.sub || "user"}`;
}

async function loadWallets(preferredId) {
  showError("");
  try {
    const result = await api("/wallets");
    walletCollection = result.wallets || [];
    const savedId = preferredId || localStorage.getItem(activeWalletKey());
    const target = walletCollection.find((item) => item.id === savedId) || walletCollection[0];
    wallet = target || null;
    renderWalletList();
    if (target) await loadWallet(target.id);
  } catch (error) { showError(error.message); }
}

async function selectWallet(walletId) {
  if (!walletId || wallet?.id === walletId) return;
  wallet = walletCollection.find((item) => item.id === walletId) || null;
  renderWalletList();
  await loadWallet(walletId);
}

async function loadWallet(walletId = wallet?.id) {
  if (!walletId) return;
  showError("");
  $("#refresh-button").disabled = true;
  try {
    wallet = await api(`/wallets/${encodeURIComponent(walletId)}`);
    localStorage.setItem(activeWalletKey(), wallet.id);
    const listItem = walletCollection.find((item) => item.id === wallet.id);
    if (listItem) Object.assign(listItem, wallet);
    $("#wallet-name").textContent = wallet.name;
    $("#wallet-created").textContent = wallet.createdAt ? `Created ${new Date(wallet.createdAt * 1000).toLocaleDateString()}` : "Secure custodial keypair";
    $("#balance").textContent = formatSol(wallet.balance);
    $("#wallet-address").textContent = wallet.address;
    $("#transaction-wallet-label").textContent = `${wallet.name} · ${short(wallet.address, 8, 6)}`;
    $("#send-wallet-name").textContent = wallet.name;
    $("#send-wallet-balance").textContent = `${formatSol(wallet.balance)} SOL available`;
    $("#delete-wallet").disabled = walletCollection.length <= 1;
    $("#delete-wallet").title = walletCollection.length <= 1 ? "Create another wallet before deleting this one" : "Delete this wallet";
    renderWalletList();
    renderActivity(wallet.history);
  } catch (error) { showError(error.message); }
  finally { $("#refresh-button").disabled = false; }
}

function openDialog(modalSelector, backdropSelector, focusSelector) {
  $(backdropSelector).hidden = false;
  $(modalSelector).classList.add("open");
  $(modalSelector).setAttribute("aria-hidden", "false");
  if (focusSelector) setTimeout(() => $(focusSelector).focus(), 220);
}

function closeDialog(modalSelector, backdropSelector) {
  $(modalSelector).classList.remove("open");
  $(modalSelector).setAttribute("aria-hidden", "true");
  setTimeout(() => { $(backdropSelector).hidden = true; }, 220);
}

function openSend() { if (wallet) openDialog("#send-modal", "#send-backdrop", "#recipient"); }
function closeSend() { closeDialog("#send-modal", "#send-backdrop"); }
function openCreate() { openDialog("#create-modal", "#create-backdrop", "#wallet-name-input"); }
function closeCreate() { closeDialog("#create-modal", "#create-backdrop"); }
function openDelete() { if (wallet && walletCollection.length > 1) { $("#delete-confirmation").value = ""; openDialog("#delete-modal", "#delete-backdrop", "#delete-confirmation"); } }
function closeDelete() { closeDialog("#delete-modal", "#delete-backdrop"); }

async function createWallet(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $("#create-wallet-submit");
  button.disabled = true; button.textContent = "Creating secure wallet…";
  try {
    const created = await api("/wallets", { method: "POST", body: JSON.stringify({ name: $("#wallet-name-input").value.trim() }) });
    form.reset(); closeCreate(); showToast(`${created.name} created`); await loadWallets(created.id);
  } catch (error) { showToast(error.message); }
  finally { button.disabled = false; button.innerHTML = `Create wallet <span>→</span>`; }
}

async function deleteWallet(event) {
  event.preventDefault();
  if (!wallet) return;
  const button = $("#delete-wallet-submit");
  button.disabled = true; button.textContent = "Deleting…";
  try {
    const deletedId = wallet.id;
    await api(`/wallets/${encodeURIComponent(deletedId)}`, { method: "DELETE", body: JSON.stringify({ confirmation: $("#delete-confirmation").value.trim() }) });
    closeDelete(); localStorage.removeItem(activeWalletKey()); showToast("Wallet permanently deleted"); await loadWallets();
  } catch (error) { showToast(error.message); }
  finally { button.disabled = false; button.textContent = "Permanently delete wallet"; }
}

async function fund() {
  const button = $("#fund-button"); button.disabled = true;
  try { await api(`/wallets/${encodeURIComponent(wallet.id)}/airdrop`, { method: "POST", body: JSON.stringify({ sol: 10 }) }); showToast(`10 local SOL added to ${wallet.name}`); await loadWallet(); }
  catch (error) { showToast(error.message); } finally { button.disabled = false; }
}

async function send(event) {
  event.preventDefault(); const form = event.currentTarget; const button = $("#send-submit"); button.disabled = true; button.textContent = "Sending…";
  try {
    const result = await api(`/wallets/${encodeURIComponent(wallet.id)}/transfer`, { method: "POST", body: JSON.stringify({ recipient: $("#recipient").value.trim(), sol: $("#amount").value }) });
    showToast(result.recipientUsername ? `Sent to @${result.recipientUsername}` : `Transfer ${short(result.signature)}`);
    form.reset(); closeSend(); await loadWallet();
  } catch (error) { showToast(error.message); }
  finally { button.disabled = false; button.innerHTML = `Review &amp; send <span>→</span>`; }
}

async function searchUsers() {
  const value = $("#recipient").value.trim();
  if (!value.startsWith("@") || value.length < 3) { $("#user-suggestions").innerHTML = ""; return; }
  try { const result = await api(`/users?query=${encodeURIComponent(value)}`); $("#user-suggestions").innerHTML = result.users.map((user) => `<option value="@${user.username}">${short(user.address)}</option>`).join(""); } catch {}
}

async function logout() {
  const value = tokens(); sessionStorage.removeItem(TOKEN_KEY);
  const params = new URLSearchParams({ client_id: CLIENT_ID, post_logout_redirect_uri: REDIRECT_URI });
  if (value?.id_token) params.set("id_token_hint", value.id_token);
  window.location.assign(`${AUTH_BASE}/logout?${params}`);
}

$("#login-top").addEventListener("click", () => beginAuth());
$("#login-button").addEventListener("click", () => beginAuth());
$("#register-button").addEventListener("click", () => beginAuth(true));
$("#logout-button").addEventListener("click", logout);
$("#copy-address").addEventListener("click", copyAddress); $("#profile-copy").addEventListener("click", copyAddress);
$("#open-send").addEventListener("click", openSend); $("#close-send").addEventListener("click", closeSend); $("#send-backdrop").addEventListener("click", closeSend);
$("#create-wallet").addEventListener("click", openCreate); $("#create-wallet-top").addEventListener("click", openCreate); $("#close-create").addEventListener("click", closeCreate); $("#create-backdrop").addEventListener("click", closeCreate);
$("#delete-wallet").addEventListener("click", openDelete); $("#close-delete").addEventListener("click", closeDelete); $("#delete-backdrop").addEventListener("click", closeDelete);
$("#create-wallet-form").addEventListener("submit", createWallet); $("#delete-wallet-form").addEventListener("submit", deleteWallet);
$("#send-form").addEventListener("submit", send); $("#fund-button").addEventListener("click", fund); $("#refresh-button").addEventListener("click", () => loadWallet()); $("#history-refresh").addEventListener("click", () => loadWallet());
$("#recipient").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(searchUsers, 250); });
document.querySelectorAll('.sidebar a[href^="#"]').forEach((item) => item.addEventListener("click", () => { document.querySelectorAll('.sidebar a[href^="#"]').forEach((link) => link.classList.remove("active")); item.classList.add("active"); }));
document.addEventListener("keydown", (event) => { if (event.key === "Escape") { closeSend(); closeCreate(); closeDelete(); } });

(async () => {
  try { await finishAuth(); } catch (error) { showToast(error.message); sessionStorage.removeItem(TOKEN_KEY); }
  if (tokens()?.access_token) { showDashboard(); await loadWallets(); } else showLanding();
})();
