"use strict";

// The client restores its selected conversation and initial position itself.
if (typeof history !== "undefined") history.scrollRestoration = "manual";

// New test protocol: keep prior device/history records untouched in their old DB.
const DB_NAME = "sideword-test-client-p256-payload-v1";
const STORE_NAME = "device";
const IDENTITY_KEY = "identity";
const HISTORY_PREFIX = "history:";
const POLL_INTERVAL_MS = 4000;
const REQUEST_TIMEOUT_MS = 15000;
const encoder = new TextEncoder();
const decoder = new TextDecoder("utf-8", { fatal: true });

const elements = {
  activationForm: document.querySelector("#activation-form"),
  chatList: document.querySelector("#chat-list"),
  clientPanel: document.querySelector("#client-panel"),
  connectionState: document.querySelector("#connection-state"),
  connectionLabel: document.querySelector("#connection-label"),
  detailsButton: document.querySelector("#conversation-details-button"),
  detailsDialog: document.querySelector("#conversation-details"),
  deviceDialog: document.querySelector("#device-settings"),
  devicePublicId: document.querySelector("#device-public-id"),
  keyWarning: document.querySelector("#key-change-warning"),
  conversation: document.querySelector("#conversation"),
  conversationKind: document.querySelector("#conversation-kind"),
  conversationTitle: document.querySelector("#conversation-title"),
  displayName: document.querySelector("#display-name"),
  error: document.querySelector("#error-message"),
  identityLabel: document.querySelector("#identity-label"),
  inviteToken: document.querySelector("#invite-token"),
  loadingPanel: document.querySelector("#loading-panel"),
  roomPassword: document.querySelector("#join-password"),
  activationError: document.querySelector("#activation-error"),
  attachmentInput: document.querySelector("#attachment-input"),
  attachmentButton: document.querySelector("#attachment-button"),
  attachmentPreview: document.querySelector("#attachment-preview"),
  attachmentLabel: document.querySelector("#attachment-label"),
  attachmentSize: document.querySelector("#attachment-size"),
  attachmentCancel: document.querySelector("#attachment-cancel"),
  composerError: document.querySelector("#composer-error"),
  messageForm: document.querySelector("#message-form"),
  messageInput: document.querySelector("#message-input"),
  messageList: document.querySelector("#message-list"),
  messageDetails: document.querySelector("#message-details"),
  participantCount: document.querySelector("#participant-count"),
  participantKeys: document.querySelector("#participant-keys"),
  refreshButton: document.querySelector("#refresh-button"),
  resetButton: document.querySelector("#reset-button"),
  sendButton: document.querySelector("#send-button"),
  setupPanel: document.querySelector("#setup-panel"),
  status: document.querySelector("#status-message"),
  waitingNotice: document.querySelector("#waiting-notice"),
};

let selectedFile = null;
let sendingMessage = false;
let queuedSocketBytes = 0;
let queuedSocketEvents = 0;
const failedMessageRetries = new Map();
const downloadUrls = new Map();
const mediaPreviews = globalThis.SidewordMedia?.createManager(scheduleVisiblePreviews);
const shownPreviews = new Set();
const receivedMedia = new Map();
const mediaFailures = new Map();
const mediaUploadStates = new Map();
let mediaTimer = null;
let previewTimer = null;
let transferringMedia = false;
let transferController = null;
let identity = null;
let chats = [];
let selectedChatId = null;
let invitePending = false;
let synchronizing = false;
let socket = null;
let reconnectTimer = null;
let reconnectAttempts = 0;
let heartbeatTimer = null;
let socketAuthTimer = null;
let pageActive = true;
let accessDeadline = null;
let inboundQueue = Promise.resolve();
const messagesByChat = new Map();
const renderedMessages = new Map();
let renderedChatId;
const seenMessageIds = new Set();
const seenReceiptIds = new Set();
const processingMessageIds = new Set();
const failedMessageReasons = new Map();
const pendingReadsByChat = new Map();
const pendingDeliveries = new Map();
const pendingReceiptIds = new Map();
const messageMetadata = new Map();
const loadedHistoryKeys = new Set();
const deliveryReferences = new Map();
const acknowledgedDeliveries = new Set();
const acknowledgedReads = new Set();
const outgoingAttempts = new Map();
const visibleMessageNodes = new Set();
const viewTimers = new Map();
let openMessageDetails = null;
let messageDetailsTrigger = null;
let statusPollCursor = 0;
let messageProfileReady = false;
const participantNodes = new Map();
let participantChatId;
const presenceByChat = new Map();
let presenceUntil = 0;
let presenceTimer = null;
let followResizedMessages = true;

elements.messageList.addEventListener("scroll", () => {
  if (!conversationVisible()) return;
  const list = elements.messageList;
  followResizedMessages = list.scrollHeight - list.clientHeight - list.scrollTop <= 32;
  list.setAttribute("data-follow-latest", String(followResizedMessages));
  refreshVisibleReads();
  scheduleVisiblePreviews();
});
let messageResizeObserver = null;
if (typeof ResizeObserver === "function") {
  messageResizeObserver = new ResizeObserver(() => {
    if (conversationVisible() && followResizedMessages) {
      elements.messageList.scrollTop = elements.messageList.scrollHeight;
    }
    refreshVisibleReads();
    scheduleVisiblePreviews();
  });
  messageResizeObserver.observe(elements.messageList);
}

// Dynamic viewport units alone do not account for every mobile keyboard.
function updateWorkspaceViewport() {
  const viewport = window.visualViewport;
  positionMessageDetails();
  if (!viewport || viewport.scale !== 1) return;
  document.documentElement.style.setProperty("--visual-viewport-height", `${viewport.height}px`);
}
window.visualViewport?.addEventListener("resize", updateWorkspaceViewport);
window.visualViewport?.addEventListener("scroll", positionMessageDetails);
updateWorkspaceViewport();

// Native modal dialogs provide focus containment and Escape handling.
function bindDialog(dialog, openButton, closeButton) {
  closeOnBackdrop(dialog);
  openButton.addEventListener("click", () => {
    if (!dialog.open) dialog.showModal();
    refreshVisibleReads();
  });
  closeButton.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => {
    const target = openButton === elements.detailsButton && elements.clientPanel.hidden
      ? elements.inviteToken : openButton;
    target.focus({ preventScroll: true });
  });
}

function closeOnBackdrop(dialog) {
  dialog.addEventListener("click", event => {
    if (event.target !== dialog) return;
    const rect = dialog.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right
        || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
  });
}
bindDialog(elements.detailsDialog, elements.detailsButton,
  document.querySelector("#close-conversation-details"));
bindDialog(elements.deviceDialog, document.querySelector("#device-settings-button"),
  document.querySelector("#close-device-settings"));
document.querySelector("#close-message-details").addEventListener("click", () => elements.messageDetails.close());
elements.messageDetails.addEventListener("close", () => {
  openMessageDetails = null;
  const fallback = elements.messageInput.disabled ? elements.conversationTitle : elements.messageInput;
  const target = messageDetailsTrigger?.isConnected === false ? fallback : messageDetailsTrigger;
  target?.focus({ preventScroll: true });
  messageDetailsTrigger = null;
  refreshVisibleReads();
});
closeOnBackdrop(elements.messageDetails);
elements.messageDetails.addEventListener("toggle", () => positionMessageDetails(), true);
elements.detailsDialog.addEventListener("close", refreshVisibleReads);
elements.deviceDialog.addEventListener("close", refreshVisibleReads);

function mobileWorkspace() {
  return Boolean(window.matchMedia?.("(max-width: 760px)").matches);
}

function conversationVisible() {
  return !elements.clientPanel.hidden
    && (!mobileWorkspace() || elements.clientPanel.dataset.mobileView === "conversation");
}

function rememberChatView(view, chatId = selectedChatId) {
  if (!mobileWorkspace() || !identity?.publicId || !globalThis.history?.replaceState) return;
  try {
    // Per-tab navigation only: never copy identity keys, credentials or messages.
    history.replaceState({ ...history.state,
      sidewordChatView: { publicId: identity.publicId, chatId, view } }, "");
  } catch { /* Navigation remains usable if the browser refuses a state save. */ }
}

function restoreChatView() {
  selectedChatId = identity?.activeInviteChatId || null;
  if (!mobileWorkspace() || !identity?.publicId) return;
  const saved = globalThis.history?.state?.sidewordChatView;
  const validId = saved?.chatId === null || (Number.isSafeInteger(saved?.chatId) && saved.chatId > 0);
  const validView = saved?.view === "chats" || (saved?.view === "conversation" && saved.chatId !== null);
  if (saved?.publicId === identity?.publicId && validId && validView) {
    selectedChatId = saved.chatId;
    elements.clientPanel.setAttribute("data-mobile-view", saved.view);
  } else if (selectedChatId && identity?.token) {
    elements.clientPanel.setAttribute("data-mobile-view", "conversation");
  }
}

document.querySelector("#back-to-chats").addEventListener("click", () => {
  elements.clientPanel.dataset.mobileView = "chats";
  refreshVisibleReads();
  rememberChatView("chats");
  (elements.chatList.querySelector(".active") || elements.refreshButton).focus({ preventScroll: true });
});

function publicDetail(label, value, feedback) {
  const term = document.createElement("dt");
  term.textContent = label;
  const description = document.createElement("dd");
  const code = document.createElement("code");
  code.textContent = value;
  code.tabIndex = 0;
  const copy = document.createElement("button");
  copy.type = "button";
  copy.className = "button subtle";
  copy.textContent = `Copy ${label.toLowerCase()}`;
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(code.textContent);
      feedback.textContent = `${label} copied.`;
    } catch {
      // No fallback upload or hidden form: leave the complete value selectable.
      code.focus({ preventScroll: true });
      const range = document.createRange();
      range.selectNodeContents(code);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      feedback.textContent = "Copy unavailable. The full value is selected; copy it manually.";
    }
    feedback.hidden = false;
  });
  description.append(code, copy);
  return { term, description, code, copy };
}

function participantPresence(chatId, publicId) {
  const snapshot = presenceByChat.get(chatId);
  if (!snapshot || performance.now() >= presenceUntil || !snapshot.members.has(publicId)) return "unknown";
  return snapshot.online.has(publicId) ? "online" : "offline";
}

function renderPresence() {
  const chat = chats.find(item => item.id === selectedChatId);
  if (!chat) {
    elements.participantCount.textContent = "";
    return;
  }
  const states = chat.participants.map(peer => participantPresence(chat.id, peer.public_id));
  const known = states.length > 0 && !states.includes("unknown")
    && presenceByChat.get(chat.id)?.members.size === states.length;
  elements.participantCount.textContent = known
    ? `${states.filter(state => state === "online").length} of ${states.length} online`
    : `${states.length} participant${states.length === 1 ? "" : "s"} · Presence unknown`;
  for (const peer of chat.participants) {
    const item = participantNodes.get(peer.public_id);
    if (!item) continue;
    const state = participantPresence(chat.id, peer.public_id);
    item.presence.className = `participant-presence ${state}`;
    item.presence.textContent = state === "unknown" ? "Presence unknown" : state === "online" ? "Online" : "Offline";
  }
}

function clearPresence() {
  presenceByChat.clear();
  presenceUntil = 0;
  if (presenceTimer !== null) window.clearTimeout(presenceTimer);
  presenceTimer = null;
  renderPresence();
}

function receivePresence(payload) {
  // A complete snapshot replaces all prior state; never persist activity.
  clearPresence();
  if (navigator.onLine === false) return;
  if (!Number.isFinite(payload.valid_for_ms) || payload.valid_for_ms <= 0
      || payload.valid_for_ms > 35000 || !Array.isArray(payload.chats)) return;
  for (const snapshot of payload.chats) {
    if (!Number.isInteger(snapshot.chat_id) || !Array.isArray(snapshot.online)
        || !Array.isArray(snapshot.participants)
        || !snapshot.participants.every(id => typeof id === "string")
        || !snapshot.online.every(id => typeof id === "string" && snapshot.participants.includes(id))) {
      clearPresence();
      return;
    }
    presenceByChat.set(snapshot.chat_id, {
      online: new Set(snapshot.online), members: new Set(snapshot.participants),
    });
  }
  presenceUntil = performance.now() + payload.valid_for_ms;
  presenceTimer = window.setTimeout(clearPresence, payload.valid_for_ms);
  renderPresence();
}

function renderParticipantDetails(chat) {
  if (participantChatId !== chat?.id) {
    elements.participantKeys.replaceChildren();
    participantNodes.clear();
    participantChatId = chat?.id;
    document.querySelector("#details-copy-status").hidden = true;
  }
  document.querySelector("#details-conversation-title").textContent = chat ? chatName(chat) : "";
  const ids = new Set(chat?.participants.map(peer => peer.public_id) || []);
  for (const [id, item] of participantNodes) {
    if (!ids.has(id)) { item.node.remove(); participantNodes.delete(id); }
  }
  for (const [index, peer] of (chat?.participants || []).entries()) {
    let item = participantNodes.get(peer.public_id);
    if (!item) {
      const node = document.createElement("details");
      node.className = "participant";
      const summary = document.createElement("summary");
      const name = document.createElement("span");
      name.className = "participant-name";
      const publicId = document.createElement("span");
      publicId.className = "participant-identity";
      publicId.textContent = peer.public_id;
      const state = document.createElement("span");
      const presence = document.createElement("span");
      summary.append(name, presence, publicId, state);
      const values = document.createElement("dl");
      values.className = "technical-details";
      const feedback = document.querySelector("#details-copy-status");
      const id = publicDetail("Public ID", peer.public_id, feedback);
      const fingerprint = publicDetail("Fingerprint", "", feedback);
      values.append(id.term, id.description, fingerprint.term, fingerprint.description);
      node.append(summary, values);
      item = { node, name, state, fingerprint, presence };
      participantNodes.set(peer.public_id, item);
    }
    const own = peer.public_id === identity?.publicId;
    item.name.textContent = `${peer.display_name?.trim() || "Unnamed participant"}${own ? " (this device)" : ""}`;
    item.state.textContent = peer.key_changed ? "KEY CHANGED; blocked"
      : !peer.local_fingerprint ? "Unchecked key"
      : own ? "This device key" : "Locally pinned · identity not verified";
    item.state.className = `key-state${peer.key_changed ? " changed" : ""}`;
    item.fingerprint.code.textContent = peer.local_fingerprint || "not checked";
    item.fingerprint.copy.disabled = !peer.local_fingerprint;
    if (elements.participantKeys.children[index] !== item.node) {
      elements.participantKeys.insertBefore(item.node, elements.participantKeys.children[index] || null);
    }
  }
  renderPresence();
}

// Only locally written explanations may reach the UI; request bodies and raw
// browser/server exception text can contain credentials or message data.
class ClientError extends Error {}

class DeviceStorageError extends ClientError {
  constructor(message = "The browser could not read your saved chat keys from site storage. The saved device record is unreadable. Keep existing device data; do not use Reset device.") {
    super(message);
    this.name = "DeviceStorageError";
  }
}

function storageFailure(error, explanation) {
  if (error instanceof DOMException && ["QuotaExceededError", "SecurityError"].includes(error.name)) {
    return new ClientError(errorMessage(error));
  }
  return new ClientError(explanation);
}

