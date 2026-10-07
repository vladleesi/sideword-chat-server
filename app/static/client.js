"use strict";

// New test protocol: keep prior device/history records untouched in their old DB.
const DB_NAME = "sideword-test-client-p256";
const STORE_NAME = "device";
const IDENTITY_KEY = "identity";
const HISTORY_PREFIX = "history:";
const POLL_INTERVAL_MS = 4000;
const encoder = new TextEncoder();
const decoder = new TextDecoder("utf-8", { fatal: true });

const elements = {
  activationForm: document.querySelector("#activation-form"),
  chatList: document.querySelector("#chat-list"),
  clientPanel: document.querySelector("#client-panel"),
  connectionState: document.querySelector("#connection-state"),
  conversationKind: document.querySelector("#conversation-kind"),
  conversationTitle: document.querySelector("#conversation-title"),
  displayName: document.querySelector("#display-name"),
  error: document.querySelector("#error-message"),
  identityLabel: document.querySelector("#identity-label"),
  inviteToken: document.querySelector("#invite-token"),
  roomPassword: document.querySelector("#join-password"),
  activationError: document.querySelector("#activation-error"),
  messageForm: document.querySelector("#message-form"),
  messageInput: document.querySelector("#message-input"),
  messageList: document.querySelector("#message-list"),
  participantCount: document.querySelector("#participant-count"),
  participantKeys: document.querySelector("#participant-keys"),
  refreshButton: document.querySelector("#refresh-button"),
  resetButton: document.querySelector("#reset-button"),
  sendButton: document.querySelector("#send-button"),
  setupPanel: document.querySelector("#setup-panel"),
  status: document.querySelector("#status-message"),
};

let identity = null;
let chats = [];
let selectedChatId = null;
let invitePending = false;
let synchronizing = false;
let socket = null;
let reconnectTimer = null;
let heartbeatTimer = null;
let socketAuthTimer = null;
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
const pendingReceiptIds = new Map();

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

