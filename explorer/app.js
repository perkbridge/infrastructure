const RPC_URL = "/api/";
const REFRESH_MS = 4000;
let requestId = 0;
let lastBlockHeight = null;
let refreshTimer = null;
let lastStructuredSlot = null;

const TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA";
const TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb";
const PROGRAM_NAMES = {
  "11111111111111111111111111111111": "System Program",
  "Vote111111111111111111111111111111111111111": "Vote Program",
  "Stake11111111111111111111111111111111111111": "Stake Program",
  "ComputeBudget111111111111111111111111111111": "Compute Budget",
  [TOKEN_PROGRAM]: "SPL Token",
  [TOKEN_2022_PROGRAM]: "Token-2022",
  "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL": "Associated Token",
  "BPFLoaderUpgradeab1e11111111111111111111111": "Upgradeable Loader",
};

const $ = (selector) => document.querySelector(selector);
const compact = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 2 });
const number = new Intl.NumberFormat("en");

async function rpc(method, params = []) {
  const response = await fetch(RPC_URL, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: ++requestId, method, params }),
  });
  if (!response.ok) throw new Error(`RPC returned HTTP ${response.status}`);
  const payload = await response.json();
  if (payload.error) throw new Error(payload.error.message || "RPC request failed");
  return payload.result;
}

function short(value, head = 6, tail = 6) {
  if (!value) return "—";
  return value.length > head + tail + 3 ? `${value.slice(0, head)}…${value.slice(-tail)}` : value;
}

function age(timestamp) {
  if (!timestamp) return "just now";
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - timestamp));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return new Date(timestamp * 1000).toLocaleString();
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

function setConnection(online, label) {
  const pill = $("#connection-pill");
  pill.className = `connection-pill ${online ? "online" : "offline"}`;
  $("#connection-label").textContent = label;
}

async function loadOverview() {
  try {
    const [health, version, slot, height, epoch, txCount, votes] = await Promise.all([
      rpc("getHealth"), rpc("getVersion"), rpc("getSlot", [{ commitment: "confirmed" }]),
      rpc("getBlockHeight", [{ commitment: "confirmed" }]), rpc("getEpochInfo", [{ commitment: "confirmed" }]),
      rpc("getTransactionCount", [{ commitment: "confirmed" }]), rpc("getVoteAccounts"),
    ]);

    setConnection(health === "ok", health === "ok" ? "RPC online" : "RPC syncing");
    $("#hero-slot").textContent = number.format(slot);
    $("#block-height").textContent = number.format(height);
    $("#transaction-count").textContent = compact.format(txCount);
    $("#version-badge").textContent = `Agave ${version["solana-core"] || "4.1.2"}`;
    $("#epoch-label").textContent = `Epoch ${epoch.epoch}`;
    const progress = epoch.slotsInEpoch ? (epoch.slotIndex / epoch.slotsInEpoch) * 100 : 0;
    $("#epoch-progress").style.width = `${Math.max(1, progress)}%`;
    $("#epoch-detail").textContent = `${progress.toFixed(1)}% · ${number.format(epoch.slotIndex)} / ${number.format(epoch.slotsInEpoch)} slots`;
    $("#block-delta").textContent = lastBlockHeight === null ? "Live cluster head" : `+${Math.max(0, height - lastBlockHeight)} since refresh`;
    lastBlockHeight = height;
    renderValidators(votes);
    await loadBlocks(slot);
    if (lastStructuredSlot !== slot) {
      lastStructuredSlot = slot;
      loadStructuredData(slot);
    }
  } catch (error) {
    setConnection(false, "RPC unavailable");
    showToast(error.message);
  }
}

function programName(programId, fallback) {
  return PROGRAM_NAMES[programId] || fallback || `Program ${short(programId, 4, 4)}`;
}

async function loadStructuredData(latestSlot) {
  try {
    const slots = await rpc("getBlocks", [Math.max(0, latestSlot - 16), latestSlot, { commitment: "confirmed" }]);
    const blocks = await Promise.all(slots.slice(-6).reverse().map(async (slot) => {
      try {
        const value = await rpc("getBlock", [slot, { commitment: "confirmed", encoding: "jsonParsed", transactionDetails: "full", rewards: false, maxSupportedTransactionVersion: 0 }]);
        return { slot, ...value };
      } catch { return { slot, transactions: [] }; }
    }));
    const transactions = blocks.flatMap((block) => (block.transactions || []).map((tx) => ({ slot: block.slot, ...tx })));
    renderTransactions(transactions);
    renderPrograms(transactions);
  } catch (error) {
    $("#transactions-body").innerHTML = `<tr><td colspan="5"><div class="empty-state"><strong>Transactions unavailable</strong>${escapeHtml(error.message)}</div></td></tr>`;
  }
}

