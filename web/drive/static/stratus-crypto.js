/*
 * Stratus browser encryption.
 *
 * This is the same envelope format the terminal uses (control/e2ee.py):
 * P-256 ECDH -> HKDF-SHA256 -> AES-256-GCM, with the content key wrapped once
 * per recipient device. Every primitive here comes from WebCrypto; there is no
 * hand-written cryptography, which is why the terminal and this page can read
 * each other's messages.
 *
 * The host never receives a private key or a passphrase. It only stores the
 * sealed key blob, the ciphertext, and the list of device ids each envelope
 * was sealed for.
 */
(function (root, factory) {
  const api = factory(root.crypto);
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.StratusCrypto = api;
})(globalThis, function (crypto) {
  "use strict";

  const subtle = crypto && crypto.subtle;

  /* Browsers deliberately withhold the WebCrypto API from pages that are not a
   * "secure context": https, or a loopback address. A Stratus site reached over
   * plain http on a LAN address therefore has no cryptography at all, and every
   * call below would fail with something unhelpful like "subtle is undefined".
   * The check is made once, here, so the page can say what is actually wrong. */
  function requireSecureContext() {
    if (subtle) return;
    const where = (typeof location !== "undefined" && location.hostname) || "this address";
    throw new Error(
      "This page cannot decrypt anything because the browser has disabled its " +
      "cryptography API. Browsers only enable it on secure pages — https, or an " +
      "address like 127.0.0.1. This page was opened over plain http on \"" + where +
      "\". Open Stratus from a browser on the host machine (it will use 127.0.0.1), " +
      "or use the terminal client.");
  }
  const CURVE = { name: "ECDH", namedCurve: "P-256" };
  const INFO_V2 = "stratus-envelope-v2";

  const utf8 = (text) => new TextEncoder().encode(text);

  function b64decode(value) {
    const binary = atob(value);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
    return bytes;
  }

  function b64encode(bytes) {
    const view = new Uint8Array(bytes);
    let binary = "";
    // Chunked so a multi-megabyte file cannot blow the argument limit.
    for (let index = 0; index < view.length; index += 0x8000) {
      binary += String.fromCharCode.apply(null, view.subarray(index, index + 0x8000));
    }
    return btoa(binary);
  }

  /* Python's HKDF(salt=None) uses a zero key; an empty salt is equivalent,
   * because HMAC zero-pads any short key to the block size. */
  async function hkdf(sharedBits, info) {
    const material = await subtle.importKey("raw", sharedBits, "HKDF", false, ["deriveBits"]);
    const bits = await subtle.deriveBits(
      { name: "HKDF", hash: "SHA-256", salt: new Uint8Array(0), info: utf8(info) }, material, 256);
    return subtle.importKey("raw", bits, { name: "AES-GCM" }, false, ["encrypt", "decrypt"]);
  }

  async function agree(privateKey, publicPoint, info) {
    const publicKey = await subtle.importKey("raw", publicPoint, CURVE, false, []);
    const shared = await subtle.deriveBits({ name: "ECDH", public: publicKey }, privateKey, 256);
    return hkdf(shared, info);
  }

  async function importPrivateKey(pkcs8Base64) {
    requireSecureContext();
    return subtle.importKey("pkcs8", b64decode(pkcs8Base64), CURVE, false, ["deriveBits"]);
  }

  /* Open the passphrase-sealed device key. Runs entirely in this browser. */
  async function unsealDeviceKey(blob, passphrase, deviceId) {
    requireSecureContext();
    const material = await subtle.importKey("raw", utf8(passphrase), "PBKDF2", false, ["deriveBits"]);
    const bits = await subtle.deriveBits(
      { name: "PBKDF2", hash: "SHA-256", salt: b64decode(blob.salt), iterations: blob.iterations || 600000 },
      material, 256);
    const key = await subtle.importKey("raw", bits, { name: "AES-GCM" }, false, ["decrypt"]);
    const opened = await subtle.decrypt(
      { name: "AES-GCM", iv: b64decode(blob.nonce), additionalData: utf8(deviceId) }, key, b64decode(blob.sealed));
    return new TextDecoder().decode(opened);
  }

  async function decrypt(payload, privateKey, deviceId, aad) {
    requireSecureContext();
    const envelope = payload.envelopes[deviceId];
    if (!envelope) throw new Error("This message was not shared with this device.");
    if (Number(payload.version || 1) !== 2) {
      throw new Error("This message uses the older terminal-only format and cannot be opened in a browser.");
    }
    const wrapping = await agree(privateKey, b64decode(envelope.ephemeral), INFO_V2);
    const contentKeyBytes = await subtle.decrypt(
      { name: "AES-GCM", iv: b64decode(envelope.nonce), additionalData: utf8(deviceId) },
      wrapping, b64decode(envelope.key));
    const contentKey = await subtle.importKey("raw", contentKeyBytes, { name: "AES-GCM" }, false, ["decrypt"]);
    const plaintext = await subtle.decrypt(
      { name: "AES-GCM", iv: b64decode(payload.nonce), additionalData: utf8(aad) },
      contentKey, b64decode(payload.ciphertext));
    return new Uint8Array(plaintext);
  }

  async function encrypt(plaintext, recipients, aad) {
    requireSecureContext();
    const missing = recipients.filter((item) => !item.ecdh_public_key);
    if (missing.length) {
      throw new Error("Some devices have not published a current encryption key: " +
        missing.map((item) => item.display_name || item.id).join(", "));
    }
    if (!recipients.length) throw new Error("No recipients to encrypt for.");
    const contentKeyBytes = crypto.getRandomValues(new Uint8Array(32));
    const nonce = crypto.getRandomValues(new Uint8Array(12));
    const contentKey = await subtle.importKey("raw", contentKeyBytes, { name: "AES-GCM" }, false, ["encrypt"]);
    const ciphertext = await subtle.encrypt(
      { name: "AES-GCM", iv: nonce, additionalData: utf8(aad) }, contentKey, plaintext);

    const envelopes = {};
    for (const recipient of recipients) {
      const ephemeral = await subtle.generateKey(CURVE, true, ["deriveBits"]);
      const wrapping = await agree(ephemeral.privateKey, b64decode(recipient.ecdh_public_key), INFO_V2);
      const wrapNonce = crypto.getRandomValues(new Uint8Array(12));
      const wrapped = await subtle.encrypt(
        { name: "AES-GCM", iv: wrapNonce, additionalData: utf8(recipient.id) }, wrapping, contentKeyBytes);
      const point = await subtle.exportKey("raw", ephemeral.publicKey);
      envelopes[recipient.id] = {
        ephemeral: b64encode(point), nonce: b64encode(wrapNonce), key: b64encode(wrapped),
      };
    }
    return { version: 2, nonce: b64encode(nonce), ciphertext: b64encode(ciphertext), envelopes: envelopes };
  }

  const encryptText = async (text, recipients, aad) => encrypt(utf8(text), recipients, aad);
  const decryptText = async (payload, privateKey, deviceId, aad) =>
    new TextDecoder().decode(await decrypt(payload, privateKey, deviceId, aad));

  return {
    b64decode, b64encode, importPrivateKey, unsealDeviceKey,
    encrypt, decrypt, encryptText, decryptText,
  };
});