function unreadableIdentity(value) {
  return value === null || Boolean(value && (
    (Object.hasOwn(value, "privateKey") && !value.privateKey)
    || (Object.hasOwn(value, "storageKey") && !value.storageKey)
  ));
}

function bytesToBase64(bytes) {
  return SidewordProtocol.bytesToBase64(bytes);
}

function openDatabase() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(STORE_NAME);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(storageFailure(request.error, "The browser could not open local chat storage."));
  });
}

async function readIdentity() {
  const database = await openDatabase();
  return new Promise((resolve, reject) => {
    const transaction = database.transaction(STORE_NAME, "readonly");
    const request = transaction.objectStore(STORE_NAME).get(IDENTITY_KEY);
    let value;
    request.onsuccess = () => { value = request.result; };
    transaction.oncomplete = () => {
      database.close();
      // Missing records return undefined. WebKit can return null when it
      // cannot deserialize a saved CryptoKey; preserve that record.
      if (unreadableIdentity(value)) reject(new DeviceStorageError());
      else resolve(value || null);
    };
    transaction.onerror = transaction.onabort = () => {
      database.close();
      reject(storageFailure(transaction.error || request.error, "The browser could not finish reading your saved device data. The saved login was not loaded."));
    };
  });
}

async function writeIdentity(value, updateSession = false) {
  const database = await openDatabase();
  return new Promise((resolve, reject) => {
    const transaction = database.transaction(STORE_NAME, "readwrite");
    const store = transaction.objectStore(STORE_NAME);
    const current = store.get(IDENTITY_KEY);
    let failure;
    current.onsuccess = () => {
      if (unreadableIdentity(current.result)) {
        failure = new DeviceStorageError();
        transaction.abort();
        return;
      }
      // Routine profile/history updates must not overwrite a newer rotation
      // committed by another tab while a network request was in flight.
      if (!updateSession && current.result && current.result.publicId === value.publicId) {
        for (const field of ["token", "suspendedToken", "refreshCredential", "sessionId",
          "tokenExpiresAt", "sessionExpiresAt", "pendingRefreshCredential",
          "activeInviteToken", "activeInviteChatId"]) {
          if (Object.hasOwn(current.result, field)) value[field] = current.result[field];
          else delete value[field];
        }
      }
      store.put(value, IDENTITY_KEY);
    };
    transaction.oncomplete = () => {
      database.close();
      resolve();
    };
    transaction.onerror = transaction.onabort = () => {
      database.close();
      reject(failure || storageFailure(transaction.error, "The browser could not save your device keys or login in local storage."));
    };
  });
}

async function clearIdentity() {
  stopMediaTransfers();
  const database = await openDatabase();
  return new Promise((resolve, reject) => {
    const transaction = database.transaction(STORE_NAME, "readwrite");
    transaction.objectStore(STORE_NAME).clear();
    transaction.oncomplete = () => {
      database.close();
      resolve();
    };
    transaction.onerror = () => reject(storageFailure(transaction.error, "The browser could not remove local device data."));
  });
}

async function generateIdentity() {
  const pair = await crypto.subtle.generateKey(
    { name: "ECDH", namedCurve: "P-256" }, false, ["deriveBits"],
  );
  const publicKey = new Uint8Array(await crypto.subtle.exportKey("raw", pair.publicKey));
  const privateKey = pair.privateKey;
  const storageKey = await crypto.subtle.generateKey(
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt", "decrypt"],
  );
  return {
    privateKey,
    publicKey: bytesToBase64(publicKey),
    storageKey,
    token: null,
    publicId: null,
  };
}

async function ensureStorageKey() {
  if (!identity || identity.storageKey) return;
  identity.storageKey = await crypto.subtle.generateKey(
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt", "decrypt"],
  );
  await writeIdentity(identity);
}

async function verifyIdentityPersistence() {
  const stored = await readIdentity();
  if (!stored?.privateKey || !stored.storageKey) throw new DeviceStorageError();
  if (stored.publicId !== identity.publicId || stored.publicKey !== identity.publicKey) {
    throw new ClientError("The device saved in this browser no longer matches the device this tab loaded. Its participant ID or encryption key changed. Chat is paused to avoid using the wrong identity.");
  }
}

function historyKey(chatId, entryId) {
  return `${HISTORY_PREFIX}${identity.publicId}:${chatId}:${entryId}`;
}

async function putDatabaseValue(key, value, removeKey = null, allowed = null) {
  const database = await openDatabase();
  if (allowed && !allowed()) { database.close(); return false; }
  return new Promise((resolve, reject) => {
    const transaction = database.transaction(STORE_NAME, "readwrite");
    const store = transaction.objectStore(STORE_NAME);
    if (removeKey) {
      // Atomic handoff: no observable child exists while its parent can recreate it.
      const request = store.get(key);
      request.onsuccess = () => {
        if (request.result === undefined) store.put(value, key);
        store.delete(removeKey);
      };
    } else store.put(value, key);
    transaction.oncomplete = () => {
      database.close();
      resolve();
    };
    transaction.onabort = transaction.onerror = () => {
      database.close();
      reject(storageFailure(transaction.error, "The browser could not save local chat history. The message has not been confirmed as saved."));
    };
  });
}

async function observePeerKey(peer) {
  const fingerprint = await SidewordProtocol.publicKeyFingerprint(peer.public_key);
  if (peer.public_id === identity.publicId) {
    const ownFingerprint = await SidewordProtocol.publicKeyFingerprint(identity.publicKey);
    return { ...peer, local_fingerprint: ownFingerprint, key_changed: ownFingerprint !== fingerprint };
  }
  const key = `peer:${identity.publicKey}:${peer.public_id}`;
  const database = await openDatabase();
  const pinned = await new Promise((resolve, reject) => {
    // Atomic read/check/insert: another tab must not overwrite a first-use pin.
    const transaction = database.transaction(STORE_NAME, "readwrite");
    const store = transaction.objectStore(STORE_NAME);
    const request = store.get(key);
    let value;
    request.onsuccess = () => {
      value = request.result;
      if (value === undefined) {
        value = fingerprint;
        store.put(value, key);
      }
    };
    transaction.oncomplete = () => { database.close(); resolve(value); };
    transaction.onabort = transaction.onerror = () => {
      database.close();
      reject(storageFailure(transaction.error, "The browser could not save a participant's key fingerprint. Messages are blocked because key changes cannot be checked safely."));
    };
  });
  return { ...peer, local_fingerprint: fingerprint, key_changed: pinned !== fingerprint };
}

async function assertTrustedPeer(peer) {
  if ((await observePeerKey(peer)).key_changed) {
    throw new ClientError("A participant's encryption key changed and no longer matches the fingerprint saved on this device. Sending and decryption are blocked. Keep this device's history.");
  }
}

async function readHistoryRecords(recordPrefix = null) {
  if (!identity?.publicId) return [];
  const prefix = recordPrefix || `${HISTORY_PREFIX}${identity.publicId}:`;
  const database = await openDatabase();
  return new Promise((resolve, reject) => {
    const records = [];
    const transaction = database.transaction(STORE_NAME, "readonly");
    const request = transaction.objectStore(STORE_NAME).openCursor();
    request.onsuccess = () => {
      const cursor = request.result;
      if (!cursor) return;
      if (typeof cursor.key === "string" && cursor.key.startsWith(prefix)) {
        records.push({ key: cursor.key, value: cursor.value });
      }
      cursor.continue();
    };
    request.onerror = () => reject(storageFailure(request.error, "The browser could not read saved chat history or pending messages."));
    transaction.oncomplete = () => {
      database.close();
      resolve(records);
    };
  });
}

async function persistHistoryEntry(chatId, entry, allowed = null) {
  if (!identity?.storageKey || !identity.publicId || !entry.id) {
    throw new ClientError("Local history storage is unavailable: this device's history key or identity is missing. Message retained for retry.");
  }
  const key = historyKey(chatId, entry.id);
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv, additionalData: encoder.encode(key) },
    identity.storageKey,
    encoder.encode(JSON.stringify(entry)),
  );
  return putDatabaseValue(key, { iv, ciphertext }, null, allowed);
}

async function loadStoredHistory() {
  if (!identity?.storageKey || !identity.publicId) return;
  if (!messagesByChat.size) { loadedHistoryKeys.clear(); receivedMedia.clear(); }
  const records = await readHistoryRecords();
  for (const record of records) {
    if (loadedHistoryKeys.has(record.key)) continue;
    try {
      const plaintext = await crypto.subtle.decrypt(
        {
          name: "AES-GCM",
          iv: record.value.iv,
          additionalData: encoder.encode(record.key),
        },
        identity.storageKey,
        record.value.ciphertext,
      );
      const parts = record.key.split(":");
      const chatId = Number(parts[2]);
      const entry = JSON.parse(decoder.decode(plaintext));
      if (entry.attachment) SidewordProtocol.validatePayload(entry.attachment);
      await appendMessage(chatId, entry, false);
      loadedHistoryKeys.add(record.key);
    } catch {
      // Ignore corrupted local records; authenticated decryption prevents use.
    }
  }
  for (const entries of messagesByChat.values()) {
    entries.sort(compareHistoryEntries);
  }
}

function showToast(message, isError = false) {
  const target = isError ? elements.error : elements.status;
  const other = isError ? elements.status : elements.error;
  other.hidden = true;
  target.textContent = message;
  target.hidden = false;
  // Keep the same announcement visible when a modal makes the workspace inert.
  const dialogPrefix = elements.deviceDialog.open ? "device" : elements.detailsDialog.open ? "details" : null;
  const dialogTarget = dialogPrefix ? document.querySelector(`#${dialogPrefix}-${isError ? "error" : "status"}`) : null;
  if (dialogTarget) {
    document.querySelector(`#${dialogPrefix}-${isError ? "status" : "error"}`).hidden = true;
    dialogTarget.textContent = message;
    dialogTarget.hidden = false;
  }
  window.setTimeout(() => {
    target.hidden = true;
    if (dialogTarget) dialogTarget.hidden = true;
  }, isError ? 7000 : 3500);
}

function errorMessage(error) {
  if (error instanceof ClientError || error instanceof SidewordProtocol.ProtocolError) {
    return error.message;
  }
  if (error instanceof DOMException && error.name === "QuotaExceededError") {
    return "The browser has no space available for saved chat data. Device data has not been reset.";
  }
  if (error instanceof DOMException && error.name === "SecurityError") {
    return "The browser blocked access to local storage or encryption features.";
  }
  if (error instanceof DOMException && error.name === "NotSupportedError") {
    return "This browser does not support the P-256 encryption required by this client.";
  }
  if (error instanceof DOMException && error.name === "OperationError") {
    return "The browser could not encrypt or decrypt this data with the available keys.";
  }
  return "An unexpected client error prevented this operation.";
}

function requestError(status, detail, context = "Chat request failed") {
  const explanations = {
    "link not found": "This invite does not exist on this service.",
    "link expired": "This invite has expired.",
    "link revoked": "The administrator disabled this invite.",
    "conversation closed": "This conversation is closed. Your saved history and pending messages are kept; other conversations remain available.",
    "room sealed; reconnect with your saved session": "This room is full or closed to new devices. Joining again cannot recover an existing participant slot.",
    "This room has an invalid P-256 participant key. Create a new invite.": "This room contains an invalid participant encryption key and cannot accept devices.",
    "The room phrase or password is incorrect.": "The room password does not match, or a required password was left empty.",
    "Too many failed attempts. Try again later.": "Password checks for this room are temporarily blocked after too many failed attempts.",
    "public_key mismatch with existing session": "Your saved login belongs to a different encryption key than this device is using.",
    "public_key mismatch with existing participant": "The saved room participant uses a different encryption key than this device.",
    "This device has joined. Use its saved session or resume credential.": "This device already claimed a room slot, but its saved login or recovery credential is missing.",
    "personal chat has no peer yet": "The other participant has not joined this room yet.",
    "message identity already has a different payload": "This message ID was already used for different encrypted data. The new upload was rejected.",
    "message has no retry record; verify delivery": "The service cannot safely retry this message because its delivery record is missing.",
    "credential already rotated; use saved refresh state": "The saved login recovery credential is older than the server's current credential.",
  };
  const defaults = {
    400: "The service rejected the request because its data does not match the chat state.",
    401: "The service rejected the saved login because it is invalid, expired, or disabled.",
    403: "The service refused permission for this request.",
    404: "The requested room or service endpoint does not exist.",
    409: "The request conflicts with the device, room, or message state saved by the service.",
    410: "This invite, room, or client operation is no longer available.",
    413: "The request is larger than the service allows.",
    422: "The service rejected one or more request fields as invalid.",
    429: "The service temporarily blocked this request because a rate or capacity limit was reached.",
  };
  let explanation = typeof detail === "string" && Object.hasOwn(explanations, detail)
    ? explanations[detail]
    : defaults[status] || (status >= 500
      ? "The chat service could not complete the request because of a server error."
      : "The chat service rejected this request.");
  if (status === 422 && Array.isArray(detail) && detail.some(item => (
    Array.isArray(item?.loc) && item.loc.length === 2
    && item.loc[0] === "body" && item.loc[1] === "public_key"
  ))) {
    explanation = "The service rejected this device's public encryption key. The client and server may use different test encryption formats.";
  }
  return new ClientError(`${context} (HTTP ${status}). ${explanation}`);
}

async function fetchClient(path, options = {}) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  options.signal?.addEventListener("abort", abort, { once: true });
  if (options.signal?.aborted) abort();
  const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    let response;
    try {
      response = await fetch(path, { ...options, signal: controller.signal });
    } catch {
      throw new ClientError("No response was received from the chat service. The network connection failed or the service is unavailable; the server may already have processed the request.");
    }
    // Reject an invalid login promptly even if a proxy's error body stalls.
    if (response.status === 401) return { ok: false, status: 401 };
    let body;
    try {
      if (response.ok && path.endsWith("/media/read")) {
        const reader = response.body.getReader();
        const bytes = new Uint8Array(SidewordProtocol.MAX_WIRE_CHARS + 2048);
        let size = 0;
        try {
          for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            if (size + value.byteLength > bytes.length) {
              await reader.cancel(); throw new Error("Media response too large");
            }
            bytes.set(value, size);
            size += value.byteLength;
          }
        } finally { reader.releaseLock(); }
        body = JSON.parse(decoder.decode(bytes.subarray(0, size)));
      } else body = await response.json();
    } catch {
      if (response.ok) {
        throw new ClientError("The chat service returned a response this client could not read; the server may already have processed the request.");
      }
    }
    return { ok: response.ok, status: response.status, body };
  } finally {
    options.signal?.removeEventListener("abort", abort);
    window.clearTimeout(timeout);
    controller.abort();
  }
}

class SessionExpiredError extends ClientError {}