function transactionPrograms(tx) {
  const instructions = tx.transaction?.message?.instructions || [];
  const unique = new Map();
  instructions.forEach((instruction) => {
    const id = typeof instruction.programId === "string" ? instruction.programId : String(instruction.programId || "");
    if (id) unique.set(id, programName(id, instruction.program));
  });
  return [...unique.entries()].map(([id, name]) => ({ id, name }));
}

function renderTransactions(transactions) {
  $("#tx-total-badge").textContent = `${transactions.length} loaded`;
  $("#transactions-body").innerHTML = transactions.slice(0, 30).map((tx) => {
    const signature = tx.transaction?.signatures?.[0] || "";
    const programs = transactionPrograms(tx);
    return `<tr>
      <td><span class="address inspect-transaction" data-signature="${escapeHtml(signature)}">${short(signature, 10, 8)}</span></td>
      <td>${number.format(tx.slot)}</td>
      <td><div class="tx-programs">${programs.slice(0, 3).map((item) => `<span class="mini-chip">${escapeHtml(item.name)}</span>`).join("") || '<span class="mini-chip">Native</span>'}</div></td>
      <td>${((tx.meta?.fee || 0) / 1e9).toFixed(6)} SOL</td>
      <td><span class="${tx.meta?.err ? "result-fail" : "result-ok"}">${tx.meta?.err ? "Failed" : "Success"}</span></td>
    </tr>`;
  }).join("") || `<tr><td colspan="5"><div class="empty-state"><strong>No transactions yet</strong>Recent confirmed blocks are empty.</div></td></tr>`;
  document.querySelectorAll(".inspect-transaction").forEach((node) => node.addEventListener("click", () => inspectTransaction(node.dataset.signature)));
}

function renderPrograms(transactions) {
  const activity = new Map();
  transactions.forEach((tx) => transactionPrograms(tx).forEach(({ id, name }) => {
    const current = activity.get(id) || { id, name, calls: 0 };
    current.calls += 1;
    activity.set(id, current);
  }));
  const programs = [...activity.values()].sort((a, b) => b.calls - a.calls);
  $("#programs-grid").innerHTML = programs.map((program, index) => `<article class="program-card inspect-address" data-address="${escapeHtml(program.id)}">
    <div class="program-card-head"><span class="program-glyph ${index % 2 ? "violet" : ""}">${escapeHtml(program.name.split(/\s+/).map((part) => part[0]).join("").slice(0, 2))}</span><span class="program-calls">${program.calls} CALL${program.calls === 1 ? "" : "S"}</span></div>
    <div><h3>${escapeHtml(program.name)}</h3><code>${escapeHtml(short(program.id, 9, 8))}</code></div>
  </article>`).join("") || `<div class="program-card"><div class="empty-state"><strong>No invocations yet</strong>Programs appear after transactions land.</div></div>`;
  document.querySelectorAll("#programs-grid .inspect-address").forEach((node) => node.addEventListener("click", () => inspectAccount(node.dataset.address)));
}

async function loadTokenPortfolio(owner) {
  $("#token-results").innerHTML = `<div class="skeleton"></div><div class="skeleton"></div>`;
  try {
    const [classic, token2022] = await Promise.all([
      rpc("getTokenAccountsByOwner", [owner, { programId: TOKEN_PROGRAM }, { encoding: "jsonParsed", commitment: "confirmed" }]),
      rpc("getTokenAccountsByOwner", [owner, { programId: TOKEN_2022_PROGRAM }, { encoding: "jsonParsed", commitment: "confirmed" }]),
    ]);
    const accounts = [...(classic.value || []).map((item) => ({ ...item, standard: "SPL Token" })), ...(token2022.value || []).map((item) => ({ ...item, standard: "Token-2022" }))];
    $("#token-results").innerHTML = accounts.map((account) => {
      const info = account.account?.data?.parsed?.info || {};
      const amount = info.tokenAmount?.uiAmountString || "0";
      return `<div class="token-row"><div><strong class="address inspect-address" data-address="${escapeHtml(info.mint)}">${short(info.mint, 10, 8)}</strong><span>${escapeHtml(account.standard)} · account ${short(account.pubkey, 6, 5)}</span></div><div class="token-balance"><strong>${escapeHtml(amount)}</strong><span>${info.tokenAmount?.decimals || 0} decimals</span></div></div>`;
    }).join("") || `<div class="empty-state"><strong>No SPL holdings</strong>This owner has no Token or Token-2022 accounts.</div>`;
    document.querySelectorAll("#token-results .inspect-address").forEach((node) => node.addEventListener("click", () => inspectAccount(node.dataset.address)));
  } catch (error) {
    $("#token-results").innerHTML = `<div class="empty-state"><strong>Portfolio unavailable</strong>${escapeHtml(error.message)}</div>`;
  }
}

