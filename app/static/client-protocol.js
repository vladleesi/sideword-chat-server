"use strict";

// P-256 test wire protocol. No DOM, storage, network, or implicit identity state.
// See docs/PROTOCOL.md before implementing another client. This is not a ratchet.
const SidewordProtocol = (() => {
  class ProtocolError extends Error {}
  const encoder = new TextEncoder();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  const keyAlgorithm = Object.freeze({ name: "ECDH", namedCurve: "P-256" });

  function bytesToBase64(bytes) {
    let binary = "";
    for (let offset = 0; offset < bytes.length; offset += 0x8000) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
    }
    return btoa(binary);
  }

  function base64ToBytes(value) {
    const binary = atob(value);
    return Uint8Array.from(binary, (character) => character.charCodeAt(0));
  }

  function envelopeContext(chatId, messageId, senderId, recipientId) {
    return encoder.encode(`sideword-web-p256-v1|${chatId}|${messageId}|${senderId}|${recipientId}`);
  }

  async function importPublicKey(value) {
    let bytes;
    try {
      bytes = base64ToBytes(value);
    } catch {
      throw new ProtocolError("Invalid P-256 public key: the participant's encryption key is not valid base64 data.");
    }
    if (bytes.length !== 65 || bytes[0] !== 4) {
      throw new ProtocolError("Invalid P-256 public key: the participant's encryption key has the wrong format.");
    }
    try {
      return await crypto.subtle.importKey("raw", bytes, keyAlgorithm, false, []);
    } catch (error) {
      if (error instanceof DOMException && error.name === "NotSupportedError") throw error;
      throw new ProtocolError("Invalid P-256 public key: the supplied point is not a valid key on this curve.");
    }
  }

  async function deriveSharedSecret(privateKey, publicKey) {
    return new Uint8Array(await crypto.subtle.deriveBits(
      { name: "ECDH", public: publicKey },
      privateKey,
      256,
    ));
  }

  async function deriveMessageKey(sharedSecret, salt, info, usage) {
    const material = await crypto.subtle.importKey("raw", sharedSecret, "HKDF", false, ["deriveKey"]);
    sharedSecret.fill(0);
    return crypto.subtle.deriveKey(
      { name: "HKDF", hash: "SHA-256", salt, info },
      material,
      { name: "AES-GCM", length: 256 },
      false,
      [usage],
    );
  }

  async function encryptForRecipient(identity, plaintext, chatId, messageId, recipient) {
    const recipientKey = await importPublicKey(recipient.public_key);
    const ephemeral = await crypto.subtle.generateKey(keyAlgorithm, false, ["deriveBits"]);
    const salt = crypto.getRandomValues(new Uint8Array(16));
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const info = envelopeContext(chatId, messageId, identity.publicId, recipient.public_id);
    const staticSecret = await deriveSharedSecret(identity.privateKey, recipientKey);
    const ephemeralSecret = await deriveSharedSecret(ephemeral.privateKey, recipientKey);
    const combinedSecret = new Uint8Array(staticSecret.length + ephemeralSecret.length);
    combinedSecret.set(staticSecret);
    combinedSecret.set(ephemeralSecret, staticSecret.length);
    staticSecret.fill(0);
    ephemeralSecret.fill(0);
    const key = await deriveMessageKey(combinedSecret, salt, info, "encrypt");
    const ciphertext = await crypto.subtle.encrypt(
      { name: "AES-GCM", iv, additionalData: info, tagLength: 128 },
      key,
      encoder.encode(plaintext),
    );
    const ephemeralPublicKey = await crypto.subtle.exportKey("raw", ephemeral.publicKey);
    const envelope = {
      v: 1,
      alg: "P256-2DH-HKDF-SHA256-AES256GCM",
      epk: bytesToBase64(new Uint8Array(ephemeralPublicKey)),
      salt: bytesToBase64(salt),
      iv: bytesToBase64(iv),
      ct: bytesToBase64(new Uint8Array(ciphertext)),
    };
    return bytesToBase64(encoder.encode(JSON.stringify(envelope)));
  }

  async function decryptMessage(identity, message, sender) {
    let envelope;
    try {
      envelope = JSON.parse(decoder.decode(base64ToBytes(message.ciphertext)));
    } catch {
      throw new ProtocolError("Invalid ciphertext format: this message is not valid encrypted-message data.");
    }
    if (!envelope || envelope.v !== 1 || envelope.alg !== "P256-2DH-HKDF-SHA256-AES256GCM") {
      throw new ProtocolError("Unsupported ciphertext format: this message uses an encryption format this client does not support.");
    }
    const senderKey = await importPublicKey(sender.public_key);
    const ephemeralKey = await importPublicKey(envelope.epk);
    let salt, iv, ciphertext;
    try {
      salt = base64ToBytes(envelope.salt);
      iv = base64ToBytes(envelope.iv);
      ciphertext = base64ToBytes(envelope.ct);
    } catch {
      throw new ProtocolError("Invalid ciphertext format: encryption parameters are missing or are not valid base64 data.");
    }
    if (salt.length !== 16 || iv.length !== 12 || ciphertext.length < 16) {
      throw new ProtocolError("Invalid ciphertext format: encryption parameters are missing or have invalid lengths.");
    }
    const info = envelopeContext(
      message.chat_id,
      message.client_message_id,
      message.sender_public_id,
      identity.publicId,
    );
    const staticSecret = await deriveSharedSecret(identity.privateKey, senderKey);
    const ephemeralSecret = await deriveSharedSecret(identity.privateKey, ephemeralKey);
    const combinedSecret = new Uint8Array(staticSecret.length + ephemeralSecret.length);
    combinedSecret.set(staticSecret);
    combinedSecret.set(ephemeralSecret, staticSecret.length);
    staticSecret.fill(0);
    ephemeralSecret.fill(0);
    const key = await deriveMessageKey(combinedSecret, salt, info, "decrypt");
    let plaintext;
    try {
      plaintext = await crypto.subtle.decrypt(
        { name: "AES-GCM", iv, additionalData: info, tagLength: 128 }, key, ciphertext,
      );
    } catch {
      throw new ProtocolError("This message could not be authenticated or decrypted: its encrypted data or encryption keys do not match.");
    }
    return decoder.decode(plaintext);
  }

  async function publicKeyFingerprint(publicKey) {
    const bytes = base64ToBytes(publicKey);
    await importPublicKey(publicKey);
    const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
    return Array.from(digest, byte => byte.toString(16).padStart(2, "0")).join("");
  }

  return Object.freeze({ bytesToBase64, base64ToBytes, encryptForRecipient,
    decryptMessage, publicKeyFingerprint, ProtocolError });
})();