async function invalidateSession(reason = "Session expired") {
  mediaPreviews?.clear();
  stopMediaTransfers();
  if (!identity) return;
  clearPresence();
  messageProfileReady = false;
  refreshVisibleReads();
  if (identity.token) identity.suspendedToken = identity.token;
  identity.token = null;
  accessDeadline = null;
  updateSessionCountdown();
  await writeIdentity(identity, true);
  chats = [];
  selectedChatId = null;
  disconnectSocket();
  updateIdentityUi();
  elements.inviteToken.focus();
  throw new SessionExpiredError(`${reason}. Chat is paused; your local keys and saved messages are kept.`);
}

function randomCredential() {
  return bytesToBase64(crypto.getRandomValues(new Uint8Array(32)))
    .replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

async function deviceLock(name, action) {
  if (!globalThis.navigator?.locks) {
    throw new ClientError("This browser cannot coordinate saved logins and message retries safely across tabs because Web Locks is unavailable.");
  }
  return navigator.locks.request(`sideword:${name}`, action);
}

async function ensureFreshSession(force = false) {
  if (!identity?.token) return;
  const previousToken = identity.token;
  await deviceLock("session", async () => {
    const stored = await readIdentity();
    if (!stored) {
      throw new DeviceStorageError("Local device data is missing: the browser no longer has the saved device record this tab loaded. Chat is paused to avoid losing access to its encryption key.");
    }
    if (stored.publicId !== identity.publicId || stored.publicKey !== identity.publicKey) {
      throw new ClientError("The device saved in this browser no longer matches the device this tab loaded. Its participant ID or encryption key changed. Chat is paused to avoid using the wrong identity.");
    }
    identity = stored;
    if ((!force || identity.token !== previousToken) && identity.refreshCredential
        && Date.now() < identity.tokenExpiresAt - 60000) return;
    if (!identity.refreshCredential) {
      await invalidateSession("This saved login is missing its refresh credential; reopen its invite to recover access");
    }
    identity.pendingRefreshCredential ||= randomCredential();
    await writeIdentity(identity, true); // Keep the proposed rotation if the response/save is lost.
    const response = await fetchClient("/api/v1/sessions/refresh", {
      method: "POST", cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        credential: identity.refreshCredential, next_credential: identity.pendingRefreshCredential,
      }),
    });
    if (!response.ok) {
      if (response.status === 401) await invalidateSession("The server rejected the saved login (HTTP 401); it has expired or was disabled");
      throw requestError(response.status, undefined, "Could not renew the saved login");
    }
    const result = response.body;
    identity.token = result.token;
    identity.sessionId = result.session_id;
    identity.tokenExpiresAt = Date.parse(result.access_expires_at);
    identity.sessionExpiresAt = Date.parse(result.session_expires_at);
    identity.refreshCredential = identity.pendingRefreshCredential;
    delete identity.pendingRefreshCredential;
    await writeIdentity(identity, true);
    disconnectSocket();
    connectSocket();
  });
}

async function api(path, options = {}) {
  const activation = path.includes("/links/");
  if (!activation && identity?.token) await ensureFreshSession();
  const headers = new Headers(options.headers || {});
  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (identity?.token) {
    headers.set("Authorization", `Bearer ${identity.token}`);
  }
  const response = await fetchClient(path, { ...options, headers, cache: "no-store" });
  if (!response.ok) {
    if (response.status === 401) {
      await invalidateSession("The server rejected the saved login (HTTP 401); it has expired or was disabled");
    }
    throw requestError(response.status, response.body?.detail, activation ? "Could not join the room" : "Chat request failed");
  }
  return response.body;
}

async function encryptForRecipient(plaintext, chatId, messageId, recipient) {
  await assertTrustedPeer(recipient);
  return SidewordProtocol.encryptForRecipient(identity, plaintext, chatId, messageId, recipient);
}

async function decryptMessage(message) {
  const chat = chats.find(candidate => candidate.id === message.chat_id);
  const sender = chat?.participants.find(peer => peer.public_id === message.sender_public_id);
  if (!sender) throw new ClientError("The message sender is missing from this room's participant list, so their encryption key is unavailable.");
  await assertTrustedPeer(sender);
  return SidewordProtocol.decryptMessage(identity, message, sender);
}

async function appendMessage(chatId, entry, persist = true) {
  const entries = messagesByChat.get(chatId) || [];
  if (entry.id && entries.some((candidate) => candidate.id === entry.id)) {
    return Promise.resolve();
  }
  entry.createdAt ||= Date.now();
  if (persist && entry.id) await persistHistoryEntry(chatId, entry);
  if (persist && entry.id && identity?.publicId) loadedHistoryKeys.add(historyKey(chatId, entry.id));
  entries.push(entry);
  entries.sort(compareHistoryEntries);
  messagesByChat.set(chatId, entries);
  indexMessageMetadata(chatId, entry);
  if (selectedChatId === chatId) renderMessages();
}

function serverTimestamp(value) {
  const utc = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}Z`;
  return Date.parse(utc) || Date.now();
}

function messageKey(message) {
  return `incoming:${JSON.stringify([message.chat_id, message.sender_public_id, message.client_message_id])}`;
}

function metadataKey(chatId, messageId) {
  return JSON.stringify([chatId, messageId]);
}

async function messageEventId(kind, fields) {
  // Keep recipient metadata inside encrypted values, not plaintext database keys.
  const digest = await crypto.subtle.digest("SHA-256", encoder.encode(JSON.stringify(fields)));
  return `${kind}:${bytesToBase64(new Uint8Array(digest))}`;
}

function metadataFor(chatId, messageId) {
  const key = metadataKey(chatId, messageId);
  if (!messageMetadata.has(key)) messageMetadata.set(key, { receipts: new Map(), acceptance: null });
  return messageMetadata.get(key);
}

function outgoingMessageId(entry) {
  return entry.clientMessageId;
}

function hiddenMessageEntry(entry) {
  return ["receipt", "acceptance", "viewed", "read_ack", "delivery_ack", "delivery_ref", "send_failed", "delivery_state", "media_ready"].includes(entry.kind);
}

function indexMessageMetadata(chatId, entry) {
  if (entry.kind === "media_ready") {
    receivedMedia.set(entry.target, entry);
    const original = messagesByChat.get(chatId)?.find(message => message.id === entry.target);
    if (original) original.attachment = entry.attachment;
    queueDelivery(entry.delivery);
    return;
  }
  if (entry.id && receivedMedia.has(entry.id)) entry.attachment = receivedMedia.get(entry.id).attachment;
  if (entry.kind === "receipt" && entry.readerPublicId && entry.clientMessageId) {
    const metadata = metadataFor(chatId, entry.clientMessageId);
    metadata.receipts.set(entry.id, entry);
    seenReceiptIds.add(entry.id);
  } else if (entry.kind === "acceptance") {
    const metadata = metadataFor(chatId, entry.clientMessageId);
    if (!metadata.acceptance || Object.keys(entry.deliveries || {}).length
        >= Object.keys(metadata.acceptance.deliveries || {}).length) metadata.acceptance = entry;
  } else if (entry.kind === "delivery_state") {
    const metadata = metadataFor(chatId, entry.clientMessageId);
    metadata.unavailable ||= new Map();
    metadata.unavailable.set(entry.recipient, entry);
  } else if (entry.kind === "send_failed") {
    outgoingAttempts.set(metadataKey(chatId, entry.clientMessageId), "failed");
  } else if (entry.kind === "delivery_ack") {
    acknowledgedDeliveries.add(entry.delivery.delivery_id);
    pendingDeliveries.delete(entry.delivery.delivery_id);
  } else if (entry.kind === "read_ack") {
    acknowledgedReads.add(entry.delivery.delivery_id);
    pendingReadsByChat.get(chatId)?.delete(entry.delivery.delivery_id);
  } else if (entry.kind === "viewed") {
    queueRead(entry.delivery);
  }
  if (entry.kind === "theirs" || entry.kind === "delivery_ref") {
    if (entry.kind === "theirs" && entry.id) seenMessageIds.add(entry.id);
    if (entry.delivery) {
      const key = entry.kind === "theirs" ? entry.id : entry.messageKey;
      const refs = deliveryReferences.get(key) || new Map();
      refs.set(entry.delivery.delivery_id, entry.delivery);
      deliveryReferences.set(key, refs);
      queueDelivery(entry.delivery);
    }
  }
}

function receiptTime(value) {
  if (!value) return null;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  const parsed = Date.parse(/(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}Z`);
  return Number.isNaN(parsed) ? null : parsed;
}

function outgoingStatus(chatId, entry) {
  const messageId = outgoingMessageId(entry);
  const metadata = metadataFor(chatId, messageId);
  const acceptance = metadata.acceptance;
  const recipientIds = acceptance?.recipients || entry.recipients;
  const recipientsKnown = Array.isArray(recipientIds) && recipientIds.length > 0
    && new Set(recipientIds).size === recipientIds.length;
  const ids = recipientsKnown ? recipientIds : [...new Set(
    [...metadata.receipts.values()].map(receipt => receipt.readerPublicId))];
  const states = ids.map(publicId => ({ publicId, delivered: false, read: false,
    deliveredAt: null, readAt: null }));
  for (const receipt of metadata.receipts.values()) {
    const state = states.find(peer => peer.publicId === receipt.readerPublicId);
    if (!state) continue;
    const expected = acceptance?.deliveries?.[state.publicId];
    if (expected && receipt.messageDeliveryId !== expected) continue;
    const time = Object.hasOwn(receipt, "confirmedAt") ? receipt.confirmedAt : historyTimestamp(receipt);
    state.delivered = true;
    if (receipt.receiptKind === "read") {
      state.read = true;
      if (time !== null) state.readAt = state.readAt === null ? time : Math.min(state.readAt, time);
    } else {
      if (time !== null) state.deliveredAt = state.deliveredAt === null ? time : Math.min(state.deliveredAt, time);
    }
  }
  for (const state of states) {
    const terminal = metadata.unavailable?.get(state.publicId);
    const expected = acceptance?.deliveries?.[state.publicId];
    if (!state.delivered && terminal && expected === terminal.deliveryId) state.failure = terminal.state;
  }
  const failures = states.filter(peer => peer.failure);
  const accepted = Boolean(acceptance || states.some(peer => peer.delivered));
  const attempt = outgoingAttempts.get(metadataKey(chatId, messageId));
  const readCount = states.filter(peer => peer.read).length;
  const allRead = recipientsKnown && readCount === states.length;
  const status = !accepted ? (attempt || "pending")
    : failures.length ? (failures.some(peer => peer.failure === "unavailable") ? "unavailable" : "expired")
    : readCount > 0 ? "read"
    : recipientsKnown && states.every(peer => peer.delivered) ? "delivered" : "sent";
  return { status, states, recipientsKnown, messageId, accepted, readCount, allRead,
    timestamp: acceptance?.createdAt || historyTimestamp(entry) };
}

function statusLabel(status) {
  return ({ sending: "Sending", pending: "Pending retry", failed: "Failed", sent: "Sent",
    delivered: "Delivered", read: "Read", expired: "Delivery expired", unavailable: "Delivery unavailable" })[status] || "Status unknown";
}

function messageStatusLabel(state) {
  if (state.status !== "read" || (state.recipientsKnown && state.states.length === 1)) {
    return statusLabel(state.status);
  }
  return state.recipientsKnown ? `Read by ${state.readCount} of ${state.states.length} recipients`
    : `Read by ${state.readCount} confirmed recipient${state.readCount === 1 ? "" : "s"}`;
}

function enqueueIncoming(action) {
  const result = inboundQueue.then(action);
  inboundQueue = result.catch(() => {});
  return result;
}

function historyTimestamp(entry) {
  if (typeof entry.createdAt === "number") return entry.createdAt;
  const parsed = Date.parse(entry.createdAt);
  return Number.isNaN(parsed) ? 0 : parsed;
}

function compareHistoryEntries(left, right) {
  const timestampDifference = historyTimestamp(left) - historyTimestamp(right);
  if (timestampDifference) return timestampDifference;
  return String(left.id || "").localeCompare(String(right.id || ""));
}

function participantLabel(chatId, publicId, includePublicId = true) {
  const chat = chats.find((candidate) => candidate.id === chatId);
  const participant = chat?.participants.find((peer) => peer.public_id === publicId);
  if (!participant) return null;
  const name = participant.display_name?.trim();
  return name ? (includePublicId ? `${name} (${publicId})` : name) : publicId;
}

function messageMeta(chatId, entry) {
  if (entry.kind === "mine") {
    const label = participantLabel(chatId, entry.senderPublicId || identity?.publicId, false);
    return label ? `Sent by ${label}` : entry.meta;
  }
  if (entry.kind !== "theirs") return entry.meta;
  const label = participantLabel(chatId, entry.senderPublicId);
  return label ? `From ${label}` : entry.meta;
}

function readableNow(node) {
  if (chats.find(chat => chat.id === selectedChatId)?.closed_at
      || !messageProfileReady || !conversationVisible() || document.visibilityState !== "visible"
      || typeof document.hasFocus !== "function" || !document.hasFocus()
      || elements.detailsDialog.open || elements.deviceDialog.open || elements.messageDetails.open
      || !visibleMessageNodes.has(node) || renderedMessages.get(node.messageEntry?.id) !== node) return false;
  const box = node.messageBubble.getBoundingClientRect();
  const list = elements.messageList.getBoundingClientRect();
  const top = Math.max(box.top, list.top, 0);
  const bottom = Math.min(box.bottom, list.bottom, window.innerHeight);
  const width = Math.min(box.right, list.right, window.innerWidth) - Math.max(box.left, list.left, 0);
  return box.height > 0 && width >= box.width / 2
    && bottom - top >= Math.min(box.height / 2, 48);
}

const messageVisibilityObserver = typeof IntersectionObserver === "function"
  ? new IntersectionObserver(entries => {
    for (const entry of entries) {
      if (entry.isIntersecting) visibleMessageNodes.add(entry.target);
      else visibleMessageNodes.delete(entry.target);
    }
    refreshVisibleReads();
    scheduleVisiblePreviews();
  }, { root: elements.messageList, threshold: [0, 0.5, 1] }) : null;

function refreshVisibleReads() {
  for (const [node, timer] of viewTimers) {
    if (!readableNow(node)) { window.clearTimeout(timer); viewTimers.delete(node); }
  }
  for (const node of visibleMessageNodes) {
    if (!readableNow(node) || viewTimers.has(node)) continue;
    const refs = [...(deliveryReferences.get(node.messageEntry.id)?.values() || [])];
    if (!refs.some(ref => !acknowledgedReads.has(ref.delivery_id)
        && !pendingReadsByChat.get(ref.chat_id)?.has(ref.delivery_id))) continue;
    viewTimers.set(node, window.setTimeout(() => {
      viewTimers.delete(node);
      if (!readableNow(node)) return;
      void enqueueIncoming(async () => {
        if (!readableNow(node)) return;
        for (const reference of refs) {
          // Save viewing evidence before queuing a retryable read confirmation.
          await appendMessage(reference.chat_id, { id: `viewed:${reference.delivery_id}`,
            kind: "viewed", delivery: reference });
        }
        await flushAcknowledgements();
      }).catch(error => showToast(errorMessage(error), true));
    }, 750));
  }
}