function renderValidators(votes) {
  const validators = [...(votes.current || []), ...(votes.delinquent || [])];
  const totalStake = validators.reduce((sum, item) => sum + item.activatedStake, 0);
  $("#active-stake").textContent = `${(totalStake / 1e9).toFixed(2)} SOL`;
  $("#validator-count").textContent = `${validators.length} voting validators`;
  $("#validators-body").innerHTML = validators.map((validator, index) => {
    const isCurrent = (votes.current || []).some((item) => item.nodePubkey === validator.nodePubkey);
    const share = totalStake ? (validator.activatedStake / totalStake) * 100 : 0;
    return `<tr>
      <td><div class="validator-name"><span class="validator-avatar">V${index}</span><span class="address inspect-address" data-address="${escapeHtml(validator.nodePubkey)}">${short(validator.nodePubkey, 7, 5)}</span></div></td>
      <td><span class="address inspect-address" data-address="${escapeHtml(validator.votePubkey)}">${short(validator.votePubkey, 7, 5)}</span></td>
      <td>${(validator.activatedStake / 1e9).toFixed(3)} SOL<div class="stake-bar"><i style="width:${share}%"></i></div></td>
      <td>${validator.lastVote || "—"}</td>
      <td><span class="status-chip ${isCurrent ? "" : "delinquent"}">${isCurrent ? "Active" : "Warming up"}</span></td>
    </tr>`;
  }).join("");
  document.querySelectorAll(".inspect-address").forEach((node) => node.addEventListener("click", () => inspectAccount(node.dataset.address)));
}

async function loadBlocks(latestSlot) {
  try {
    const start = Math.max(0, latestSlot - 24);
    const slots = await rpc("getBlocks", [start, latestSlot, { commitment: "confirmed" }]);
    const recent = slots.slice(-7).reverse();
    const blocks = await Promise.all(recent.map(async (slot) => {
      try {
        const block = await rpc("getBlock", [slot, { commitment: "confirmed", transactionDetails: "signatures", rewards: false, maxSupportedTransactionVersion: 0 }]);
        return { slot, ...block };
      } catch { return { slot }; }
    }));
    $("#blocks-list").classList.remove("loading-list");
    $("#blocks-list").innerHTML = blocks.map((block) => `<div class="block-row" data-slot="${block.slot}">
      <span class="block-slot">#${number.format(block.slot)}</span>
      <div class="block-hash"><strong>${escapeHtml(short(block.blockhash, 12, 10))}</strong><span>${block.signatures?.length || 0} transactions</span></div>
      <span class="block-time">${age(block.blockTime)}</span>
    </div>`).join("") || `<div class="empty-state"><strong>No blocks yet</strong>The cluster is warming up.</div>`;
    document.querySelectorAll(".block-row").forEach((row) => row.addEventListener("click", () => inspectBlock(Number(row.dataset.slot))));
  } catch (error) {
    $("#blocks-list").innerHTML = `<div class="empty-state"><strong>Blocks unavailable</strong>${escapeHtml(error.message)}</div>`;
  }
}

function openInspector(title, html) {
  $("#inspector-title").textContent = title;
  $("#inspector-content").innerHTML = html;
  $("#inspector-backdrop").hidden = false;
  $("#inspector").classList.add("open");
  $("#inspector").setAttribute("aria-hidden", "false");
}

function closeInspector() {
  $("#inspector").classList.remove("open");
  $("#inspector").setAttribute("aria-hidden", "true");
  setTimeout(() => { $("#inspector-backdrop").hidden = true; }, 280);
}

async function inspectBlock(slot) {
  openInspector(`Block ${number.format(slot)}`, `<div class="skeleton"></div><div class="skeleton"></div>`);
  try {
    const block = await rpc("getBlock", [slot, { commitment: "confirmed", transactionDetails: "signatures", rewards: true, maxSupportedTransactionVersion: 0 }]);
    openInspector(`Block ${number.format(slot)}`, `<div class="detail-grid">
      <div class="detail-item"><small>SLOT</small><strong>${number.format(slot)}</strong></div>
      <div class="detail-item"><small>TIME</small><strong>${block.blockTime ? new Date(block.blockTime * 1000).toLocaleString() : "—"}</strong></div>
      <div class="detail-item full"><small>BLOCKHASH</small><strong>${escapeHtml(block.blockhash)}</strong></div>
      <div class="detail-item"><small>TRANSACTIONS</small><strong>${block.signatures?.length || 0}</strong></div>
      <div class="detail-item"><small>REWARDS</small><strong>${block.rewards?.length || 0}</strong></div>
    </div><pre class="json-view">${escapeHtml(JSON.stringify(block, null, 2))}</pre>`);
  } catch (error) { showInspectorError(error); }
}

