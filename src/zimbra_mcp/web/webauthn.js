"use strict";

// --- texts -------------------------------------------------------------------
// The page carries its texts in a JSON data block (<script type="application/json"
// id="i18n">), which the CSP allows because it is never executed.

let STRINGS = null;

function t(key, values) {
  if (STRINGS === null) {
    const el = document.getElementById("i18n");
    try {
      STRINGS = el ? JSON.parse(el.textContent) : {};
    } catch (e) {
      STRINGS = {};
    }
  }
  const text = Object.prototype.hasOwnProperty.call(STRINGS, key) ? STRINGS[key] : key;
  const vals = values || {};
  // One pass, so a value that itself contains "{...}" is never expanded.
  return text.replace(/\{(\w+)\}/g, (m, name) =>
    Object.prototype.hasOwnProperty.call(vals, name) ? String(vals[name]) : m
  );
}

// --- base64url helpers -----------------------------------------------------

function b64urlToBuf(value) {
  const pad = "=".repeat((4 - (value.length % 4)) % 4);
  const bin = atob((value + pad).replace(/-/g, "+").replace(/_/g, "/"));
  const buf = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
  return buf.buffer;
}

function bufToB64url(buf) {
  const bytes = new Uint8Array(buf);
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function creationOptions(json) {
  const o = Object.assign({}, json);
  o.challenge = b64urlToBuf(json.challenge);
  o.user = Object.assign({}, json.user, { id: b64urlToBuf(json.user.id) });
  o.excludeCredentials = (json.excludeCredentials || []).map((c) =>
    Object.assign({}, c, { id: b64urlToBuf(c.id) })
  );
  return o;
}

function requestOptions(json) {
  const o = Object.assign({}, json);
  o.challenge = b64urlToBuf(json.challenge);
  o.allowCredentials = (json.allowCredentials || []).map((c) =>
    Object.assign({}, c, { id: b64urlToBuf(c.id) })
  );
  return o;
}

function credentialToJSON(cred) {
  const r = cred.response;
  const response = { clientDataJSON: bufToB64url(r.clientDataJSON) };
  if (r.attestationObject) {
    response.attestationObject = bufToB64url(r.attestationObject);
    if (r.getTransports) response.transports = r.getTransports();
  } else {
    response.authenticatorData = bufToB64url(r.authenticatorData);
    response.signature = bufToB64url(r.signature);
    if (r.userHandle) response.userHandle = bufToB64url(r.userHandle);
  }
  return {
    id: cred.id,
    rawId: bufToB64url(cred.rawId),
    type: cred.type,
    response: response,
    authenticatorAttachment: cred.authenticatorAttachment || undefined,
    clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
  };
}

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify(body),
  });
  let data = {};
  try {
    data = await res.json();
  } catch (e) {
    /* empty body */
  }
  if (!res.ok) throw new Error(data.error || t("error_status", { status: res.status }));
  return data;
}

// --- page helpers ----------------------------------------------------------

function $(id) {
  return document.getElementById(id);
}

function show(id) {
  document.querySelectorAll("section[data-step]").forEach((s) => (s.hidden = s.id !== id));
  // Progress bar: steps before the current one are done, the current one is active.
  let reached = false;
  document.querySelectorAll(".steps li[data-step-for]").forEach((li) => {
    const current = li.dataset.stepFor === id;
    li.classList.toggle("active", current);
    li.classList.toggle("done", !reached && !current);
    if (current) {
      reached = true;
      li.setAttribute("aria-current", "step");
    } else {
      li.removeAttribute("aria-current");
    }
  });
}

function setStatus(text, isError) {
  const el = $("status");
  el.textContent = text || "";
  el.className = isError ? "status error" : "status";
}

function webauthnError(e) {
  if (e && e.name === "NotAllowedError") return t("not_allowed");
  if (e && e.name === "InvalidStateError") return t("invalid_state");
  return (e && e.message) || String(e);
}

function requireWebAuthn() {
  if (!window.PublicKeyCredential) {
    setStatus(t("no_webauthn"), true);
    return false;
  }
  return true;
}

// --- login page ------------------------------------------------------------

