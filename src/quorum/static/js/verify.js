// Offline, in-browser verification of Quorum Ed25519 records. WebCrypto first; falls back
// to the vendored @noble/ed25519 (served locally, no CDN) on browsers without Ed25519.
import * as noble from "/static/vendor/noble-ed25519.js";

function b64u(s) {
  s = s.replace(/-/g, "+").replace(/_/g, "/");
  while (s.length % 4) s += "=";
  return Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
}

async function verify(record) {
  const keys = await (await fetch("/.well-known/quorum-keys.json")).json();
  const key = keys.keys.find((k) => k.kid === record.key_id);
  if (!key) return { ok: false, why: "unknown key id " + record.key_id };
  const pub = b64u(key.x), sig = b64u(record.signature), msg = new TextEncoder().encode(record.payload);
  try {
    const k = await crypto.subtle.importKey("raw", pub, { name: "Ed25519" }, false, ["verify"]);
    return { ok: await crypto.subtle.verify({ name: "Ed25519" }, k, sig, msg), how: "WebCrypto" };
  } catch (e) {
    return { ok: await noble.verifyAsync(sig, msg, pub), how: "noble-ed25519 (vendored)" };
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const btn = document.getElementById("verify-local");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    const out = document.getElementById("local-result");
    try {
      const rec = JSON.parse(document.getElementById("record-input").value);
      const r = await verify(rec);
      out.innerHTML = r.ok ? '<span class="badge good">valid</span> verified in your browser with ' + r.how
                           : '<span class="badge bad">invalid</span> ' + (r.why || "signature does not match");
    } catch (e) {
      out.innerHTML = '<span class="badge bad">error</span> paste the JSON record exactly as issued';
    }
  });
});