function displayTimestamp(value, full = false, formatters = null) {
  if (!Number.isFinite(value) || value <= 0) return "Time unavailable";
  let formatter = formatters?.get(full);
  if (!formatter) {
    const options = full ? { dateStyle: "short", timeStyle: "medium" }
      : { hour: "2-digit", minute: "2-digit" };
    formatter = new Intl.DateTimeFormat(undefined, options);
    formatters?.set(full, formatter);
  }
  return formatter.format(new Date(value));
}

function messageSenderId(chatId, entry) {
  if (entry.senderPublicId) return entry.senderPublicId;
  if (entry.kind === "mine") return identity?.publicId || null;
  return null;
}

function messageSenderName(chatId, entry) {
  const publicId = messageSenderId(chatId, entry);
  const peer = chats.find(chat => chat.id === chatId)?.participants
    .find(peer => peer.public_id === publicId);
  if (peer) return peer.display_name?.trim() || (entry.kind === "mine" ? "You" : "Participant");
  // Preserve saved labels while keeping routing IDs out of the timeline.
  let saved = entry.meta?.replace(/^(?:From |Sent by )/, "").trim();
  if (publicId && saved?.endsWith(` (${publicId})`)) saved = saved.slice(0, -(publicId.length + 3));
  if (saved && saved !== publicId && /^(?:From |Sent by )/.test(entry.meta)) return saved;
  return entry.kind === "mine" ? "You" : "Participant";
}

function messageDisplayTime(chatId, entry) {
  if (entry.kind !== "mine") return historyTimestamp(entry);
  const acceptance = metadataFor(chatId, outgoingMessageId(entry)).acceptance;
  return acceptance?.createdAt || historyTimestamp(entry);
}

function groupedWithPrevious(chatId, entry, previous) {
  if (!previous || !["mine", "theirs"].includes(entry.kind) || entry.kind !== previous.kind) return false;
  const sender = messageSenderId(chatId, entry);
  const elapsed = messageDisplayTime(chatId, entry) - messageDisplayTime(chatId, previous);
  return Boolean(sender && sender === messageSenderId(chatId, previous)
    && elapsed >= 0 && elapsed <= 5 * 60 * 1000);
}

function deliveryIcon(status) {
  const kind = status === "delivered" || status === "read" ? "double-check"
    : status === "sent" ? "check" : status === "sending" ? "clock" : "warning";
  const paths = {
    check: ["M6 9l5 5L19 3"],
    "double-check": ["M2 10l5 5L18 4", "M13 14l2 2 7-7"],
    clock: ["M12 3a6 6 0 1 0 0 12a6 6 0 1 0 0-12", "M12 5v4l3 2"],
    warning: ["M12 2l10 14H2Z", "M12 7v4", "M12 13h.01"],
  };
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 18");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.dataset.icon = kind;
  for (const definition of paths[kind]) {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", definition);
    svg.append(path);
  }
  return svg;
}

function updateMessageFooter(node, chatId, entry) {
  if (entry.kind !== "mine") return;
  let footer = node.messageFooter;
  if (!footer) {
    footer = document.createElement("span");
    footer.className = "message-footer";
    footer.indicator = document.createElement("button");
    footer.indicator.type = "button";
    footer.indicator.className = "message-status";
    footer.indicator.addEventListener("click", event => {
      event.stopPropagation(); showMessageDetails(chatId, entry, footer.indicator);
    });
    footer.append(footer.indicator);
    node.messageFooter = footer;
    node.messageBubble.append(footer);
  }
  const state = outgoingStatus(chatId, entry);
  const status = entry.attachment ? messageStatusLabel(state).replace(/^Read/, "Card viewed") : messageStatusLabel(state);
  const label = `${status}. Open message details`;
  footer.indicator.className = `message-status status-${state.status}`;
  footer.indicator.setAttribute("aria-label", label);
  footer.indicator.title = label;
  const icon = deliveryIcon(state.status);
  if (footer.indicator.dataset.icon !== icon.dataset.icon) {
    footer.indicator.replaceChildren(icon);
    footer.indicator.dataset.icon = icon.dataset.icon;
  }
}

function updateMessageHeader(node, chatId, entry, previous, timestampFormatters) {
  if (!node.messageHeader) return;
  const header = node.messageHeader;
  const timestamp = messageDisplayTime(chatId, entry);
  const grouped = groupedWithPrevious(chatId, entry, previous);
  node.className = `message ${entry.kind}${grouped ? " continuation" : ""}`;
  header.sender.textContent = messageSenderName(chatId, entry);
  header.sender.hidden = grouped;
  header.dot.hidden = grouped;
  header.timestamp.textContent = displayTimestamp(timestamp, false, timestampFormatters);
  header.timestamp.title = displayTimestamp(timestamp, true, timestampFormatters);
  if (timestamp > 0) header.timestamp.dateTime = new Date(timestamp).toISOString();
  header.hidden = grouped && Math.floor(timestamp / 60000)
    === Math.floor(messageDisplayTime(chatId, previous) / 60000);
  node.setAttribute("aria-label", `Message from ${header.sender.textContent}`);
  updateMessageFooter(node, chatId, entry);
}

function detailsParticipantLabel(chatId, publicId) {
  const chat = chats.find(chat => chat.id === chatId);
  const peer = chat?.participants.find(peer => peer.public_id === publicId);
  const name = peer?.display_name?.trim();
  if (!name) return publicId;
  const duplicate = chat.participants.some(candidate => candidate.public_id !== publicId
    && candidate.display_name?.trim() === name);
  return duplicate ? `${name} (${publicId})` : name;
}

function refreshMessageDetails() {
  if (!openMessageDetails) return;
  const { chatId, entry } = openMessageDetails;
  const state = outgoingStatus(chatId, entry);
  document.querySelector("#message-details-status").textContent = entry.attachment
    ? `${messageStatusLabel(state).replace(/^Read/, "Card viewed")} · File opening/download is not confirmed`
    : messageStatusLabel(state);
  document.querySelector("#message-details-time").textContent = displayTimestamp(state.timestamp, true);
  const groups = document.querySelector("#message-details-recipients");
  groups.replaceChildren();
  for (const [label, peers, timeField] of [
    ["Read by", state.states.filter(peer => peer.read), "readAt"],
    ["Delivered to", state.states.filter(peer => peer.delivered && !peer.read), "deliveredAt"],
    ["Pending", state.states.filter(peer => !peer.delivered && !peer.failure), null],
    ...(["expired", "unavailable"].filter(failure => state.states.some(peer => peer.failure === failure))
      .map(failure => [failure === "expired" ? "Expired without confirmation" : "Unavailable without confirmation",
        state.states.filter(peer => peer.failure === failure), null])),
  ]) {
    const section = document.createElement("section");
    const heading = document.createElement("h3"); heading.textContent = label;
    const list = document.createElement("ul");
    for (const peer of peers) {
      const row = document.createElement("li");
      const name = document.createElement("span"); name.textContent = detailsParticipantLabel(chatId, peer.publicId);
      row.append(name);
      if (timeField) {
        const time = document.createElement("span"); time.className = "receipt-time";
        time.textContent = displayTimestamp(peer[timeField], true); row.append(time);
      }
      list.append(row);
    }
    if (!peers.length) { const empty = document.createElement("li"); empty.textContent = "None confirmed"; list.append(empty); }
    section.append(heading, list); groups.append(section);
  }
  const note = document.querySelector("#message-details-note");
  note.textContent = !state.recipientsKnown ? "The original recipient list is unavailable. Full delivery and reading cannot be confirmed."
    : state.states.some(peer => peer.failure) ? "Some recipients have no delivery confirmation and their relay copies are no longer available. Your local copy is kept; verify with them before sending again."
    : "Pending means delivery has not been confirmed. Confirmations may be delayed.";
  // Keep technical controls and focus stable as receipt updates arrive.
  const technical = document.querySelector("#message-details-technical");
  const signature = JSON.stringify([state.messageId, state.states.map(peer => peer.publicId)]);
  if (technical.dataset.signature !== signature) {
    technical.dataset.signature = signature;
    technical.replaceChildren();
    const feedback = document.querySelector("#message-details-copy-status");
    for (const [label, value] of [["Message ID", state.messageId],
      ["Sender public ID", entry.senderPublicId || identity.publicId],
      ...state.states.map(peer => ["Participant public ID", peer.publicId])]) {
      const detail = publicDetail(label, value, feedback);
      technical.append(detail.term, detail.description);
    }
  }
  positionMessageDetails();
}

function positionMessageDetails() {
  const dialog = elements.messageDetails;
  if (!dialog.open || !messageDetailsTrigger) return;
  if (mobileWorkspace()) {
    dialog.style.left = "";
    dialog.style.top = "";
    dialog.style.maxWidth = "";
    dialog.style.maxHeight = "";
    return;
  }
  const viewport = window.visualViewport;
  const left = (viewport?.offsetLeft || 0) + 12;
  const top = (viewport?.offsetTop || 0) + 12;
  const right = left + (viewport?.width || window.innerWidth) - 24;
  const bottom = top + (viewport?.height || window.innerHeight) - 24;
  dialog.style.maxWidth = `${Math.max(0, right - left)}px`;
  dialog.style.maxHeight = `${Math.max(0, bottom - top)}px`;
  const box = messageDetailsTrigger.getBoundingClientRect();
  const rect = dialog.getBoundingClientRect();
  const below = box.bottom + 8;
  const preferredTop = below + rect.height <= bottom ? below : box.top - rect.height - 8;
  dialog.style.left = `${Math.max(left, Math.min(box.right - rect.width, right - rect.width))}px`;
  dialog.style.top = `${Math.max(top, Math.min(preferredTop, bottom - rect.height))}px`;
}

function showMessageDetails(chatId, entry, trigger) {
  openMessageDetails = { chatId, entry };
  messageDetailsTrigger = trigger;
  document.querySelector("#message-details-copy-status").hidden = true;
  refreshMessageDetails();
  const dialog = elements.messageDetails;
  if (!dialog.open) dialog.showModal();
  positionMessageDetails();
  document.querySelector("#close-message-details").focus({ preventScroll: true });
  refreshVisibleReads();
}

function renderMessages(openAtLatest = false) {
  if (!conversationVisible()) { mediaPreviews?.clear(); return; }
  const list = elements.messageList;
  const changedChat = renderedChatId !== selectedChatId;
  const followLatest = openAtLatest || changedChat || followResizedMessages;
  followResizedMessages = followLatest;
  list.setAttribute("data-follow-latest", String(followLatest));
  if (changedChat) {
    mediaPreviews?.clear();
    messageVisibilityObserver?.disconnect();
    visibleMessageNodes.clear();
    refreshVisibleReads();
    for (const node of renderedMessages.values()) messageResizeObserver?.unobserve(node.messageBubble);
    list.replaceChildren();
    renderedMessages.clear();
    renderedChatId = selectedChatId;
  }
  const entries = [...(messagesByChat.get(selectedChatId) || [])]
    .filter(entry => !hiddenMessageEntry(entry)).sort((left, right) => {
      return messageDisplayTime(selectedChatId, left) - messageDisplayTime(selectedChatId, right)
        || compareHistoryEntries(left, right);
    });
  if (!entries.length) {
    mediaPreviews?.clear();
    if (list.firstChild && !renderedMessages.size) return;
    for (const node of renderedMessages.values()) messageResizeObserver?.unobserve(node.messageBubble);
    list.replaceChildren();
    renderedMessages.clear();
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "No messages yet.";
    list.append(empty);
    return;
  }
  if (!renderedMessages.size) list.replaceChildren();
  const keys = new Set(entries.map(entry => entry.id || entry));
  for (const [key, node] of renderedMessages) {
    if (!keys.has(key)) {
      node.releaseMedia?.();
      messageResizeObserver?.unobserve(node.messageBubble);
      node.remove();
      messageVisibilityObserver?.unobserve(node);
      visibleMessageNodes.delete(node);
      renderedMessages.delete(key);
    }
  }
  // Reuse formatters within this render only, so later renders pick up locale/time-zone changes.
  const timestampFormatters = new Map();
  let position = 0;
  for (const entry of entries) {
    const key = entry.id || entry;
    const messageTextContent = entry.text || "";
    const previous = entries[position - 1];
    const existing = renderedMessages.get(key);
    if (existing) {
      if (existing.messageContent.textContent !== messageTextContent) existing.messageContent.textContent = messageTextContent;
      existing.messageEntry = entry;
      if (entry.attachment) updateAttachmentCard(existing);
      updateMessageHeader(existing, selectedChatId, entry, previous, timestampFormatters);
      if (list.children[position] !== existing) list.insertBefore(existing, list.children[position] || null);
      position++;
      continue;
    }
    const message = document.createElement("div");
    message.className = `message ${entry.kind}`;
    message.messageEntry = entry;
    const chatId = selectedChatId;
    if (["mine", "theirs"].includes(entry.kind)) {
      const header = document.createElement("div");
      header.className = "message-header";
      header.sender = document.createElement("span");
      header.sender.className = "message-sender";
      header.dot = document.createElement("span");
      header.dot.className = "message-separator";
      header.dot.textContent = "·";
      header.dot.setAttribute("aria-hidden", "true");
      header.timestamp = document.createElement("time");
      header.append(header.sender, header.dot, header.timestamp);
      message.messageHeader = header;
      message.append(header);
      message.setAttribute("role", "group");
    }
    const bubble = document.createElement("div");
    bubble.className = "message-bubble";
    message.messageBubble = bubble;
    const text = document.createElement("span");
    text.className = "message-content";
    text.textContent = messageTextContent;
    message.messageContent = text;
    bubble.append(text);
    if (entry.attachment) {
      bubble.className += " attachment-card";
      text.hidden = !messageTextContent;
      const download = document.createElement("button");
      download.type = "button";
      const info = document.createElement("div");
      info.className = "attachment-info";
      const size = document.createElement("small");
      size.className = "attachment-size";
      size.textContent = `${["image/jpeg", "image/png", "image/webp"].includes(entry.attachment.mime) ? "Image" : "File"} · ${fileSize(entry.attachment.size)}`;
      const state = document.createElement("small");
      state.className = "attachment-state";
      state.setAttribute("role", "status");
      info.append(size, state);
      download.className = "button subtle attachment-download";
      const downloadLabel = document.createElement("span");
      downloadLabel.className = "sr-only";
      downloadLabel.textContent = "Download";
      download.append(downloadIcon(), downloadLabel);
      download.title = "Download file";
      download.setAttribute("aria-label", "Download attachment");
      const error = document.createElement("span");
      error.className = "field-error";
      error.setAttribute("role", "alert");
      download.addEventListener("click", event => {
        event.stopPropagation();
        const attachment = message.messageEntry.attachment;
        if (attachment.type === "attachment") downloadAttachment(attachment, error);
        else { mediaFailures.delete(entry.id); entry.mediaTransfer = null; scheduleMediaTransfers(); updateAttachmentCard(message); }
      });
      const tile = document.createElement("span");
      tile.className = "file-tile";
      tile.setAttribute("aria-hidden", "true");
      tile.append(fileIcon());
      bubble.append(tile, info, download, error);
      const preview = document.createElement("div");
      preview.className = "media-preview";
      bubble.append(preview);
      message.attachmentUi = { state, download, preview };
      updateAttachmentCard(message);
    }
    message.append(bubble);
    messageResizeObserver?.observe(bubble);
    updateMessageHeader(message, chatId, entry, previous, timestampFormatters);
    if (entry.kind === "mine") {
      message.tabIndex = 0;
      message.setAttribute("aria-haspopup", "dialog");
      message.addEventListener("click", event => {
        if (event.target?.tagName === "BUTTON" || window.getSelection()?.toString()) return;
        showMessageDetails(chatId, entry, message);
      });
      message.addEventListener("keydown", event => {
        if (event.target !== message || !["Enter", " "].includes(event.key)) return;
        event.preventDefault(); showMessageDetails(chatId, entry, message);
      });
    }
    list.insertBefore(message, list.children[position] || null);
    renderedMessages.set(key, message);
    if (entry.kind === "theirs" || entry.attachment) messageVisibilityObserver?.observe(message);
    position++;
  }
  if (followLatest) list.scrollTop = list.scrollHeight;
  refreshMessageDetails();
  refreshVisibleReads();
  scheduleVisiblePreviews();
}

