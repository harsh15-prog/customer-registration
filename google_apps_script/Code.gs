const QUESTIONS = [
  ["name", "Full Name", "TEXT", String.raw`^\S(?:.{0,48}\S)?$`,
   "Enter 1 to 50 characters without leading or trailing spaces."],
  ["email", "Email Address", "TEXT", String.raw`^(?=.{1,50}$)[A-Za-z0-9.!#$%&'*+/=?^_\x60{|}~-]+@(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,}$`,
   "Enter a valid email address with up to 50 characters."],
  ["dob", "Date of Birth", "DATE", null, "Include the year."],
  ["balance", "Account Balance", "TEXT", String.raw`^[0-9]{1,12}(\.[0-9]{1,2})?$`,
   "Enter a non-negative amount in ₹, using a period for up to two decimal places."],
];

function setupRegistrationForm() {
  const props = PropertiesService.getScriptProperties();
  deliveryConfig_(props);
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    const existingId = props.getProperty("FORM_ID");
    const form = existingId
      ? FormApp.openById(existingId)
      : FormApp.create("Customer Registration", false);
    if (!existingId) props.setProperty("FORM_ID", form.getId());

    form.setDescription("We collect your name, email, date of birth and account balance to create your customer record and calculate your age. Google Forms also stores your responses.")
      .setConfirmationMessage("Google Forms received your details. Your customer record will be processed automatically.")
      .setCollectEmail(false)
      .setAllowResponseEdits(false)
      .setLimitOneResponsePerUser(false)
      .setPublishingSummary(false)
      .setShowLinkToRespondAgain(false);

    for (const [field, title, type, pattern, help] of QUESTIONS) {
      const storedId = props.getProperty("ITEM_" + field);
      let item = storedId && form.getItemById(Number(storedId));
      if (!item) {
        item = form.getItems(FormApp.ItemType[type])
          .find(candidate => candidate.getTitle() === title || !candidate.getTitle());
      }
      if (!item) item = type === "DATE" ? form.addDateItem() : form.addTextItem();
      if (item.getType() !== FormApp.ItemType[type]) throw new Error("Unexpected item type for " + field);
      props.setProperty("ITEM_" + field, String(item.getId()));
      if (type === "DATE") {
        item.asDateItem().setTitle(title).setHelpText(help).setIncludesYear(true).setRequired(true);
      } else {
        const validation = FormApp.createTextValidation()
          .requireTextMatchesPattern(pattern).setHelpText(help).build();
        item.asTextItem().setTitle(title).setHelpText(help).setValidation(validation).setRequired(true);
      }
    }

    const triggers = ScriptApp.getProjectTriggers();
    if (!triggers.some(trigger => trigger.getHandlerFunction() === "onRegistrationSubmit"
      && trigger.getTriggerSourceId() === form.getId()
      && trigger.getEventType() === ScriptApp.EventType.ON_FORM_SUBMIT)) {
      ScriptApp.newTrigger("onRegistrationSubmit").forForm(form).onFormSubmit().create();
    }
    if (!triggers.some(trigger => trigger.getHandlerFunction() === "retryRegistrations"
      && trigger.getEventType() === ScriptApp.EventType.CLOCK)) {
      ScriptApp.newTrigger("retryRegistrations").timeBased().everyMinutes(5).create();
    }

    if (form.supportsAdvancedResponderPermissions() && !form.isPublished()) form.setPublished(true);
    if (!form.isAcceptingResponses()) form.setAcceptingResponses(true);
    const urls = { respondentUrl: form.getPublishedUrl(), ownerEditUrl: form.getEditUrl() };
    console.log("Respondent URL: " + urls.respondentUrl);
    console.log("Owner editing URL: " + urls.ownerEditUrl);
    return urls;
  } finally {
    lock.releaseLock();
  }
}

function deliveryConfig_(props) {
  const apiBase = (props.getProperty("API_BASE_URL") || "").trim().replace(/\/+$/, "");
  const secret = props.getProperty("GOOGLE_WEBHOOK_SECRET") || "";
  if (!/^https:\/\/[A-Za-z0-9.-]+$/.test(apiBase) || !/^[!-~]{32,}$/.test(secret)) {
    throw new Error("Set API_BASE_URL to the HTTPS production origin and set GOOGLE_WEBHOOK_SECRET in Script Properties.");
  }
  return { url: apiBase + "/api/google-form", secret };
}

function dateAsIso_(value) {
  if (value instanceof Date && !Number.isNaN(value.getTime())) {
    return Utilities.formatDate(value, Session.getScriptTimeZone(), "yyyy-MM-dd");
  }
  const match = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(String(value).trim());
  if (!match) throw new Error("Unsupported date answer.");
  return [match[1], match[2].padStart(2, "0"), match[3].padStart(2, "0")].join("-");
}

function buildPayload_(response, props) {
  const answers = new Map(response.getItemResponses()
    .map(answer => [String(answer.getItem().getId()), answer.getResponse()]));
  const payload = {};
  for (const [field] of QUESTIONS) {
    const answer = answers.get(props.getProperty("ITEM_" + field));
    if (answer == null) throw new Error("A required response is missing.");
    payload[field] = field === "dob" ? dateAsIso_(answer) : String(answer).trim();
  }
  return payload;
}

