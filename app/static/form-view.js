// The fill-in form for what a resume lacks (app/agent/forms.py), drawn from the JSON schema the server sends: inside the chat
// (index.html, under the agent's reply) and on a page of its own for a recruiter on WhatsApp (fill.html, /fill/<token>).
// The server checks every answer with the same model; this only draws it. Everything is built with textContent, so nothing the
// server or a resume says can inject HTML.
(function () {
  const LINK_RE = /(?:https?:\/\/[^\s)<>\]]*)?\/fill\/([A-Za-z0-9_-]{20,})/;

  function el(tag, text, cls) {
    const e = document.createElement(tag);
    if (text != null) e.textContent = text;
    if (cls) e.className = cls;
    return e;
  }

  // The form's token in a reply from the agent, or null.
  function tokenIn(text) {
    const m = LINK_RE.exec(String(text || ""));
    return m ? m[1] : null;
  }

  // The reply without the form's link (the form is drawn under it instead).
  function strip(text) {
    return String(text || "")
      .replace(/\[([^\]]*)\]\(\s*(?:https?:\/\/[^\s)]*)?\/fill\/[A-Za-z0-9_-]{20,}\s*\)/g, "$1")
      .replace(new RegExp(LINK_RE.source, "g"), "")
      .replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
  }

  async function call(token, body) {
    const opts = body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
    try {
      const resp = await fetch("/api/form/" + encodeURIComponent(token), opts);
      const data = await resp.json().catch(() => ({}));
      return data.status ? data : { status: "error", message: data.detail || "the server answered HTTP " + resp.status };
    } catch (err) {
      return { status: "error", message: "couldn't reach the server (" + err.message + ")" };
    }
  }

  function working(text) {
    const w = el("div", undefined, "ff-working");
    w.append(el("span", undefined, "ff-spin"), el("span", text));
    return w;
  }

  function input(name, prop, required, kept) {
    const kind = prop.input || "text";
    const box = kind === "skills" ? el("textarea") : el("input");
    if (kind === "skills") box.rows = 2;
    else box.type = kind === "email" ? "email" : kind === "tel" ? "tel" : "text";
    box.name = name;
    box.value = kept && name in kept ? kept[name] : (prop.default != null ? prop.default : "");
    if (prop.maxLength) box.maxLength = prop.maxLength;
    if (prop.placeholder) box.placeholder = prop.placeholder;
    if (required) box.required = true;
    box.autocomplete = "off";
    box.spellcheck = false;
    return box;
  }

  function drawForm(box, token, data, kept) {
    const schema = data.schema || { properties: {}, required: [] };
    const props = schema.properties || {};
    const required = new Set(schema.required || []);
    const errors = data.errors || {};
    box.replaceChildren(el("div", data.title || "Complete the details", "ff-title"));
    if (data.file) box.append(el("div", data.file, "ff-file"));
    if (data.intro) box.append(el("p", data.intro, "ff-intro"));
    if (data.message) box.append(el("p", data.message, "ff-msg" + (Object.keys(errors).length || data.status === "error" ? " bad" : "")));
    (data.blocked || []).forEach(b => box.append(el("p", "Needs a new resume: " + b, "ff-blocked")));

    const form = el("form");
    form.noValidate = true;  // the server's model is the one that checks; its messages are shown under each field
    const inputs = {};
    let group = null;
    for (const [name, prop] of Object.entries(props)) {
      if (prop.group !== group) {
        group = prop.group;
        form.append(el("div", group === "missing" ? "Missing" : "Check what was read", "ff-group"));
      }
      const row = el("label", undefined, "ff-row" + (group === "missing" ? " missing" : "") + (errors[name] ? " bad" : ""));
      const label = el("span", prop.title || name, "ff-label");
      if (required.has(name)) label.append(el("span", " *", "ff-req"));
      inputs[name] = input(name, prop, required.has(name), kept);
      row.append(label, inputs[name]);
      if (prop.description) row.append(el("span", prop.description, "ff-hint"));
      if (errors[name]) row.append(el("span", errors[name], "ff-error"));
      form.append(row);
    }
    const buttons = el("div", undefined, "ff-buttons");
    const submit = el("button", required.size ? "Submit" : "Create the profile", "ff-submit");
    submit.type = "submit";
    submit.disabled = data.can_submit === false;
    buttons.append(submit);
    let skip = null;
    if (data.can_skip) {
      skip = el("button", "Create without the missing details", "ff-skip");
      skip.type = "button";
      buttons.append(skip);
    }
    form.append(buttons);
    if (data.skip_note) form.append(el("p", data.skip_note, "ff-note"));
    box.append(form);

    async function send(skipping) {
      const values = {};
      for (const [name, field] of Object.entries(inputs)) values[name] = field.value;
      submit.disabled = true;
      if (skip) skip.disabled = true;
      form.append(working(skipping ? "Creating the profile…" : "Saving… reading and creating the profile can take up to a minute"));
      const next = await call(token, { values, skip: skipping });
      render(box, token, next, values);
    }
    form.addEventListener("submit", ev => { ev.preventDefault(); send(false); });
    if (skip) skip.addEventListener("click", () => send(true));
    const first = form.querySelector(".ff-row.bad input, .ff-row.bad textarea");
    if (first) first.focus();
  }

  function drawResult(box, data) {
    const res = data.result || {};
    const p = res.profile || {};
    const created = p.status === "created";
    box.replaceChildren(el("div", created ? "Profile created" : "Already in Questlight", "ff-title ff-done"));
    if (res.file) box.append(el("div", res.file, "ff-file"));
    const facts = el("ul", undefined, "ff-facts");
    if (res.name) facts.append(el("li", "Candidate: " + res.name));
    if (res.experience) facts.append(el("li", "Experience: " + res.experience));
    if (p.candidate_id) facts.append(el("li", "Candidate ID: " + p.candidate_id));
    if (!created) facts.append(el("li", "A profile with this email already exists, so nothing was changed."));
    if (created && p.resume_attached === false) facts.append(el("li", "Created without the resume file: it can be uploaded in Questlight."));
    if ((p.adjusted || []).length) facts.append(el("li", "Filled in or shortened: " + p.adjusted.join("; ")));
    box.append(facts);
    const roles = res.top_roles || [];
    if (roles.length) {
      box.append(el("div", "Top matching roles", "ff-group"));
      const list = el("ol", undefined, "ff-roles");
      const said = { screened: "added to Screening", already: "already on this job", failed: "screening failed", skipped: "not screened" };
      roles.forEach(r => {
        const item = el("li", r.title + " (" + r.job_id + ")");
        item.append(el("span", " · score " + r.score + "/100" + (said[r.screening] ? " · " + said[r.screening] : ""), "ff-hint"));
        list.append(item);
      });
      box.append(list);
    } else if (res.roles_message) {
      box.append(el("p", res.roles_message, "ff-note"));
    }
    if (res.screening && res.screening.status && res.screening.status !== "screened") box.append(el("p", res.screening.message, "ff-note"));
  }

  function render(box, token, data, kept) {
    box.className = "fillform";
    if (data.status === "open") return drawForm(box, token, data, kept);
    if (data.status === "done") return drawResult(box, data);
    box.replaceChildren(el("div", data.status === "expired" ? "This form is closed" : "Something went wrong", "ff-title"),
                        el("p", data.message || "Please try again.", "ff-msg bad"));
  }

  // Loads the form for this token into `box` and keeps it up to date until the profile exists.
  async function open(box, token) {
    box.className = "fillform";
    box.replaceChildren(working("Loading the form…"));
    render(box, token, await call(token));
  }

  window.FillForm = { tokenIn, strip, open };
})();