function scheduleVisiblePreviews() {
  if (!mediaPreviews || previewTimer !== null) return;
  previewTimer = window.setTimeout(() => {
    previewTimer = null;
    if (!pageActive || document.visibilityState !== "visible" || !conversationVisible()) {
      mediaPreviews?.clear(); shownPreviews.clear(); return;
    }
    const list = elements.messageList.getBoundingClientRect();
    const candidates = new Set(messageVisibilityObserver ? visibleMessageNodes : renderedMessages.values());
    for (const node of shownPreviews) candidates.add(node);
    const near = new Set(), visible = new Set();
    const top = Math.max(0, list.top), bottom = Math.min(window.innerHeight, list.bottom);
    for (const node of candidates) {
      if (!node.previewAttached || node.isConnected === false || !node.releaseMedia.available()
          || renderedMessages.get(node.messageEntry.id || node.messageEntry) !== node) continue;
      const box = node.getBoundingClientRect();
      if (box.right <= list.left || box.left >= list.right) continue;
      if (box.bottom > top - 256 && box.top < bottom + 256) near.add(node);
      if (box.bottom > top && box.top < bottom) visible.add(node);
    }
    // Keep visible previews stable; use remaining slots for already decoded nearby images.
    const selected = new Set();
    for (const group of [[...shownPreviews].filter(node => visible.has(node)), visible,
      [...shownPreviews].filter(node => near.has(node))]) {
      for (const node of group) {
        if (selected.size < SidewordMedia.MAX_ACTIVE) selected.add(node);
      }
    }
    for (const node of shownPreviews) if (!selected.has(node)) node.releaseMedia?.();
    shownPreviews.clear();
    for (const node of selected) { shownPreviews.add(node); void node.releaseMedia.load(); }
  }, 0);
}

function updateAttachmentCard(node) {
  const entry = node.messageEntry, ui = node.attachmentUi;
  if (!ui) return;
  const ready = entry.attachment.type === "attachment";
  const failure = mediaFailures.get(entry.id) || entry.mediaTransfer;
  const upload = entry.kind === "mine" && mediaUploadStates.get(metadataKey(selectedChatId, entry.clientMessageId));
  const label = ready ? upload || "Saved on this device" : failure?.attempts >= 6 ? "Unavailable · Retry download"
    : failure?.active ? "Downloading…" : "Waiting for media…";
  if (ui.state.textContent !== label) ui.state.textContent = label;
  ui.download.disabled = !ready && Boolean(failure?.active);
  ui.download.title = ready ? "Download file" : "Retry media download";
  ui.download.setAttribute("aria-label", ready ? "Download attachment" : "Retry media download");
  const image = ["image/jpeg", "image/png", "image/webp"].includes(entry.attachment.mime);
  if (!ready && image) {
    ui.preview.className = "media-preview media-frame";
    if (!ui.placeholder) {
      ui.placeholder = document.createElement("span");
      ui.placeholder.setAttribute("role", "status");
      ui.preview.append(ui.placeholder);
    }
    ui.placeholder.className = failure?.attempts >= 6 ? "media-status" : "media-status media-loading";
    ui.placeholder.textContent = failure?.attempts >= 6 ? "Image unavailable" : "Loading image…";
  }
  if (ready && mediaPreviews && !node.previewAttached
      && (image || ["", "application/octet-stream"].includes(entry.attachment.mime))) {
    node.previewAttached = true;
    ui.preview.replaceChildren();
    node.releaseMedia = mediaPreviews.attach(ui.preview,
      () => SidewordProtocol.attachmentBytes(node.messageEntry.attachment), entry.attachment.mime);
  }
}

function chatName(chat) {
  if (chat.title) return chat.title;
  const peers = chat.participants.filter((participant) => participant.public_id !== identity.publicId);
  return peers.map((peer) => peer.display_name || peer.public_id).join(", ") || "Waiting for peer";
}

function renderChats() {
  const focusedChatId = document.activeElement?.dataset?.chatId;
  elements.chatList.replaceChildren();
  if (!chats.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "No chats yet. Activate an invite.";
    elements.chatList.append(empty);
    return;
  }
  for (const chat of chats) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `chat-button${chat.id === selectedChatId ? " active" : ""}`;
    button.dataset.chatId = String(chat.id);
    button.setAttribute("aria-current", chat.id === selectedChatId ? "true" : "false");
    const title = document.createElement("strong");
    title.textContent = chatName(chat);
    const detail = document.createElement("span");
    detail.textContent = `${chat.closed_at ? "Closed · " : ""}${chat.chat_type} · ${chat.participants.length} participant${chat.participants.length === 1 ? "" : "s"}`;
    button.append(title, detail);
    button.addEventListener("click", () => {
      elements.clientPanel.dataset.mobileView = "conversation";
      selectChat(chat.id, mobileWorkspace());
      rememberChatView("conversation", chat.id);
      if (mobileWorkspace()) {
        if (!elements.messageInput.disabled) elements.messageInput.focus({ preventScroll: true });
        else elements.conversationTitle.focus({ preventScroll: true });
      }
    });
    const row = document.createElement("div");
    row.setAttribute("role", "listitem");
    row.append(button);
    elements.chatList.append(row);
    if (focusedChatId === String(chat.id)) button.focus({ preventScroll: true });
  }
}

function selectChat(chatId, openAtLatest = false) {
  const changedChat = renderedChatId !== chatId;
  if (selectedChatId !== chatId && !sendingMessage) clearAttachment();
  selectedChatId = chatId;
  if (openMessageDetails && openMessageDetails.chatId !== chatId) elements.messageDetails.close();
  const chat = chats.find((candidate) => candidate.id === chatId);
  elements.conversationKind.textContent = chat?.closed_at
    ? "Closed · Saved history" : chat?.chat_type || "Conversation";
  elements.conversationTitle.textContent = chat ? chatName(chat) : "No conversation selected";
  elements.detailsButton.disabled = !chat;
  const changedKey = Boolean(chat?.participants.some(item => item.key_changed));
  elements.keyWarning.hidden = !changedKey;
  const warningText = changedKey
    ? "KEY CHANGED; blocked. A participant's encryption key changed and no longer matches the fingerprint saved on this device. Sending and decryption are blocked. Keep this device's history."
    : "";
  if (elements.keyWarning.textContent !== warningText) elements.keyWarning.textContent = warningText;
  renderParticipantDetails(chat);
  const hasPeers = Boolean(chat?.participants.some(item => item.public_id !== identity.publicId));
  const waitingForPeer = Boolean(chat && !chat.closed_at && !changedKey && !hasPeers);
  const canSend = Boolean(chat && !chat.closed_at && !changedKey && hasPeers);
  elements.waitingNotice.hidden = !waitingForPeer;
  elements.messageInput.disabled = !canSend;
  elements.messageInput.placeholder = waitingForPeer ? "Waiting for another participant…" : "Write a message";
  elements.sendButton.disabled = !canSend || sendingMessage;
  elements.attachmentButton.disabled = !canSend || sendingMessage;
  elements.attachmentInput.disabled = !canSend || sendingMessage;
  elements.messageInput.hidden = false;
  elements.messageForm.classList.toggle("has-attachment", Boolean(selectedFile));
  elements.sendButton.textContent = sendingMessage ? "Sending…" : selectedFile ? "Send file" : "Send";
  elements.attachmentButton.setAttribute("aria-label", selectedFile ? "Replace attached file" : "Attach a file");
  document.querySelector("#composer-help").textContent = chat?.closed_at
    ? "Conversation closed. Saved history is kept; messaging is paused."
    : changedKey ? "Messaging is blocked because a participant's encryption key changed."
    : waitingForPeer ? "Sending is unavailable until another participant joins."
    : !chat ? "Select a conversation to send a message."
    : selectedFile ? "Ready to send · Optional message above the attachment"
    : "Enter to send · Shift+Enter for a new line";
  renderChats();
  renderMessages(openAtLatest);
  if (canSend && changedChat && !elements.clientPanel.hidden
      && !elements.detailsDialog.open && !elements.deviceDialog.open
      && (!mobileWorkspace() || elements.clientPanel.dataset.mobileView === "conversation")) {
    elements.messageInput.focus({ preventScroll: true });
  }
}

function updateIdentityUi() {
  const active = Boolean(!invitePending && identity?.token && identity?.publicId);
  elements.loadingPanel.hidden = true;
  elements.setupPanel.hidden = active;
  elements.clientPanel.hidden = !active;
  if (!active && elements.detailsDialog.open) elements.detailsDialog.close();
  document.querySelector("#reconnect-session").hidden = invitePending || active || !identity?.suspendedToken;
  updateConnectionState(false);
  if (active) renderMessages();
}

function updateConnectionState(connected) {
  if (!connected) clearPresence();
  const active = Boolean(!invitePending && identity?.token && identity?.publicId);
  elements.connectionState.classList.toggle("online", active && connected);
  elements.identityLabel.textContent = active
    ? `Device ${identity.publicId} · ${connected ? "Live" : "Polling"}`
    : "No local identity";
  elements.connectionLabel.textContent = active ? (connected ? "Live" : "Polling") : "Offline";
  elements.devicePublicId.textContent = identity?.publicId || "No local identity";
}

async function loadChats() {
  if (invitePending || !identity?.token) return;
  const data = await api("/api/v1/me");
  const deadline = data.session_expires_at || data.access_expires_at;
  accessDeadline = deadline
    ? performance.now() + serverTimestamp(deadline) - serverTimestamp(data.server_time)
    : null;
  updateSessionCountdown();
  if (data.user.public_key !== identity.publicKey) {
    throw new ClientError("The server's account encryption key differs from this device's saved key. The login and local device identity do not match. Keep this device's history.");
  }
  const checkedChats = [];
  for (const chat of data.chats) {
    const participants = [];
    for (const peer of chat.participants) participants.push(await observePeerKey(peer));
    checkedChats.push({ ...chat, participants });
  }
  identity.publicId = data.user.public_id;
  identity.retryWindowSeconds = data.send_retry_window_seconds;
  if (!Number.isSafeInteger(data.receipt_retention_seconds) || data.receipt_retention_seconds <= 0) {
    throw new ClientError("The service returned incomplete message retention information.");
  }
  identity.receiptRetentionSeconds = data.receipt_retention_seconds;
  messageProfileReady = true;
  await writeIdentity(identity);
  chats = checkedChats;
  if (selectedChatId && !chats.some((chat) => chat.id === selectedChatId)) {
    selectedChatId = null;
    if (mobileWorkspace()) {
      elements.clientPanel.setAttribute("data-mobile-view", "chats");
      rememberChatView("chats", null);
    }
  }
  if (!selectedChatId && chats.length) selectedChatId = chats[0].id;
  if (selectedChatId) selectChat(selectedChatId);
  else selectChat(null);
}

function senderKeyIsLoaded(message) {
  const chat = chats.find((candidate) => candidate.id === message.chat_id);
  return Boolean(
    chat?.participants.some(
      (participant) => participant.public_id === message.sender_public_id,
    ),
  );
}

function deliveryReference(delivery, peerField) {
  if (!/^[0-9a-f]{32}$/.test(delivery.delivery_id || "")) {
    throw new ClientError("The server sent incomplete delivery information. This client cannot safely confirm that the message was received, so it stays queued.");
  }
  return {
    delivery_id: delivery.delivery_id,
    chat_id: delivery.chat_id,
    client_message_id: delivery.client_message_id,
    [peerField]: delivery[peerField],
  };
}

function queueRead(message) {
  if (acknowledgedReads.has(message.delivery_id)) return;
  const reference = deliveryReference(message, "sender_public_id");
  const ids = pendingReadsByChat.get(message.chat_id) || new Map();
  ids.set(message.delivery_id, reference);
  pendingReadsByChat.set(message.chat_id, ids);
}

function queueDelivery(message) {
  if (acknowledgedDeliveries.has(message.delivery_id)) return;
  pendingDeliveries.set(message.delivery_id, deliveryReference(message, "sender_public_id"));
}

async function flushDeliveryBatch(path, field, pending, marker = null) {
  // Keep each failed batch queued. Delete only the successful snapshot so a
  // delivery added during the request cannot be lost.
  while (pending.size) {
    const batch = [...pending.values()].slice(0, 100);
    await api(path, {
      method: "POST",
      body: JSON.stringify({ [field]: batch }),
    });
    for (const reference of batch) {
      if (marker) await appendMessage(reference.chat_id, {
        id: `${marker}:${reference.delivery_id}`, kind: marker, delivery: reference,
      });
      pending.delete(reference.delivery_id);
    }
  }
}

async function flushDurableDeliveries() {
  await flushDeliveryBatch("/api/v1/ack/exact", "messages", pendingDeliveries,
    "delivery_ack");
}

async function flushAcknowledgements() {
  await flushDurableDeliveries();
  if (messageProfileReady) {
    for (const [chatId, ids] of pendingReadsByChat) {
      if (chats.find(chat => chat.id === chatId)?.closed_at) continue;
      await flushDeliveryBatch(`/api/v1/chats/${chatId}/read/exact`, "messages", ids,
        "read_ack");
      pendingReadsByChat.delete(chatId);
    }
  }
  await flushDeliveryBatch("/api/v1/ack/exact", "receipts", pendingReceiptIds);
}