async function putDatabaseValue(key, value) {
  const database = await openDatabase();
  return new Promise((resolve, reject) => {
    const transaction = database.transaction(STORE_NAME, "readwrite");
    transaction.objectStore(STORE_NAME).put(value, key);
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

async function persistHistoryEntry(chatId, entry) {
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
  await putDatabaseValue(key, { iv, ciphertext });
}

async function loadStoredHistory() {
  if (!identity?.storageKey || !identity.publicId) return;
  const records = await readHistoryRecords();
  for (const record of records) {
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
      appendMessage(chatId, JSON.parse(decoder.decode(plaintext)), false);
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
  window.setTimeout(() => {
    target.hidden = true;
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
    "room sealed; reconnect with your saved session": "This room is full or closed to new devices. Joining again cannot recover an existing participant slot.",
    "This room uses retired encryption. Create a new invite.": "This room was created with the old test encryption and cannot accept P-256 devices.",
    "The room phrase or password is incorrect.": "The room password does not match, or a required password was left empty.",
    "Too many failed attempts. Try again later.": "Password checks for this room are temporarily blocked after too many failed attempts.",
    "public_key mismatch with existing session": "Your saved login belongs to a different encryption key than this device is using.",
    "public_key mismatch with existing participant": "The saved room participant uses a different encryption key than this device.",
    "This device has joined. Use its saved session or resume credential.": "This device already claimed a room slot, but its saved login or recovery credential is missing.",
    "personal chat has no peer yet": "The other participant has not joined this room yet.",
    "message identity already has a different payload": "This message ID was already used for different encrypted data. The new upload was rejected.",
    "message predates retry ledger; verify delivery": "The service cannot safely retry this older message because its original delivery record is missing.",
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

async function fetchClient(path, options) {
  try {
    return await fetch(path, options);
  } catch {
    throw new ClientError("No response was received from the chat service. The network connection failed or the service is unavailable; the server may already have processed the request.");
  }
}

async function responseData(response) {
  try {
    return await response.json();
  } catch {
    throw new ClientError("The chat service returned a response this client could not read.");
  }
}

class SessionExpiredError extends ClientError {}

async function invalidateSession(reason = "Session expired") {
  if (!identity) return;
  if (identity.token) identity.suspendedToken = identity.token;
  identity.token = null;
  accessDeadline = null;
  updateSessionCountdown();
  await writeIdentity(identity, true);
  chats = [];
  selectedChatId = null;
  window.clearTimeout(reconnectTimer);
  reconnectTimer = null;
  window.clearInterval(heartbeatTimer);
  heartbeatTimer = null;
  window.clearTimeout(socketAuthTimer);
  socketAuthTimer = null;
  const activeSocket = socket;
  socket = null;
  if (activeSocket && activeSocket.readyState <= WebSocket.OPEN) activeSocket.close();
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
    const migrating = !identity.refreshCredential;
    identity.pendingRefreshCredential ||= randomCredential();
    await writeIdentity(identity, true); // Keep the proposed rotation if the response/save is lost.
    const response = await fetchClient(migrating ? "/api/v1/sessions" : "/api/v1/sessions/refresh", {
      method: "POST", cache: "no-store",
      headers: { "Content-Type": "application/json", ...(migrating
        ? { Authorization: `Bearer ${identity.token}` } : {}) },
      body: JSON.stringify(migrating ? { credential: identity.pendingRefreshCredential } : {
        credential: identity.refreshCredential, next_credential: identity.pendingRefreshCredential,
      }),
    });
    if (!response.ok) {
      if (response.status === 401) await invalidateSession("The server rejected the saved login (HTTP 401); it has expired or was disabled");
      throw requestError(response.status, undefined, "Could not renew the saved login");
    }
    const result = await responseData(response);
    identity.token = result.token;
    identity.sessionId = result.session_id;
    identity.tokenExpiresAt = Date.parse(result.access_expires_at);
    identity.sessionExpiresAt = Date.parse(result.session_expires_at);
    identity.refreshCredential = identity.pendingRefreshCredential;
    delete identity.pendingRefreshCredential;
    await writeIdentity(identity, true);
    if (socket) { const old = socket; socket = null; old.close(); }
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
    let detail;
    try {
      const body = await response.json();
      detail = body?.detail;
    } catch {
      // A proxy may return HTML; explain its HTTP status without displaying it.
    }
    if (response.status === 401) {
      await invalidateSession("The server rejected the saved login (HTTP 401); it has expired or was disabled");
    }
    throw requestError(response.status, detail, activation ? "Could not join the room" : "Chat request failed");
  }
  return responseData(response);
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
  entries.push(entry);
  entries.sort(compareHistoryEntries);
  messagesByChat.set(chatId, entries);
  if (selectedChatId === chatId) renderMessages();
}

function serverTimestamp(value) {
  const utc = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}Z`;
  return Date.parse(utc) || Date.now();
}

function messageKey(message) {
  return `incoming:${JSON.stringify([message.chat_id, message.sender_public_id, message.client_message_id])}`;
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

function messageMeta(chatId, entry) {
  const chat = chats.find((candidate) => candidate.id === chatId);
  if (entry.kind !== "theirs" || chat?.chat_type !== "group") return entry.meta;
  let senderPublicId = entry.senderPublicId;
  if (!senderPublicId && typeof entry.id === "string" && entry.id.startsWith("incoming:")) {
    try {
      const [storedChatId, storedSenderId] = JSON.parse(entry.id.slice("incoming:".length));
      if (storedChatId === chatId) senderPublicId = storedSenderId;
    } catch {
      // Older history without a routing identity keeps its saved label.
    }
  }
  const sender = chat.participants.find((participant) => participant.public_id === senderPublicId);
  if (!sender) return entry.meta;
  const name = sender.display_name?.trim();
  return name ? `From ${name} (${senderPublicId})` : `From ${senderPublicId}`;
}

function renderMessages() {
  if (elements.clientPanel.hidden) return;
  const list = elements.messageList;
  const changedChat = renderedChatId !== selectedChatId;
  const followLatest = changedChat || list.scrollHeight - list.clientHeight - list.scrollTop <= 32;
  if (changedChat) {
    list.replaceChildren();
    renderedMessages.clear();
    renderedChatId = selectedChatId;
  }
  const entries = [...(messagesByChat.get(selectedChatId) || [])].sort(compareHistoryEntries);
  if (!entries.length) {
    if (list.firstChild && !renderedMessages.size) return;
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
      node.remove();
      renderedMessages.delete(key);
    }
  }
  let position = 0;
  for (const entry of entries) {
    const key = entry.id || entry;
    const metaText = messageMeta(selectedChatId, entry);
    const existing = renderedMessages.get(key);
    if (existing) {
      if (existing.children[1]) existing.children[1].textContent = metaText || "";
      if (list.children[position] !== existing) list.insertBefore(existing, list.children[position] || null);
      position++;
      continue;
    }
    const message = document.createElement("div");
    message.className = `message ${entry.kind}`;
    const text = document.createElement("span");
    text.textContent = entry.text;
    message.append(text);
    if (metaText) {
      const meta = document.createElement("span");
      meta.className = "message-meta";
      meta.textContent = metaText;
      message.append(meta);
    }
    list.insertBefore(message, list.children[position] || null);
    renderedMessages.set(key, message);
    position++;
  }
  if (followLatest) list.scrollTop = list.scrollHeight;
}

function chatName(chat) {
  if (chat.title) return chat.title;
  const peers = chat.participants.filter((participant) => participant.public_id !== identity.publicId);
  return peers.map((peer) => peer.display_name || peer.public_id).join(", ") || "Waiting for peer";
}

function renderChats() {
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
    button.setAttribute("role", "listitem");
    const title = document.createElement("strong");
    title.textContent = chatName(chat);
    const detail = document.createElement("span");
    detail.textContent = `${chat.chat_type} · ${chat.participants.length} participant${chat.participants.length === 1 ? "" : "s"}`;
    button.append(title, detail);
    button.addEventListener("click", () => selectChat(chat.id));
    elements.chatList.append(button);
  }
}

function selectChat(chatId) {
  const changedChat = renderedChatId !== chatId;
  selectedChatId = chatId;
  const chat = chats.find((candidate) => candidate.id === chatId);
  elements.conversationKind.textContent = chat?.chat_type || "Conversation";
  elements.conversationTitle.textContent = chat ? chatName(chat) : "No conversation selected";
  elements.participantCount.textContent = chat ? `${chat.participants.length} participants` : "";
  elements.participantKeys.textContent = chat
    ? chat.participants
        .map((item) => {
          const label = item.public_id === identity.publicId ? " (this device)" : "";
          const warning = item.key_changed ? " — KEY CHANGED; blocked" : "";
          return `${item.public_id}${label}: ${item.local_fingerprint || "not checked"}${warning}`;
        })
        .join(" · ")
    : "";
  const canSend = Boolean(chat && !chat.participants.some(item => item.key_changed)
    && chat.participants.some((item) => item.public_id !== identity.publicId));
  elements.messageInput.disabled = !canSend;
  elements.sendButton.disabled = !canSend;
  renderChats();
  renderMessages();
  if (canSend && changedChat) elements.messageInput.focus({ preventScroll: true });
}

function updateIdentityUi() {
  const active = Boolean(!invitePending && identity?.token && identity?.publicId);
  elements.setupPanel.hidden = active;
  elements.clientPanel.hidden = !active;
  document.querySelector("#reconnect-session").hidden = invitePending || active || !identity?.suspendedToken;
  updateConnectionState(false);
  if (active) renderMessages();
}

function updateConnectionState(connected) {
  const active = Boolean(!invitePending && identity?.token && identity?.publicId);
  elements.connectionState.classList.toggle("online", active && connected);
  elements.identityLabel.textContent = active
    ? `Device ${identity.publicId} · ${connected ? "Live" : "Polling"}`
    : "No local identity";
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
  await writeIdentity(identity);
  chats = checkedChats;
  if (selectedChatId && !chats.some((chat) => chat.id === selectedChatId)) selectedChatId = null;
  if (!selectedChatId && chats.length) selectedChatId = chats[0].id;
  if (selectedChatId) selectChat(selectedChatId);
  else renderChats();
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
  const reference = deliveryReference(message, "sender_public_id");
  const ids = pendingReadsByChat.get(message.chat_id) || new Map();
  ids.set(message.delivery_id, reference);
  pendingReadsByChat.set(message.chat_id, ids);
}

async function flushDeliveryBatch(path, field, pending) {
  // Keep each failed batch queued. Delete only the successful snapshot so a
  // delivery added during the request cannot be lost.
  while (pending.size) {
    const batch = [...pending.values()].slice(0, 100);
    await api(path, {
      method: "POST",
      body: JSON.stringify({ [field]: batch }),
    });
    for (const reference of batch) pending.delete(reference.delivery_id);
  }
}

async function flushAcknowledgements() {
  for (const [chatId, ids] of pendingReadsByChat) {
    await flushDeliveryBatch(`/api/v1/chats/${chatId}/read/exact`, "messages", ids);
    pendingReadsByChat.delete(chatId);
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
      queueRead(message);
      continue;
    }
    if (processingMessageIds.has(key)) continue;
    processingMessageIds.add(key);
    try {
      const plaintext = await decryptMessage(message);
      const entry = {
        id: key,
        kind: "theirs",
        text: plaintext,
        senderPublicId: message.sender_public_id,
        meta: `From ${message.sender_public_id}`,
        createdAt: serverTimestamp(message.created_at),
      };
      entry.meta = messageMeta(message.chat_id, entry);
      await appendMessage(message.chat_id, entry);
      seenMessageIds.add(key);
      failedMessageReasons.delete(key);
      queueRead(message);
    } catch (error) {
      const reason = errorMessage(error);
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

async function processReceipts(receipts) {
  for (const receipt of receipts) {
    const reference = deliveryReference(receipt, "reader_public_id");
    const key = `receipt:${JSON.stringify([receipt.chat_id, receipt.reader_public_id, receipt.client_message_id])}`;
    if (seenReceiptIds.has(key)) {
      pendingReceiptIds.set(receipt.delivery_id, reference);
      continue;
    }
    await appendMessage(receipt.chat_id, {
      id: key,
      kind: "system",
      text: `Message ${receipt.client_message_id} was read by ${receipt.reader_public_id}.`,
      createdAt: serverTimestamp(receipt.created_at),
    });
    seenReceiptIds.add(key);
    pendingReceiptIds.set(receipt.delivery_id, reference);
  }
}

async function processIncoming(data) {
  await processMessages(data.messages || []);
  await processReceipts(data.read_receipts || []);
  await flushAcknowledgements();
}

async function synchronize() {
  if (invitePending || !identity?.token || synchronizing) return;
  synchronizing = true;
  try {
    await loadChats();
    try { await flushOutbox(); } catch (error) { showToast(errorMessage(error), true); }
    await renderOutbox();
    const data = await api("/api/v1/poll");
    await enqueueIncoming(() => processIncoming(data));
  } catch (error) {
    showToast(errorMessage(error), true);
  } finally {
    synchronizing = false;
  }
}

function scheduleReconnect() {
  if (invitePending || reconnectTimer || !identity?.token) return;
  reconnectTimer = window.setTimeout(() => {
    reconnectTimer = null;
    connectSocket();
  }, 2000);
}

async function handleSocketPayload(payload) {
  if (payload.type === "auth_error") {
    if (identity?.refreshCredential) {
      await ensureFreshSession(true);
      connectSocket();
      return;
    }
    await invalidateSession("The saved session is no longer valid");
  } else if (payload.type === "hello") {
    updateConnectionState(true);
    await loadChats();
    await processIncoming(payload.backlog || {});
  } else if (payload.type === "message") {
    await processMessages([payload.message]);
    await flushAcknowledgements();
  } else if (payload.type === "read") {
    await processReceipts([payload.read]);
    await flushAcknowledgements();
  }
}

function connectSocket() {
  if (invitePending || !identity?.token || (socket && socket.readyState <= WebSocket.OPEN)) return;
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${scheme}//${window.location.host}/ws`, ["sideword.v1"]);
  socket = ws;
  ws.addEventListener("open", () => {
    if (socket !== ws || !identity?.token) {
      ws.close();
      return;
    }
    ws.send(JSON.stringify({ type: "auth", token: identity.token }));
    window.clearTimeout(socketAuthTimer);
    socketAuthTimer = window.setTimeout(() => {
      if (socket === ws) ws.close();
    }, 7000);
    let lastResponse = Date.now();
    ws.addEventListener("message", () => { lastResponse = Date.now(); });
    window.clearInterval(heartbeatTimer);
    heartbeatTimer = window.setInterval(() => {
      if (Date.now() - lastResponse > 60000) {
        ws.close();
        return;
      }
      if (socket === ws && ws.readyState === WebSocket.OPEN) ws.send("ping");
    }, 25000);
  });
  ws.addEventListener("message", (event) => {
    if (socket !== ws) return;
    if (event.data === "pong") return;
    enqueueIncoming(async () => {
        if (socket !== ws) return;
        const payload = JSON.parse(event.data);
        if (payload.type === "hello") {
          window.clearTimeout(socketAuthTimer);
          socketAuthTimer = null;
        }
        await handleSocketPayload(payload);
      })
      .catch((error) => showToast(errorMessage(error), true));
  });
  ws.addEventListener("close", () => {
    if (socket !== ws) return;
    window.clearTimeout(socketAuthTimer);
    socketAuthTimer = null;
    window.clearInterval(heartbeatTimer);
    heartbeatTimer = null;
    socket = null;
    updateConnectionState(false);
    scheduleReconnect();
  });
  ws.addEventListener("error", () => ws.close());
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
      seenMessageIds.clear();
      seenReceiptIds.clear();
      pendingReadsByChat.clear();
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
    selectChat(result.chat.id);
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
  if (event.key !== "Enter" || !event.shiftKey || event.isComposing
      || event.ctrlKey || event.altKey || event.metaKey) return;
  event.preventDefault();
  if (event.repeat || elements.messageInput.disabled || elements.sendButton.disabled) return;
  elements.messageForm.requestSubmit(elements.sendButton);
});

async function renderOutbox() {
  const panel = document.querySelector("#pending-sends");
  const list = document.querySelector("#pending-send-list");
  if (!panel || !identity?.publicId) return;
  const records = await readHistoryRecords(`outbox:${identity.publicId}:`);
  const rows = [];
  for (const stored of records) {
    const row = document.createElement("li");
    try {
      const decoded = await crypto.subtle.decrypt({ name: "AES-GCM", iv: stored.value.iv,
        additionalData: encoder.encode(stored.key) }, identity.storageKey, stored.value.ciphertext);
      const record = JSON.parse(decoder.decode(decoded));
      row.textContent = `${record.expiresAt <= Date.now() ? "Retry expired" : "Pending"}: ${record.plaintext} `;
    } catch { row.textContent = "Unreadable pending message. "; }
    const discard = document.createElement("button");
    discard.type = "button";
    discard.textContent = "Discard retry";
    discard.addEventListener("click", async () => {
      if (!window.confirm("Discard this saved retry? The message may already have reached the server. This cannot undo delivery.")) return;
      try {
        await deviceLock(`outbox:${identity.publicId}`, () => removeOutbox(stored.key));
        await renderOutbox();
      } catch (error) { showToast(errorMessage(error), true); }
    });
    row.append(discard);
    rows.push(row);
  }
  list.replaceChildren(...rows);
  panel.hidden = records.length === 0;
}

document.querySelector("#retry-pending-sends").addEventListener("click", () => {
  void synchronize();
});

async function persistOutbox(record) {
  if (!identity.storageKey) throw new ClientError("This device is missing the encryption key for its saved outgoing messages.");
  const key = `outbox:${identity.publicId}:${record.chatId}:${record.messageId}`;
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv, additionalData: encoder.encode(key) },
    identity.storageKey, encoder.encode(JSON.stringify(record)),
  );
  await putDatabaseValue(key, { iv, ciphertext });
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
      if (record.expiresAt <= Date.now()) {
        throw new ClientError("An outgoing retry expired. The message is still saved locally; uploading it now could duplicate a message already delivered.");
      }
      const result = await api(`/api/v1/chats/${record.chatId}/messages`, {
        method: "POST", body: JSON.stringify({ client_message_id: record.messageId,
          envelopes: record.envelopes }),
      });
      await appendMessage(record.chatId, { id: `outgoing:${record.messageId}`, kind: "mine",
        text: record.plaintext, meta: `Sent ? ${record.messageId.slice(0, 8)}`,
        createdAt: serverTimestamp(result.created_at) });
      await removeOutbox(stored.key); // History must commit before removing retry state.
    }
  });
}