function deliver_(response, props, config) {
  let payload;
  try {
    payload = buildPayload_(response, props);
  } catch (error) {
    return { code: "invalid_payload", permanent: true };
  }

  let result;
  try {
    result = UrlFetchApp.fetch(config.url, {
      method: "post",
      contentType: "application/json",
      headers: {
        Authorization: "Bearer " + config.secret,
        "Idempotency-Key": response.getId(),
      },
      payload: JSON.stringify(payload),
      followRedirects: false,
      muteHttpExceptions: true,
    });
  } catch (error) {
    return { code: "connection_error", permanent: false };
  }
  const code = result.getResponseCode();
  if (code === 200 || code === 201) {
    try {
      const receipt = JSON.parse(result.getContentText());
      if (Number.isInteger(receipt.customer_id) && receipt.customer_id > 0) return null;
    } catch (error) {
      return { code: "invalid_receipt", permanent: false };
    }
    return { code: "invalid_receipt", permanent: false };
  }
  return { code, permanent: [400, 409, 413, 415, 422].includes(code) };
}

function trackDelivery_(response, props, config) {
  const id = response.getId();
  const key = "RETRY_" + id;
  const prior = JSON.parse(props.getProperty(key) || "null");
  if (prior && (prior.permanent || prior.next > Date.now())) return;
  const failure = deliver_(response, props, config);
  if (!failure) {
    if (prior) props.deleteProperty(key);
    return;
  }
  const attempts = (prior ? prior.attempts : 0) + 1;
  const delay = Math.min(360, 2 ** Math.min(attempts, 9)) * 60000;
  props.setProperty(key, JSON.stringify({ ...failure, attempts, next: Date.now() + delay }));
  console.warn("Registration delivery pending for response " + id + " (status " + failure.code + ").");
}

function onRegistrationSubmit(event) {
  const props = PropertiesService.getScriptProperties();
  if (!event || !event.response || event.source.getId() !== props.getProperty("FORM_ID")) {
    throw new Error("This function requires the installable trigger on the configured form.");
  }
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) return; // The form retains the response for scheduled reconciliation.
  try {
    trackDelivery_(event.response, props, deliveryConfig_(props));
  } finally {
    lock.releaseLock();
  }
}

function retryRegistrations() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) return;
  try {
    const props = PropertiesService.getScriptProperties();
    const config = deliveryConfig_(props);
    const form = FormApp.openById(props.getProperty("FORM_ID"));
    const deadline = Date.now() + 170000;
    const checkpoint = JSON.parse(props.getProperty("DELIVERY_CURSOR") || '{"time":1,"id":""}');

    // Sweep stored responses so missed triggers or failures before queueing are recoverable.
    const batch = form.getResponses(new Date(checkpoint.time - 1))
      .map(response => ({ response, time: response.getTimestamp().getTime(), id: response.getId() }))
      .filter(entry => entry.time > checkpoint.time || (entry.time === checkpoint.time && entry.id > checkpoint.id))
      .sort((a, b) => a.time - b.time || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))
      .slice(0, 12);
    for (const entry of batch) {
      if (Date.now() >= deadline) return;
      trackDelivery_(entry.response, props, config);
      props.setProperty("DELIVERY_CURSOR", JSON.stringify({ time: entry.time, id: entry.id }));
    }

    const retries = Object.entries(props.getProperties())
      .filter(([key]) => key.startsWith("RETRY_"))
      .map(([key, value]) => ({ key, ...JSON.parse(value) }))
      .filter(entry => !entry.permanent && entry.next <= Date.now())
      .sort((a, b) => a.next - b.next)
      .slice(0, 12);
    for (const entry of retries) {
      if (Date.now() >= deadline) return;
      const response = form.getResponse(entry.key.slice("RETRY_".length));
      if (!response) {
        props.setProperty(entry.key, JSON.stringify({
          code: "missing_response", permanent: true, attempts: entry.attempts, next: entry.next,
        }));
        continue;
      }
      trackDelivery_(response, props, config);
    }
  } finally {
    lock.releaseLock();
  }
}

// Set MANUAL_RETRY_RESPONSE_ID in Script Properties before running this from the editor.
function retryOneRegistration() {
  const props = PropertiesService.getScriptProperties();
  const id = props.getProperty("MANUAL_RETRY_RESPONSE_ID");
  if (!id) throw new Error("Set MANUAL_RETRY_RESPONSE_ID in Script Properties first.");
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    const config = deliveryConfig_(props);
    const form = FormApp.openById(props.getProperty("FORM_ID"));
    const response = form.getResponse(id);
    if (!response) throw new Error("The saved response could not be found.");
    props.deleteProperty("RETRY_" + id);
    trackDelivery_(response, props, config);
    console.log(props.getProperty("RETRY_" + id) ? "Response still pending." : "Response delivered.");
  } finally {
    lock.releaseLock();
  }
}
