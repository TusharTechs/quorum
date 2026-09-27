// Offline, in-browser verification of Quorum Ed25519 records. WebCrypto first; falls back
// to the vendored @noble/ed25519 (served locally, no CDN) on browsers without Ed25519.
// Checks the record's signature, the signing key's status, and the signed revocation list
// (whose own signature is verified the same way before it is trusted).
import * as noble from "/static/vendor/noble-ed25519.js";

function b64u(s) {
  s = s.replace(/-/g, "+").replace(/_/g, "/");
  while (s.length % 4) s += "=";
  return Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function signatureOk(keys, record) {
  const key = keys.keys.find((k) => k.kid === record.key_id);
  if (!key) return { ok: false, why: "unknown key id " + record.key_id };
  const pub = b64u(key.x), sig = b64u(record.signature), msg = new TextEncoder().encode(record.payload);
  try {
    const k = await crypto.subtle.importKey("raw", pub, { name: "Ed25519" }, false, ["verify"]);
    return { ok: await crypto.subtle.verify({ name: "Ed25519" }, k, sig, msg), how: "WebCrypto", key };
  } catch (e) {
    return { ok: await noble.verifyAsync(sig, msg, pub), how: "noble-ed25519 (vendored)", key };
  }
}

async function verify(record) {
  const keys = await (await fetch("/.well-known/quorum-keys.json")).json();
  const r = await signatureOk(keys, record);
  if (!r.ok) return r;
  const data = JSON.parse(record.payload);
  const stamp = data.issued_at || data.ts;
  if (r.key.retired_at && (!stamp || new Date(stamp) > new Date(r.key.retired_at))) {
    return { ok: false, why: "signed with a key after that key was retired" };
  }
  r.keyNote = r.key.retired_at ? "key retired " + r.key.retired_at.slice(0, 10) + ", signed before that" : "key active";
  if (!data.serial) return r; // audit checkpoints are not revocable
  const list = await (await fetch("/.well-known/quorum-revocations.json", { cache: "no-store" })).json();
  const lr = await signatureOk(keys, list);
  if (!lr.ok) { r.revocation = "the revocation list's signature did not verify, so revocation status is unknown"; return r; }
  const hit = JSON.parse(list.payload).revoked.find((x) => x.serial === data.serial && x.event === (data.event || {}).id);
  r.revoked = hit || null;
  r.revocation = hit ? null : "not on the signed revocation list";
  return r;
}

document.addEventListener("DOMContentLoaded", () => {
  const btn = document.getElementById("verify-local");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    const out = document.getElementById("local-result");
    try {
      const rec = JSON.parse(document.getElementById("record-input").value);
      const r = await verify(rec);
      if (!r.ok) {
        out.innerHTML = '<span class="badge bad">invalid</span> ' + esc(r.why || "signature does not match");
      } else if (r.revoked) {
        out.innerHTML = '<span class="badge bad">revoked</span> the signature is valid (' + esc(r.how) + "), but the record was revoked on " +
          esc(r.revoked.revoked_at.slice(0, 10)) + ": " + esc(r.revoked.reason);
      } else {
        out.innerHTML = '<span class="badge good">valid</span> verified in your browser with ' + esc(r.how) + " · " + esc(r.keyNote) +
          (r.revocation ? " · " + esc(r.revocation) : "");
      }
    } catch (e) {
      out.innerHTML = '<span class="badge bad">error</span> paste the JSON record exactly as issued';
    }
  });
});