elements.messageForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const plaintext = elements.messageInput.value.trim();
  const chat = chats.find((candidate) => candidate.id === selectedChatId);
  if (!plaintext || !chat) return;
  elements.sendButton.disabled = true;
  try {
    const messageId = crypto.randomUUID();
    const recipients = chat.participants.filter((item) => item.public_id !== identity.publicId);
    const envelopes = await Promise.all(
      recipients.map(async (recipient) => ({
        recipient_public_id: recipient.public_id,
        ciphertext: await encryptForRecipient(plaintext, chat.id, messageId, recipient),
      })),
    );
    if (!identity.retryWindowSeconds) throw new ClientError("The server does not advertise a safe message retry period. This message was not uploaded.");
    const record = { chatId: chat.id, messageId, envelopes, plaintext,
      expiresAt: Date.now() + Math.max(0, identity.retryWindowSeconds - 300) * 1000,
      createdAt: Date.now() };
    await deviceLock(`outbox:${identity.publicId}`, async () => {
      if ((await readHistoryRecords(`outbox:${identity.publicId}:`)).length >= 100) {
        throw new ClientError("This device has too many saved outgoing messages awaiting confirmation. Another message cannot be added to the local outbox.");
      }
      await persistOutbox(record);
    });
    elements.messageInput.value = "";
    try {
      await flushOutbox();
    } finally {
      await renderOutbox();
    }
  } catch (error) {
    showToast(errorMessage(error), true);
  } finally {
    elements.sendButton.disabled = false;
    elements.messageInput.focus({ preventScroll: true });
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
  if (!invitePending) selectedChatId = identity?.activeInviteChatId || null;
  await ensureStorageKey();
  if (!invitePending) await loadStoredHistory();
  updateIdentityUi();
  if (!invitePending && identity?.token) {
    await refresh();
    connectSocket();
  }
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
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    connectSocket();
    void synchronize();
  }
});

function showStartupError(error) {
  elements.activationError.textContent = errorMessage(error);
  elements.activationError.hidden = false;
  showToast(errorMessage(error), true);
}

start().catch(showStartupError);