async function processMessages(messages) {
  const needsFreshChat = messages.some(
    (message) => !seenMessageIds.has(messageKey(message)) && !senderKeyIsLoaded(message),
  );
  if (needsFreshChat) await loadChats();

  for (const message of messages) {
    const key = messageKey(message);
    if (seenMessageIds.has(key)) {
      const reference = deliveryReference(message, "sender_public_id");
      if (!deliveryReferences.get(key)?.has(reference.delivery_id)) {
        await appendMessage(message.chat_id, { id: `delivery_ref:${reference.delivery_id}`,
          kind: "delivery_ref", messageKey: key, delivery: reference });
      }
      queueDelivery(message);
      refreshVisibleReads();
      continue;
    }
    if (processingMessageIds.has(key) || (failedMessageRetries.get(key) || 0) > Date.now()) continue;
    processingMessageIds.add(key);
    try {
      const payload = SidewordProtocol.decodePayload(await decryptMessage(message));
      if (payload.type === "attachment") throw new SidewordProtocol.ProtocolError("File bytes require the media channel.");
      if (payload.type === "attachment-ref" && payload.media_id !== `${message.client_message_id}.media`) {
        throw new SidewordProtocol.ProtocolError("Invalid attachment reference.");
      }
      const entry = {
        id: key,
        kind: "theirs",
        ...payloadEntry(payload),
        senderPublicId: message.sender_public_id,
        meta: `From ${message.sender_public_id}`,
        createdAt: serverTimestamp(message.created_at),
        delivery: deliveryReference(message, "sender_public_id"),
      };
      entry.meta = messageMeta(message.chat_id, entry);
      await appendMessage(message.chat_id, entry);
      seenMessageIds.add(key);
      failedMessageReasons.delete(key);
      failedMessageRetries.delete(key);
      queueDelivery(message);
    } catch (error) {
      const reason = errorMessage(error);
      if (error instanceof SidewordProtocol.ProtocolError) {
        if (failedMessageRetries.size >= 1024) {
          const oldest = failedMessageRetries.keys().next().value;
          failedMessageRetries.delete(oldest);
          failedMessageReasons.delete(oldest);
        }
        failedMessageRetries.set(key, Date.now() + 60000);
      }
      if (failedMessageReasons.get(key) !== reason) {
        failedMessageReasons.set(key, reason);
        appendMessage(message.chat_id, {
          kind: "system",
          text: `Message retained for retry: ${reason}`,
        }, false);
      }
    } finally {
      processingMessageIds.delete(key);
    }
  }
}

async function processReceipts(receipts, delivered = false) {
  for (const receipt of receipts) {
    const reference = deliveryReference(receipt, "reader_public_id");
    if (!/^[0-9a-f]{32}$/.test(receipt.message_delivery_id || "")) {
      throw new ClientError("The service returned incomplete confirmation information.");
    }
    if (!delivered && receipt.view_confirmed !== true) {
      throw new ClientError("The service returned an invalid viewing confirmation.");
    }
    const stage = delivered ? "delivered" : "read";
    const key = await messageEventId("receipt", [receipt.chat_id, receipt.reader_public_id,
      receipt.client_message_id, receipt.message_delivery_id, stage]);
    if (seenReceiptIds.has(key)) {
      pendingReceiptIds.set(receipt.delivery_id, reference);
      continue;
    }
    const entry = {
      id: key,
      kind: "receipt",
      receiptKind: stage,
      messageDeliveryId: receipt.message_delivery_id,
      readerPublicId: receipt.reader_public_id,
      clientMessageId: receipt.client_message_id,
      confirmedAt: receiptTime(receipt.created_at),
      createdAt: receiptTime(receipt.created_at) || Date.now(),
    };
    await appendMessage(receipt.chat_id, entry);
    seenReceiptIds.add(key);
    pendingReceiptIds.set(receipt.delivery_id, reference);
  }
}

async function processIncoming(data) {
  await processMessages(data.messages || []);
  // Unrelated receipt-storage failures must not retain already durable ciphertext.
  await flushDurableDeliveries();
  await processReceipts(data.delivery_receipts || [], true);
  await processReceipts(data.read_receipts || []);
  await flushAcknowledgements();
  scheduleMediaTransfers();
}

function stopMediaTransfers() {
  window.clearTimeout(mediaTimer);
  mediaTimer = null;
  transferController?.abort();
  mediaFailures.clear();
}

function scheduleMediaTransfers(delay = 0) {
  if (mediaTimer !== null || transferringMedia || !pageActive || !identity?.token || invitePending
      || globalThis.navigator?.onLine === false) return;
  mediaTimer = window.setTimeout(() => {
    mediaTimer = null;
    void transferMedia();
  }, delay);
}

function mediaAttempt(key, state = null) {
  const previous = mediaFailures.get(key) || state;
  if (previous?.attempts >= 6 || previous?.nextAt > Date.now()) return null;
  if (!previous && mediaFailures.size >= 1024) mediaFailures.delete(mediaFailures.keys().next().value);
  const attempt = { attempts: previous?.attempts || 0, active: true };
  mediaFailures.set(key, attempt);
  return attempt;
}

function mediaFailed(key, attempt) {
  attempt.active = false;
  attempt.attempts++;
  attempt.nextAt = Date.now() + Math.min(30000, 1000 * 2 ** attempt.attempts);
  mediaFailures.set(key, attempt);
}

async function transferMedia() {
  if (transferringMedia || !identity?.token || !pageActive || invitePending) return;
  transferringMedia = true;
  const owner = identity.publicId, publicKey = identity.publicKey, storageKey = identity.storageKey;
  const controller = new AbortController();
  transferController = controller;
  const current = () => !controller.signal.aborted && identity?.publicId === owner
    && identity?.publicKey === publicKey && identity?.token && pageActive;
  const options = { signal: controller.signal, priority: "low" };
  let retry = false;
  try {
    // One media request at a time; this lock is independent of text-message sending.
    await deviceLock(`media:${owner}`, async () => {
      const records = await readHistoryRecords(`mediaoutbox:${owner}:`);
      for (const stored of records) {
        if (!current()) return;
        const attempt = mediaAttempt(stored.key);
        if (!attempt) { retry ||= mediaFailures.get(stored.key)?.attempts < 6; continue; }
        let uploadKey;
        try {
          const plaintext = await crypto.subtle.decrypt({ name: "AES-GCM", iv: stored.value.iv,
            additionalData: encoder.encode(stored.key) }, storageKey, stored.value.ciphertext);
          const record = JSON.parse(decoder.decode(plaintext));
          uploadKey = metadataKey(record.chatId, record.messageId.slice(0, -6));
          mediaUploadStates.set(uploadKey, "Sending media…");
          renderMessages();
          const chat = chats.find(chat => chat.id === record.chatId);
          if (!chat || chat.closed_at || record.expiresAt <= Date.now()) throw new ClientError("Media unavailable");
          const peers = record.recipients.map(id => chat.participants.find(peer => peer.public_id === id));
          for (const peer of peers) {
            if (!peer) throw new ClientError("Media participant unavailable");
            await assertTrustedPeer(peer);
          }
          if (!record.envelopes) {
            const payload = SidewordProtocol.encodePayload(record.payload);
            record.envelopes = [];
            for (const peer of peers) record.envelopes.push({ recipient_public_id: peer.public_id,
              ciphertext: await encryptForRecipient(payload, record.chatId, record.messageId, peer) });
            if (!current()) return;
            // Persist immutable ciphertext before upload; a lost response must retry identical bytes.
            await deviceLock(`outbox-capacity:${owner}`, async () => {
              const others = [...await readHistoryRecords(`mediaoutbox:${owner}:`),
                ...await readHistoryRecords(`outbox:${owner}:`)];
              const size = encoder.encode(JSON.stringify(record)).length + 16;
              if (others.filter(item => item.key !== stored.key)
                  .reduce((sum, item) => sum + item.value.ciphertext.byteLength, size) > 32 * 1024 * 1024) {
                throw new ClientError("Local media retry capacity reached");
              }
              if (current()) await persistOutbox(record, "mediaoutbox");
            });
          }
          if (!current()) return;
          await api(`/api/v1/chats/${record.chatId}/messages`, { ...options, method: "POST",
            body: JSON.stringify({ channel: "media", client_message_id: record.messageId, envelopes: record.envelopes }) });
          if (!current()) return;
          await removeOutbox(stored.key);
          mediaUploadStates.delete(uploadKey);
          mediaFailures.delete(stored.key);
          retry ||= records.length > 1;
        } catch { if (current()) {
          mediaFailed(stored.key, attempt);
          if (uploadKey && attempt.attempts >= 6) mediaUploadStates.set(uploadKey, "Could not send media");
          retry ||= attempt.attempts < 6;
        } }
        break; // Yield to message delivery between files, including group fanout.
      }
      for (const [chatId, entries] of messagesByChat) {
        if (!current()) return;
        if (chats.find(chat => chat.id === chatId)?.closed_at) continue;
        for (const entry of entries) {
          if (entry.kind !== "theirs" || entry.attachment?.type !== "attachment-ref") continue;
          const attempt = mediaAttempt(entry.id, entry.mediaTransfer);
          if (!attempt) { retry ||= (mediaFailures.get(entry.id) || entry.mediaTransfer)?.attempts < 6; continue; }
          entry.mediaTransfer = attempt;
          if (selectedChatId === chatId) renderMessages();
          try {
            const reference = entry.attachment;
            const message = await api(`/api/v1/chats/${chatId}/media/read`, { ...options, method: "POST",
              body: JSON.stringify({ sender_public_id: entry.senderPublicId, client_message_id: reference.media_id }) });
            if (!current()) return;
            if (message.chat_id !== chatId || message.sender_public_id !== entry.senderPublicId
                || message.client_message_id !== reference.media_id) throw new ClientError("Invalid media routing");
            const payload = SidewordProtocol.decodePayload(await decryptMessage(message));
            if (payload.type !== "attachment" || payload.name !== reference.name
                || payload.mime !== reference.mime || payload.size !== reference.size) throw new ClientError("Invalid media metadata");
            if (!current()) return;
            // Bytes authenticate and commit separately; metadata ACK has already completed.
            const saved = { id: `media_ready:${entry.id}`, kind: "media_ready", target: entry.id,
              attachment: payload, delivery: deliveryReference(message, "sender_public_id"), createdAt: Date.now() };
            if (await persistHistoryEntry(chatId, saved, current) === false || !current()) return;
            loadedHistoryKeys.add(historyKey(chatId, saved.id));
            await appendMessage(chatId, saved, false);
            mediaFailures.delete(entry.id);
            await flushDurableDeliveries();
          } catch { if (current()) { mediaFailed(entry.id, attempt); retry ||= attempt.attempts < 6; } }
          if (selectedChatId === chatId) renderMessages();
          retry = true;
          return;
        }
      }
    });
  } catch { retry = current(); } // Storage/network errors never escape into message rendering.
  finally {
    transferringMedia = false;
    transferController = null;
    if (current()) {
      renderMessages();
      if ([...mediaFailures.values()].some(failure => failure.attempts >= 6)) {
        try { await renderOutbox(); } catch { /* Durable retry data remains saved. */ }
      }
    }
    if (retry && current()) scheduleMediaTransfers(2000);
  }
}

async function synchronize({ initial = false } = {}) {
  if (!pageActive || globalThis.navigator?.onLine === false
      || invitePending || !identity?.token || synchronizing) return;
  synchronizing = true;
  try {
    await loadChats();
    await loadStoredHistory(); // Immutable encrypted events merge safely across tabs.
    if (initial) updateIdentityUi();
    try { await flushOutbox(); } catch (error) { showToast(errorMessage(error), true); }
    await renderOutbox();
    const data = await api("/api/v1/poll");
    await enqueueIncoming(() => processIncoming(data));
    await enqueueIncoming(recoverMessageStatuses);
    scheduleMediaTransfers();
  } catch (error) {
    showToast(errorMessage(error), true);
  } finally {
    synchronizing = false;
  }
}

function disconnectSocket() {
  window.clearTimeout(reconnectTimer);
  reconnectTimer = null;
  window.clearTimeout(socketAuthTimer);
  socketAuthTimer = null;
  window.clearInterval(heartbeatTimer);
  heartbeatTimer = null;
  const previous = socket;
  socket = null;
  updateConnectionState(false);
  if (previous && previous.readyState <= WebSocket.OPEN) previous.close();
}

function scheduleReconnect() {
  if (!pageActive || globalThis.navigator?.onLine === false
      || invitePending || reconnectTimer || !identity?.token) return;
  const ceiling = Math.min(30000, 1000 * 2 ** reconnectAttempts);
  reconnectAttempts = Math.min(reconnectAttempts + 1, 5);
  reconnectTimer = window.setTimeout(() => {
    reconnectTimer = null;
    connectSocket();
  }, Math.round(ceiling * (0.5 + Math.random() / 2)));
}

function retrySocket(ws) {
  if (socket !== ws) return;
  disconnectSocket();
  scheduleReconnect();
}

async function handleSocketPayload(payload, ws) {
  if (payload.type === "presence") {
    receivePresence(payload);
  } else if (payload.type === "auth_error") {
    clearPresence();
    if (identity?.refreshCredential) {
      await ensureFreshSession(true);
      connectSocket();
      return;
    }
    await invalidateSession("The saved session is no longer valid");
  } else if (payload.type === "hello") {
    await loadChats();
    if (socket !== ws) return;
    if (payload.user !== identity?.publicId) {
      throw new ClientError("The live connection and saved login identify different devices. Your local keys and history are kept.");
    }
    await processIncoming(payload.backlog || {});
    if (socket !== ws) return;
    reconnectAttempts = 0;
    updateConnectionState(true);
  } else if (payload.type === "chat_state") {
    await loadChats();
    await renderOutbox();
  } else if (payload.type === "message") {
    await processMessages([payload.message]);
    await flushAcknowledgements();
    scheduleMediaTransfers();
  } else if (payload.type === "read") {
    await processReceipts([payload.read]);
    await flushAcknowledgements();
  } else if (payload.type === "delivered") {
    await processReceipts([payload.delivery], true);
    await flushAcknowledgements();
  }
}

function connectSocket() {
  if (!pageActive || globalThis.navigator?.onLine === false
      || invitePending || !identity?.token || !identity.publicId
      || (socket && socket.readyState <= WebSocket.OPEN)) return;
  window.clearTimeout(reconnectTimer);
  reconnectTimer = null;
  window.clearTimeout(socketAuthTimer);
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${scheme}//${window.location.host}/ws`, ["sideword.v1"]);
  const user = identity.publicId;
  const token = identity.token;
  clearPresence();
  socket = ws;
  socketAuthTimer = window.setTimeout(() => retrySocket(ws), 10000);
  ws.addEventListener("open", () => {
    if (socket !== ws || identity?.publicId !== user || identity?.token !== token) {
      ws.close();
      return;
    }
    ws.send(JSON.stringify({ type: "auth", token, presence: true }));
    window.clearTimeout(socketAuthTimer);
    socketAuthTimer = window.setTimeout(() => retrySocket(ws), 7000);
    let lastResponse = performance.now();
    ws.addEventListener("message", () => { lastResponse = performance.now(); });
    window.clearInterval(heartbeatTimer);
    heartbeatTimer = window.setInterval(() => {
      if (socket !== ws) return;
      if (performance.now() - lastResponse > 60000) {
        retrySocket(ws);
        return;
      }
      if (ws.readyState === WebSocket.OPEN) ws.send("ping");
    }, 25000);
  });
  ws.addEventListener("message", (event) => {
    if (socket !== ws) return;
    if (event.data === "pong") return;
    if (typeof event.data !== "string" || event.data.length > 4 * 1024 * 1024
        || queuedSocketBytes + event.data.length > 6 * 1024 * 1024 || queuedSocketEvents >= 32) {
      retrySocket(ws);
      return;
    }
    // Presence must not wait behind decryption, storage or HTTP work: doing so
    // could turn an old queued snapshot into an apparently fresh status.
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch {
      clearPresence();
      return;
    }
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) return;
    if (payload.type === "hello") {
      if (payload.user !== user || identity?.publicId !== user) {
        retrySocket(ws);
        showToast("The live connection authenticated a different device. Your local keys and history are kept.", true);
        return;
      }
      window.clearTimeout(socketAuthTimer);
      socketAuthTimer = null;
    }
    if (payload.type === "presence") {
      receivePresence(payload);
      return;
    }
    if (payload.type === "auth_error") clearPresence();
    const bytes = event.data.length;
    queuedSocketBytes += bytes;
    queuedSocketEvents++;
    enqueueIncoming(async () => {
        if (socket !== ws) return;
        try {
          await handleSocketPayload(payload, ws);
        } catch (error) {
          if (payload.type === "hello") retrySocket(ws);
          throw error;
        }
      })
      .catch((error) => showToast(errorMessage(error), true))
      .finally(() => { queuedSocketBytes -= bytes; queuedSocketEvents--; });
  });
  ws.addEventListener("close", () => retrySocket(ws));
  ws.addEventListener("error", () => retrySocket(ws));
}

