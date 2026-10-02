// Relays captured odds responses to the local service and returns its verdicts.
// Keep SERVICE in step with DEFAULT_PORT in sports_betting/overlay/server.py and the
// manifest's host permission; tests/test_overlay.py checks all three agree.
const SERVICE = "http://127.0.0.1:8765";
const PAGE_ORIGIN = "https://miseojeuplus.espacejeux.com/";

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!sender.url || !sender.url.startsWith(PAGE_ORIGIN)) return false;
  if (message?.type === "health") {
    fetch(`${SERVICE}/health`)
      .then((response) => response.json())
      .then(sendResponse, (error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (message?.type !== "evaluate" || typeof message.body !== "string") return false;
  fetch(`${SERVICE}/evaluate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: message.body,
  })
    .then((response) => response.json())
    .then(sendResponse, (error) => sendResponse({ ok: false, error: String(error) }));
  return true;
});