async function inspectAccount(address) {
  openInspector("Account", `<div class="skeleton"></div><div class="skeleton"></div>`);
  try {
    const [balance, info] = await Promise.all([rpc("getBalance", [address, { commitment: "confirmed" }]), rpc("getAccountInfo", [address, { encoding: "base64", commitment: "confirmed" }])]);
    openInspector("Account", `<div class="detail-grid">
      <div class="detail-item full"><small>ADDRESS</small><strong>${escapeHtml(address)}</strong></div>
      <div class="detail-item"><small>BALANCE</small><strong>${(balance.value / 1e9).toFixed(9)} SOL</strong></div>
      <div class="detail-item"><small>EXECUTABLE</small><strong>${info.value?.executable ? "Yes" : "No"}</strong></div>
      <div class="detail-item full"><small>OWNER</small><strong>${escapeHtml(info.value?.owner || "—")}</strong></div>
    </div><pre class="json-view">${escapeHtml(JSON.stringify(info.value, null, 2))}</pre>`);
  } catch (error) { showInspectorError(error); }
}

async function inspectTransaction(signature) {
  openInspector("Transaction", `<div class="skeleton"></div><div class="skeleton"></div>`);
  try {
    const tx = await rpc("getTransaction", [signature, { encoding: "jsonParsed", commitment: "confirmed", maxSupportedTransactionVersion: 0 }]);
    if (!tx) throw new Error("Transaction not found on this cluster");
    openInspector("Transaction", `<div class="detail-grid">
      <div class="detail-item full"><small>SIGNATURE</small><strong>${escapeHtml(signature)}</strong></div>
      <div class="detail-item"><small>SLOT</small><strong>${number.format(tx.slot)}</strong></div>
      <div class="detail-item"><small>RESULT</small><strong>${tx.meta?.err ? "Failed" : "Success"}</strong></div>
      <div class="detail-item"><small>FEE</small><strong>${((tx.meta?.fee || 0) / 1e9).toFixed(9)} SOL</strong></div>
      <div class="detail-item"><small>COMPUTE UNITS</small><strong>${number.format(tx.meta?.computeUnitsConsumed || 0)}</strong></div>
    </div><pre class="json-view">${escapeHtml(JSON.stringify(tx, null, 2))}</pre>`);
  } catch (error) { showInspectorError(error); }
}

function showInspectorError(error) {
  $("#inspector-content").innerHTML = `<div class="empty-state"><strong>Nothing found</strong>${escapeHtml(error.message)}</div>`;
}

async function handleSearch(event) {
  event.preventDefault();
  const query = $("#search-input").value.trim();
  if (!query) return;
  if (/^\d+$/.test(query)) return inspectBlock(Number(query));
  if (query.length >= 80) return inspectTransaction(query);
  if (query.length >= 32) return inspectAccount(query);
  showToast("Enter a slot, transaction signature, or account address");
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(toast.hideTimer);
  toast.hideTimer = setTimeout(() => toast.classList.remove("show"), 3500);
}

function scheduleRefresh() {
  clearInterval(refreshTimer);
  refreshTimer = setInterval(loadOverview, REFRESH_MS);
}

$("#search-form").addEventListener("submit", handleSearch);
document.querySelectorAll(".view-tab").forEach((tab) => tab.addEventListener("click", () => {
  document.querySelectorAll(".view-tab").forEach((item) => item.classList.toggle("active", item === tab));
  document.querySelectorAll(".view-section").forEach((section) => section.classList.toggle("active", section.id === `${tab.dataset.view}-view`));
  window.scrollTo({ top: 0, behavior: "smooth" });
}));
$("#token-owner-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const owner = $("#token-owner").value.trim();
  if (owner.length < 32) return showToast("Enter a valid owner address");
  loadTokenPortfolio(owner);
});
$("#refresh-button").addEventListener("click", loadOverview);
$("#inspector-close").addEventListener("click", closeInspector);
$("#inspector-backdrop").addEventListener("click", closeInspector);
document.addEventListener("keydown", (event) => {
  if (event.key === "/" && document.activeElement !== $("#search-input")) { event.preventDefault(); $("#search-input").focus(); }
  if (event.key === "Escape") closeInspector();
});

loadOverview();
scheduleRefresh();