function initLogin() {
  const txn = new URLSearchParams(location.search).get("txn") || "";

  $("login-btn").addEventListener("click", async () => {
    if (!requireWebAuthn()) return;
    setStatus(t("waiting_passkey"));
    $("login-btn").disabled = true;
    try {
      const options = await postJSON("/login/options", { txn });
      const cred = await navigator.credentials.get({ publicKey: requestOptions(options) });
      const who = await postJSON("/login/verify", { txn, credential: credentialToJSON(cred) });
      $("who").textContent = who.display_name + " <" + who.email + ">";
      $("client").textContent = who.client_name;
      setStatus("");
      show("step-consent");
    } catch (e) {
      setStatus(webauthnError(e), true);
    } finally {
      $("login-btn").disabled = false;
    }
  });

  async function decide(approve) {
    $("approve-btn").disabled = $("deny-btn").disabled = true;
    try {
      const res = await postJSON("/login/approve", { txn, approve });
      setStatus(approve ? t("redirecting") : t("denied"));
      location.href = res.redirect;
    } catch (e) {
      setStatus(webauthnError(e), true);
      $("approve-btn").disabled = $("deny-btn").disabled = false;
    }
  }
  $("approve-btn").addEventListener("click", () => decide(true));
  $("deny-btn").addEventListener("click", () => decide(false));
}

// --- enrollment page -------------------------------------------------------

function initEnroll() {
  let email = "";
  // Invite links look like /invite#<token>. The token is in the fragment, so it
  // never reaches server logs; drop it from the address bar once read.
  const inviteMode = location.pathname.startsWith("/invite");
  const invite = inviteMode ? location.hash.slice(1) : "";
  const base = inviteMode ? "/invite" : "/enroll";
  if (inviteMode) {
    history.replaceState(null, "", location.pathname);
    $("invite-note").hidden = false;
    $("email-field").hidden = true;
    $("email").required = false;
    if (!invite) {
      setStatus(t("incomplete_link"), true);
      $("start-form").querySelector("button").disabled = true;
    }
  }

  $("start-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    email = $("email").value.trim();
    setStatus(t("sending_code"));
    try {
      const res = await postJSON(base + "/start", { email, invite });
      setStatus(res.message);
      show("step-code");
      $("code").focus();
    } catch (e) {
      setStatus(webauthnError(e), true);
    }
  });

  $("code-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (!requireWebAuthn()) return;
    setStatus(t("checking_code"));
    try {
      const res = await postJSON(base + "/options", { email, invite, code: $("code").value });
      setStatus(t("confirm_device"));
      const cred = await navigator.credentials.create({ publicKey: creationOptions(res.options) });
      await postJSON(base + "/verify", {
        email: res.email,
        invite,
        session: res.session,
        label: $("label").value,
        credential: credentialToJSON(cred),
      });
      setStatus("");
      show("step-done");
    } catch (e) {
      setStatus(webauthnError(e), true);
    }
  });
}

// --- calendar tokens page -------------------------------------------------

function initCalendar() {
  function when(ts) {
    return ts ? new Date(ts * 1000).toLocaleString(t("locale")) : t("never");
  }

  function render(state) {
    $("cal-who").textContent = state.email;
    const list = $("cal-list");
    list.textContent = "";
    if (!state.tokens.length) {
      const li = document.createElement("li");
      li.textContent = t("no_tokens");
      list.appendChild(li);
    }
    state.tokens.forEach((tok) => {
      const li = document.createElement("li");
      li.textContent = t("token_line", {
        label: tok.label || t("unnamed"),
        created: when(tok.created),
        last_used: when(tok.last_used),
      });
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "secondary";
      btn.textContent = t("revoke");
      btn.addEventListener("click", async () => {
        if (!confirm(t("revoke_confirm"))) return;
        try {
          render(await postJSON("/calendar/revoke", { id: tok.id }));
          setStatus(t("revoked"));
        } catch (e) {
          setStatus(webauthnError(e), true);
        }
      });
      li.appendChild(btn);
      list.appendChild(li);
    });
    if (state.new_token) {
      $("cal-user").textContent = state.email;
      $("cal-url-user").textContent = state.email;
      $("cal-token").textContent = state.new_token;
      $("cal-new").hidden = false;
    }
  }

  $("cal-login-btn").addEventListener("click", async () => {
    if (!requireWebAuthn()) return;
    setStatus(t("waiting_passkey"));
    $("cal-login-btn").disabled = true;
    try {
      const options = await postJSON("/calendar/options", {});
      const cred = await navigator.credentials.get({ publicKey: requestOptions(options) });
      render(await postJSON("/calendar/verify", { credential: credentialToJSON(cred) }));
      setStatus("");
      show("step-tokens");
    } catch (e) {
      setStatus(webauthnError(e), true);
    } finally {
      $("cal-login-btn").disabled = false;
    }
  });

  $("cal-create").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try {
      render(await postJSON("/calendar/token", { label: $("cal-label").value }));
      $("cal-label").value = "";
      setStatus("");
    } catch (e) {
      setStatus(webauthnError(e), true);
    }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  const page = document.body.dataset.page;
  const first = document.querySelector("section[data-step]");
  if (first) show(first.id);
  if (page === "login") initLogin();
  if (page === "enroll") initEnroll();
  if (page === "calendar") initCalendar();
});