const refresh = synchronize;

function showNameError() {
  const error = document.querySelector("#display-name-error");
  error.textContent = "Enter your name (1–64 characters).";
  error.style.color = "var(--danger)";
  error.hidden = false;
  elements.displayName.setAttribute("aria-invalid", "true");
  elements.displayName.focus();
}

elements.displayName.addEventListener("invalid", (event) => {
  event.preventDefault();
  showNameError();
});
elements.displayName.addEventListener("input", () => {
  document.querySelector("#display-name-error").hidden = true;
  elements.displayName.setAttribute("aria-invalid", "false");
});

async function prepareInviteResume(token) {
  identity.inviteCredentials ||= {};
  if (!identity.inviteCredentials[token]) {
    identity.inviteCredentials[token] = bytesToBase64(crypto.getRandomValues(new Uint8Array(32)))
      .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  // Persist before sending: even a lost HTTP response must be safe to retry.
  await writeIdentity(identity);
  return identity.inviteCredentials[token];
}

elements.activationForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  elements.activationError.hidden = true;
  elements.roomPassword.setAttribute("aria-invalid", "false");
  const displayName = elements.displayName.value.trim();
  if (!displayName || displayName.length > 64) {
    showNameError();
    return;
  }
  const token = elements.inviteToken.value.trim();
  if (!/^[A-Za-z0-9_-]{20,128}$/.test(token)) {
    showToast("The invite token is missing, incomplete, or contains invalid characters. The room has not been joined.", true);
    return;
  }
  const submitButton = elements.activationForm.querySelector("button[type='submit']");
  submitButton.disabled = true;
  try {
    if (!identity) identity = await generateIdentity();
    await ensureStorageKey();
    const resumeCredential = await prepareInviteResume(token);
    identity.activationCredentials ||= {};
    identity.activationCredentials[token] ||= randomCredential();
    await writeIdentity(identity);
    // Verify a separate read transaction before admission can consume a slot.
    await verifyIdentityPersistence();
    const sessionCredential = identity.activationCredentials[token];
    const previousPublicId = identity.publicId;
    const password = elements.roomPassword.value;
    elements.roomPassword.value = "";
    const result = await api("/api/v1/links/activate", {
      method: "POST",
      body: JSON.stringify({
        token,
        public_key: identity.publicKey,
        display_name: displayName,
        password: password || undefined,
        resume_credential: resumeCredential,
        session_credential: sessionCredential,
      }),
    });
    if (previousPublicId && previousPublicId !== result.user.public_id) {
      // Persisted history/outbox are scoped by public ID. Keep them for a later
      // reconnect; a new invite must never perform a destructive device reset.
      messagesByChat.clear();
      receivedMedia.clear();
      mediaUploadStates.clear();
      seenMessageIds.clear();
      seenReceiptIds.clear();
      pendingReadsByChat.clear();
      pendingDeliveries.clear();
      if (elements.messageDetails.open) elements.messageDetails.close();
      messageMetadata.clear();
      loadedHistoryKeys.clear();
      deliveryReferences.clear();
      acknowledgedDeliveries.clear();
      acknowledgedReads.clear();
      outgoingAttempts.clear();
      messageProfileReady = false;
      visibleMessageNodes.clear();
      messageVisibilityObserver?.disconnect();
      for (const timer of viewTimers.values()) window.clearTimeout(timer);
      viewTimers.clear();
      pendingReceiptIds.clear();
      failedMessageReasons.clear();
    }
    identity.token = result.token;
    identity.refreshCredential = sessionCredential;
    identity.sessionId = result.session_id;
    identity.tokenExpiresAt = Date.parse(result.access_expires_at);
    identity.sessionExpiresAt = Date.parse(result.session_expires_at);
    delete identity.activationCredentials[token];
    delete identity.pendingRefreshCredential;
    identity.suspendedToken = null;
    identity.publicId = result.user.public_id;
    identity.activeInviteToken = token;
    identity.activeInviteChatId = result.chat.id;
    await writeIdentity(identity, true);
    selectedChatId = result.chat.id;
    invitePending = false;
    await loadStoredHistory();
    history.replaceState(null, "", "/client");
    updateIdentityUi();
    await refresh();
    connectSocket();
    elements.clientPanel.setAttribute("data-mobile-view", "conversation");
    selectChat(result.chat.id, mobileWorkspace());
    rememberChatView("conversation", result.chat.id);
    showToast("Invite activated. This device key is stored locally.");
  } catch (error) {
    elements.activationError.textContent = errorMessage(error);
    elements.activationError.hidden = false;
    elements.roomPassword.setAttribute("aria-invalid", "true");
    showToast(errorMessage(error), true);
  } finally {
    submitButton.disabled = false;
  }
});

elements.messageInput.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.shiftKey || event.isComposing
      || event.ctrlKey || event.altKey || event.metaKey) return;
  event.preventDefault();
  if (event.repeat || elements.messageInput.disabled || elements.sendButton.disabled) return;
  elements.messageForm.requestSubmit(elements.sendButton);
});

async function renderOutbox() {
  const panel = document.querySelector("#pending-sends");
  const list = document.querySelector("#pending-send-list");
  if (!panel || !identity?.publicId) return;
  const records = [...await readHistoryRecords(`outbox:${identity.publicId}:`),
    ...await readHistoryRecords(`mediaoutbox:${identity.publicId}:`)];
  const rows = [];
  for (const stored of records) {
    const row = document.createElement("li");
    try {
      const decoded = await crypto.subtle.decrypt({ name: "AES-GCM", iv: stored.value.iv,
        additionalData: encoder.encode(stored.key) }, identity.storageKey, stored.value.ciphertext);
      const record = JSON.parse(decoder.decode(decoded));
      const state = chats.find(chat => chat.id === record.chatId)?.closed_at
        ? "Conversation closed" : record.expiresAt <= Date.now() ? "Send expired"
        : outgoingAttempts.get(metadataKey(record.chatId, record.messageId)) === "failed"
          || mediaFailures.get(stored.key)?.attempts >= 6 ? "Could not send" : null;
      if (!state) continue; // Normal background delivery needs no recovery panel.
      const payload = SidewordProtocol.validatePayload(record.payload);
      row.textContent = `${state}: ${payload.type === "text" ? payload.text : "Attachment"} `;
    } catch { row.textContent = "Could not read a saved message. "; }
    const discard = document.createElement("button");
    discard.type = "button";
    discard.textContent = "Remove saved send";
    discard.addEventListener("click", async () => {
      if (!window.confirm("Discard this saved retry? The message may already have reached the server. This cannot undo delivery.")) return;
      try {
        const lock = stored.key.startsWith("mediaoutbox:") ? "media" : "outbox";
        await deviceLock(`${lock}:${identity.publicId}`, () => removeOutbox(stored.key));
        await renderOutbox();
      } catch (error) { showToast(errorMessage(error), true); }
    });
    row.append(discard);
    rows.push(row);
  }
  list.replaceChildren(...rows);
  panel.hidden = rows.length === 0;
}

document.querySelector("#retry-pending-sends").addEventListener("click", () => {
  mediaFailures.clear();
  for (const entries of messagesByChat.values()) for (const entry of entries) entry.mediaTransfer = null;
  scheduleMediaTransfers();
  void synchronize();
});

async function persistOutbox(record, prefix = "outbox", removeKey = null) {
  if (!identity.storageKey) throw new ClientError("This device is missing the encryption key for its saved outgoing messages.");
  const key = `${prefix}:${identity.publicId}:${record.chatId}:${record.messageId}`;
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv, additionalData: encoder.encode(key) },
    identity.storageKey, encoder.encode(JSON.stringify(record)),
  );
  await putDatabaseValue(key, { iv, ciphertext }, removeKey);
}

async function removeOutbox(key) {
  const database = await openDatabase();
  return new Promise((resolve, reject) => {
    const transaction = database.transaction(STORE_NAME, "readwrite");
    transaction.objectStore(STORE_NAME).delete(key);
    transaction.oncomplete = () => { database.close(); resolve(); };
    transaction.onabort = transaction.onerror = () => {
      database.close(); reject(storageFailure(transaction.error, "The browser could not remove a saved outgoing retry from local storage."));
    };
  });
}

async function flushOutbox() {
  if (invitePending || !identity?.token) return;
  await deviceLock(`outbox:${identity.publicId}`, async () => {
    const records = await readHistoryRecords(`outbox:${identity.publicId}:`);
    for (const stored of records) {
      const plaintext = await crypto.subtle.decrypt({ name: "AES-GCM", iv: stored.value.iv,
        additionalData: encoder.encode(stored.key) }, identity.storageKey, stored.value.ciphertext);
      const record = JSON.parse(decoder.decode(plaintext));
      if (SidewordProtocol.validatePayload(record.payload).type === "attachment") {
        throw new ClientError("This outgoing file requires a media reference.");
      }
      await storeOutgoing(record);
      if (chats.find(chat => chat.id === record.chatId)?.closed_at) continue;
      const attemptKey = metadataKey(record.chatId, record.messageId);
      outgoingAttempts.set(attemptKey, "sending");
      renderMessages();
      try {
        if (chats.find(chat => chat.id === record.chatId)?.participants.some(peer => peer.key_changed)) {
          throw new ClientError("A participant's encryption key changed. The saved send is paused; keep this device's history.");
        }
        if (record.expiresAt <= Date.now()) {
          throw new ClientError("An outgoing retry expired. The message is still saved locally; uploading it now could duplicate a message already delivered.");
        }
        const result = await api(`/api/v1/chats/${record.chatId}/messages`, {
          method: "POST", body: JSON.stringify({ client_message_id: record.messageId,
            envelopes: record.envelopes }),
        });
        await storeAcceptance(record.chatId, record.messageId, result);
        if (record.media) {
          await deviceLock(`outbox-capacity:${identity.publicId}`, () => persistOutbox({
            chatId: record.chatId, messageId: `${record.messageId}.media`,
            payload: record.media, recipients: record.envelopes.map(envelope => envelope.recipient_public_id),
            expiresAt: Math.min(record.expiresAt, receiptTime(result.content_expires_at) || record.expiresAt),
            createdAt: record.createdAt }, "mediaoutbox", stored.key));
          mediaUploadStates.set(metadataKey(record.chatId, record.messageId), "Sending media…");
        } else await removeOutbox(stored.key); // Acceptance/history commit before retry removal.
        outgoingAttempts.delete(attemptKey);
        scheduleMediaTransfers();
      } catch (error) {
        try {
          await appendMessage(record.chatId, { id: `send_failed:${record.messageId}`,
            kind: "send_failed", clientMessageId: record.messageId });
        } catch { /* Keep the original failure and the encrypted outbox retry. */ }
        outgoingAttempts.set(attemptKey, "failed");
        renderMessages();
        throw error;
      }
    }
  });
}

async function storeOutgoing(record) {
  const entry = { id: `outgoing:${record.messageId}`, kind: "mine",
    clientMessageId: record.messageId,
    recipients: record.envelopes.map(envelope => envelope.recipient_public_id),
    senderPublicId: identity.publicId,
    ...payloadEntry(SidewordProtocol.validatePayload(record.payload)), meta: `Sent by ${identity.publicId}`,
    createdAt: record.createdAt };
  entry.meta = messageMeta(record.chatId, entry);
  if (record.media) entry.attachment = SidewordProtocol.validatePayload(record.media);
  await appendMessage(record.chatId, entry);
}

async function storeAcceptance(chatId, messageId, result) {
  const key = await messageEventId("acceptance", [messageId,
    [...(result.recipients || [])].sort(), Object.entries(result.deliveries || {}).sort(),
    result.content_expires_at || null]);
  await appendMessage(chatId, { id: key, kind: "acceptance",
    clientMessageId: messageId, recipients: result.recipients,
    deliveries: result.deliveries || {}, contentExpiresAt: receiptTime(result.content_expires_at), createdAt: receiptTime(result.created_at) || Date.now() });
}

async function recoverMessageStatuses() {
  if (!messageProfileReady || !identity?.receiptRetentionSeconds || !selectedChatId
      || chats.find(chat => chat.id === selectedChatId)?.closed_at) return;
  const entries = (messagesByChat.get(selectedChatId) || []).filter(entry => entry.kind === "mine"
    && Date.now() - historyTimestamp(entry) < identity.receiptRetentionSeconds * 1000
    && !outgoingStatus(selectedChatId, entry).allRead);
  if (!entries.length) return;
  const start = statusPollCursor % entries.length;
  const batch = [...entries.slice(start), ...entries.slice(0, start)].slice(0, 100);
  statusPollCursor += batch.length;
  const chatId = selectedChatId;
  const result = await api(`/api/v1/chats/${chatId}/messages/status`, { method: "POST",
    body: JSON.stringify({ client_message_ids: batch.map(outgoingMessageId) }) });
  for (const message of result.statuses || []) {
    await storeAcceptance(chatId, message.client_message_id, { created_at: message.created_at,
      content_expires_at: message.content_expires_at,
      recipients: message.recipients.map(peer => peer.public_id),
      deliveries: Object.fromEntries(message.recipients.filter(peer => peer.delivery_id)
        .map(peer => [peer.public_id, peer.delivery_id])) });
    for (const peer of message.recipients) {
      if (["expired", "unavailable"].includes(peer.state) && peer.delivery_id) {
        const key = await messageEventId("delivery_state", [chatId, message.client_message_id,
          peer.public_id, peer.delivery_id, peer.state]);
        await appendMessage(chatId, { id: key, kind: "delivery_state",
          clientMessageId: message.client_message_id, recipient: peer.public_id,
          deliveryId: peer.delivery_id, state: peer.state, createdAt: Date.now() });
      }
      for (const [stage, time] of [["delivered", peer.delivered_at], ["read", peer.read_at]]) {
        if (!time) continue;
        const key = await messageEventId("receipt", [chatId, peer.public_id,
          message.client_message_id, peer.delivery_id || null, stage]);
        await appendMessage(chatId, { id: key, kind: "receipt",
          receiptKind: stage, readerPublicId: peer.public_id,
          clientMessageId: message.client_message_id, messageDeliveryId: peer.delivery_id || null,
          confirmedAt: receiptTime(time), createdAt: receiptTime(time) || Date.now() });
      }
    }
  }
}

