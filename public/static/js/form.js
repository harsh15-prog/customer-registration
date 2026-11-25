const form = document.querySelector("#customer-form");
const dob = document.querySelector("#dob");
const age = document.querySelector("#age");
const submit = document.querySelector("#submit");
const reset = form.querySelector('[type="reset"]');
const label = document.querySelector("#submit-label");
const spinner = form.querySelector(".spinner");
const formError = document.querySelector("#form-error");
const storageKey = "customer-registration-attempt";
let submissionKey;

function savedKey() {
  try {
    return sessionStorage.getItem(storageKey);
  } catch {
    return null;
  }
}

function rememberKey(key) {
  submissionKey = key;
  try {
    if (key) sessionStorage.setItem(storageKey, key);
    else sessionStorage.removeItem(storageKey);
  } catch {
    // The in-memory key still covers retries on this page.
  }
}

function clearErrors() {
  formError.textContent = "";
  form.querySelectorAll("[aria-invalid]").forEach(input => input.removeAttribute("aria-invalid"));
  form.querySelectorAll(".field .field-error").forEach(error => { error.textContent = ""; });
}

function setBusy(busy) {
  submit.disabled = busy;
  reset.disabled = busy;
  spinner.hidden = !busy;
  label.textContent = busy ? "Submitting…" : "Submit registration";
  form.setAttribute("aria-busy", String(busy));
}

dob.addEventListener("input", () => {
  age.value = "";
  if (!dob.value || !dob.validity.valid) return;
  const current = form.dataset.today;
  const afterBirthday = current.slice(5) >= dob.value.slice(5);
  age.value = Number(current.slice(0, 4)) - Number(dob.value.slice(0, 4)) - Number(!afterBirthday);
});

form.addEventListener("reset", () => {
  clearErrors();
  rememberKey(null);
  window.turnstile?.reset();
});

form.addEventListener("submit", async event => {
  event.preventDefault();
  clearErrors();
  const fields = new FormData(form);
  const payload = {
    name: fields.get("name").trim(),
    email: fields.get("email").trim(),
    dob: fields.get("dob"),
    balance: fields.get("balance"),
    website: fields.get("website"),
    turnstile_token: fields.get("cf-turnstile-response"),
  };
  const errors = {};
  if (!payload.name) errors.name = "Enter your full name.";
  if (!/^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$/.test(payload.email)) {
    errors.email = "Enter a valid email address.";
  }
  if (!/^[0-9]{1,12}(?:\.[0-9]{1,2})?$/.test(payload.balance)) {
    errors.balance = "Enter a non-negative balance with up to 2 decimal places.";
  }
  if (Object.keys(errors).length) {
    showFieldErrors(errors);
    return;
  }
  if (form.querySelector(".cf-turnstile") && !payload.turnstile_token) {
    formError.textContent = "Complete the security check before submitting.";
    return;
  }

  setBusy(true);
  try {
    rememberKey(submissionKey || savedKey() || crypto.randomUUID());
    const response = await fetch(form.dataset.endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": submissionKey },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(25000),
    });
    const body = await response.json();
    if (!response.ok) {
      formError.textContent = response.status === 409
        ? "These details conflict with an earlier submission. Retry with the same details, or reset to start a new registration."
        : body.error || "Registration is temporarily unavailable.";
      showFieldErrors(body.fields || {});
      return;
    }
    document.querySelector("#receipt-id").textContent = String(body.customer_id);
    form.reset();
    form.hidden = true;
    const success = document.querySelector("#success");
    success.hidden = false;
    success.focus();
  } catch {
    formError.textContent = "We couldn’t confirm your registration. Try again with the same details.";
  } finally {
    setBusy(false);
    if (!form.hidden) window.turnstile?.reset();
  }
});

function showFieldErrors(errors) {
  let first;
  for (const [name, message] of Object.entries(errors)) {
    const field = form.elements.namedItem(name);
    const target = document.getElementById(name + "-error");
    if (!field || !target) continue;
    target.textContent = message;
    field.setAttribute("aria-invalid", "true");
    first ||= field;
  }
  first?.focus();
}

submit.disabled = false;