function payloadEntry(payload) {
  return payload.type === "attachment" || payload.type === "attachment-ref"
    ? { attachment: payload, text: payload.text || "" } : { text: payload.text };
}

function fileIcon() {
  // Fixed decorative geometry only; file content and MIME never enter the DOM.
  const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  for (const [name, value] of Object.entries({viewBox: "0 0 24 24", fill: "none",
    stroke: "currentColor", "stroke-width": "1.5", "stroke-linecap": "round", "stroke-linejoin": "round",
    "aria-hidden": "true", focusable: "false", class: "file-icon"})) {
    icon.setAttribute(name, value);
  }
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z M14 2v6h6 M8 13h8 M8 17h5");
  icon.append(path);
  return icon;
}

function downloadIcon() {
  const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  for (const [name, value] of Object.entries({viewBox: "0 0 24 24", fill: "none",
    stroke: "currentColor", "stroke-width": "1.75", "stroke-linecap": "round",
    "stroke-linejoin": "round", "aria-hidden": "true", focusable: "false"})) {
    icon.setAttribute(name, value);
  }
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", "M12 3v12 m-5-5 5 5 5-5 M5 16v4h14v-4");
  icon.append(path);
  return icon;
}

function fileSize(size) {
  return size === 1048576 ? "1 MiB" : size >= 1024 ? `${(size / 1024).toFixed(1)} KiB` : `${size} B`;
}

function composerError(message = "") {
  elements.composerError.textContent = message;
  elements.composerError.hidden = !message;
}

function clearAttachment() {
  selectedFile = null;
  elements.attachmentInput.value = "";
  elements.attachmentPreview.hidden = true;
  elements.attachmentLabel.textContent = "";
  elements.attachmentSize.textContent = "";
}

function selectAttachment(file) {
  clearAttachment();
  composerError();
  if (!file) return;
  if (file.size > SidewordProtocol.MAX_FILE_BYTES) {
    throw new ClientError("Files must be 1 MiB (1,048,576 bytes) or smaller.");
  }
  if (!file.name || file.name.length > 255 || file.type.length > 127) {
    throw new ClientError("The filename or declared file type is too long.");
  }
  selectedFile = file;
  elements.attachmentLabel.textContent = SidewordProtocol.safeDownloadName(file.name);
  elements.attachmentLabel.title = elements.attachmentLabel.textContent;
  elements.attachmentSize.textContent = `${fileSize(file.size)} · File attachment`;
  elements.attachmentPreview.hidden = false;
  // Keep the text composer available for an optional message above the media.
}

function stageAttachmentFiles(files) {
  if (sendingMessage || elements.attachmentButton.disabled) {
    composerError(sendingMessage ? "Wait for the current message to finish sending."
      : "File attachment is unavailable. Select an active conversation and resolve any access or key warnings.");
    return;
  }
  if (files.length !== 1) {
    composerError(files.length ? "Attach one file at a time. Choose a single file."
      : "No file was available. Copy or drag a file, or use the attachment button.");
    return;
  }
  try { selectAttachment(files[0]); }
  catch (error) { composerError(errorMessage(error)); }
  selectChat(selectedChatId);
  if (selectedFile && !elements.sendButton.disabled) elements.sendButton.focus();
}

function hasTransferFiles(transfer) {
  return Boolean(transfer?.files?.length || Array.from(transfer?.types || []).includes("Files"));
}

elements.attachmentButton.addEventListener("click", () => elements.attachmentInput.click());
elements.attachmentInput.addEventListener("change", () => {
  if (sendingMessage || !elements.attachmentInput.files?.[0]) return;
  stageAttachmentFiles(elements.attachmentInput.files);
});
// Read only files supplied by the user's paste/drop event. Never read clipboard
// permissions, HTML, remote URLs or file contents here; sending owns file reads.
elements.conversation.addEventListener("paste", event => {
  if (!hasTransferFiles(event.clipboardData)) return; // Preserve ordinary text paste.
  event.preventDefault();
  stageAttachmentFiles(event.clipboardData.files || []);
});
elements.conversation.addEventListener("dragover", event => {
  if (!hasTransferFiles(event.dataTransfer)) return;
  event.preventDefault(); // Prevent browser navigation, including when sending is blocked.
  event.dataTransfer.dropEffect = !sendingMessage && !elements.attachmentButton.disabled ? "copy" : "none";
  elements.conversation.classList.toggle("file-drag-over", event.dataTransfer.dropEffect === "copy");
});
elements.conversation.addEventListener("dragleave", event => {
  if (event.relatedTarget && elements.conversation.contains(event.relatedTarget)) return;
  elements.conversation.classList.toggle("file-drag-over", false);
});
elements.conversation.addEventListener("dragend", () => elements.conversation.classList.toggle("file-drag-over", false));
elements.conversation.addEventListener("drop", event => {
  elements.conversation.classList.toggle("file-drag-over", false);
  if (!hasTransferFiles(event.dataTransfer)) return;
  event.preventDefault();
  stageAttachmentFiles(event.dataTransfer.files || []);
});
elements.attachmentCancel.addEventListener("click", () => {
  if (sendingMessage) return;
  clearAttachment();
  composerError();
  selectChat(selectedChatId);
  if (!elements.messageInput.disabled) elements.messageInput.focus();
});

function releaseDownload(url) {
  if (!downloadUrls.has(url)) return;
  window.clearTimeout(downloadUrls.get(url));
  URL.revokeObjectURL(url);
  downloadUrls.delete(url);
}

function downloadAttachment(payload, errorNode) {
  let url;
  let link;
  try {
    errorNode.textContent = "";
    SidewordProtocol.validatePayload(payload);
    if (downloadUrls.size >= 8) throw new ClientError("Please wait before starting another download.");
    const bytes = SidewordProtocol.attachmentBytes(payload);
    url = URL.createObjectURL(new Blob([bytes], { type: "application/octet-stream" }));
    downloadUrls.set(url, null);
    link = document.createElement("a");
    link.href = url;
    link.download = SidewordProtocol.safeDownloadName(payload.name);
    link.hidden = true;
    document.body.append(link);
    link.click();
    // Keep the URL alive for navigation/download handoff, including slower engines.
    downloadUrls.set(url, window.setTimeout(() => releaseDownload(url), 60000));
  } catch (error) {
    if (url) releaseDownload(url);
    errorNode.textContent = error instanceof ClientError ? error.message
      : "The file could not be downloaded. Its saved copy is retained; try again.";
  } finally { link?.remove(); }
}

elements.messageForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = elements.messageInput.value.trim();
  const chat = chats.find((candidate) => candidate.id === selectedChatId);
  const file = selectedFile;
  if (sendingMessage || (!text && !file) || !chat || chat.closed_at) return;
  sendingMessage = true;
  elements.sendButton.disabled = true;
  elements.attachmentButton.disabled = true;
  elements.attachmentInput.disabled = true;
  elements.attachmentCancel.disabled = true;
  elements.sendButton.textContent = "Sending…";
  composerError();
  try {
    const messageId = crypto.randomUUID();
    const recipients = chat.participants.filter((item) => item.public_id !== identity.publicId);
    if (!recipients.length || recipients.length > 100) throw new ClientError("This conversation exceeds the recipient limit or has no peers.");
    // Verify every peer before reading the file or starting expensive encryption.
    for (const recipient of recipients) await assertTrustedPeer(recipient);
    if (file && (file.size > SidewordProtocol.MAX_FILE_BYTES
        || SidewordProtocol.attachmentSendBytes(file.size, recipients.length) > SidewordProtocol.MAX_SEND_BYTES)) {
      throw new ClientError("This file exceeds the file or group transport limit. Use a smaller file or fewer recipients.");
    }
    const payload = file ? SidewordProtocol.fileReference(file.name, file.type, file.size, `${messageId}.media`, text)
      : SidewordProtocol.textPayload(text);
    const plaintext = SidewordProtocol.encodePayload(payload);
    const envelopes = [];
    let wireBytes = 512;
    // Sequential encryption bounds transient buffers independently of group size.
    for (const recipient of recipients) {
      const ciphertext = await encryptForRecipient(plaintext, chat.id, messageId, recipient);
      wireBytes += ciphertext.length + 512;
      if (wireBytes > SidewordProtocol.MAX_SEND_BYTES) throw new ClientError("This message exceeds the group transport limit.");
      envelopes.push({ recipient_public_id: recipient.public_id, ciphertext });
    }
    if (encoder.encode(JSON.stringify({ client_message_id: messageId, envelopes })).length > SidewordProtocol.MAX_SEND_BYTES) {
      throw new ClientError("This message exceeds the group transport limit. Use a smaller message or fewer recipients.");
    }
    if (!identity.retryWindowSeconds) throw new ClientError("The server does not advertise a safe message retry period. This message was not uploaded.");
    const record = { chatId: chat.id, messageId, envelopes, payload,
      expiresAt: Date.now() + Math.max(0, identity.retryWindowSeconds - 300) * 1000,
      createdAt: Date.now() };
    if (file) record.media = SidewordProtocol.filePayload(file.name, file.type, new Uint8Array(await file.arrayBuffer()));
    await deviceLock(`outbox:${identity.publicId}`, async () => {
      await deviceLock(`outbox-capacity:${identity.publicId}`, async () => {
        const records = [...await readHistoryRecords(`outbox:${identity.publicId}:`),
          ...await readHistoryRecords(`mediaoutbox:${identity.publicId}:`)];
        const bytes = records.reduce((sum, item) => sum + item.value.ciphertext.byteLength, 0);
        if (records.length >= 100 || bytes + encoder.encode(JSON.stringify(record)).length + 16
            > (file ? 16 : 32) * 1024 * 1024) {
          throw new ClientError("The encrypted local outbox is full. Retry or discard pending sends before adding another message.");
        }
        await persistOutbox(record);
      });
      // Ownership transfers to the durable outbox even if subsequent history persistence fails.
      if (file) clearAttachment();
      elements.messageInput.value = "";
      await storeOutgoing(record);
    });
    try { await flushOutbox(); }
    finally { await renderOutbox(); }
  } catch (error) {
    composerError(errorMessage(error));
  } finally {
    sendingMessage = false;
    elements.attachmentCancel.disabled = false;
    selectChat(selectedChatId);
    if (!elements.messageInput.disabled) elements.messageInput.focus({ preventScroll: true });
  }
});

elements.refreshButton.addEventListener("click", refresh);
document.querySelector("#reconnect-session").addEventListener("click", async (event) => {
  if (invitePending || !identity?.suspendedToken) return;
  event.target.disabled = true;
  identity.token = identity.suspendedToken;
  try {
    await loadChats();
    identity.suspendedToken = null;
    await writeIdentity(identity);
    updateIdentityUi();
    connectSocket();
    await synchronize();
  } catch (error) {
    identity.token = null;
    await writeIdentity(identity);
    updateIdentityUi();
    showToast(errorMessage(error), true);
  } finally {
    event.target.disabled = false;
  }
});
elements.resetButton.addEventListener("click", async () => {
  const confirmed = window.confirm(
    "Remove this browser's private key and session? Existing chats cannot be recovered on this device.",
  );
  if (!confirmed) return;
  try {
    await clearIdentity();
    window.location.assign("/client");
  } catch (error) {
    showToast(errorMessage(error), true);
  }
});

async function start() {
  if (!window.isSecureContext) throw new ClientError("This page is not running in a secure browser context. Chat requires HTTPS or localhost.");
  if (!window.crypto?.subtle) throw new ClientError("The browser's encryption features are unavailable, so this client cannot create or use chat keys.");
  if (!window.indexedDB) throw new ClientError("The browser's local database is unavailable, so this client cannot save device keys or chat history.");
  identity = await readIdentity();
  const requestedInvite = (elements.inviteToken.value || "").trim();
  invitePending = Boolean(requestedInvite && requestedInvite !== identity?.activeInviteToken);
  if (!invitePending) restoreChatView();
  await ensureStorageKey();
  if (!invitePending) await loadStoredHistory();
  if (!invitePending && identity?.token) {
    await refresh({ initial: true });
  }
  // Apply the first chat snapshot before revealing the workspace. Failed refreshes
  // still reveal saved local history.
  if (!elements.loadingPanel.hidden) updateIdentityUi();
  if (!invitePending && selectedChatId) renderMessages(true);
  if (!invitePending && identity?.token) connectSocket();
  window.setInterval(synchronize, POLL_INTERVAL_MS);
  window.setInterval(updateSessionCountdown, 1000);
}

function formatTimeRemaining(milliseconds) {
  const total = Math.max(0, Math.ceil(milliseconds / 1000));
  const days = Math.floor(total / 86400);
  const hours = Math.floor(total / 3600) % 24;
  const minutes = Math.floor(total / 60) % 60;
  const seconds = total % 60;
  const clock = [hours, minutes, seconds].map(value => String(value).padStart(2, "0")).join(":");
  return `${days ? `${days}d ` : ""}${clock}`;
}

function updateSessionCountdown() {
  const element = document.querySelector("#session-countdown");
  if (!element) return;
  element.hidden = invitePending || !identity?.token || accessDeadline === null;
  if (element.hidden) return;
  const remaining = accessDeadline - performance.now();
  element.textContent = `Session expires in ${formatTimeRemaining(remaining)}`;
  if (remaining <= 0) {
    accessDeadline = null;
    void invalidateSession("Session expired").catch(error => showToast(errorMessage(error), true));
  }
}

window.addEventListener("online", () => {
  connectSocket();
  void synchronize();
});
window.addEventListener("offline", disconnectSocket);
window.addEventListener("pagehide", () => {
  mediaPreviews?.clear();
  stopMediaTransfers();
  for (const url of downloadUrls.keys()) releaseDownload(url);
  pageActive = false;
  disconnectSocket();
});
window.addEventListener("pageshow", (event) => {
  pageActive = true;
  scheduleVisiblePreviews();
  if (event.persisted) {
    connectSocket();
    void synchronize();
  }
});
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") mediaPreviews?.clear();
  refreshVisibleReads();
  if (document.visibilityState === "visible") {
    scheduleVisiblePreviews();
    // Timers can be suspended in background tabs. Expired leases remain unknown
    // until a fresh snapshot arrives; request one on the existing socket.
    renderPresence();
    if (socket?.readyState === WebSocket.OPEN) socket.send("ping");
    connectSocket();
    void synchronize();
  }
});
window.addEventListener("focus", refreshVisibleReads);
window.addEventListener("blur", () => {
  for (const timer of viewTimers.values()) window.clearTimeout(timer);
  viewTimers.clear();
});
window.addEventListener("resize", () => {
  scheduleVisiblePreviews();
  refreshVisibleReads();
  positionMessageDetails();
});

function showStartupError(error) {
  elements.loadingPanel.hidden = true;
  elements.setupPanel.hidden = false;
  elements.clientPanel.hidden = true;
  elements.activationError.textContent = errorMessage(error);
  elements.activationError.hidden = false;
  showToast(errorMessage(error), true);
}

start().catch(showStartupError);
