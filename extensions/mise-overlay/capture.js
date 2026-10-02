// Runs in the page's own JavaScript world so it can see the odds responses the page
// already requested. It sends no request of its own and reads only the event-list
// queries below: never the bet, account, payment or identity services.
(() => {
  const ODDS_QUERY =
    /^https:\/\/content\.mojp-sgdigital-jel\.com\/content-service\/api\/v1\/q\/(time-band-event-list|event-list|events-by-ids|popular-bets-event-list)\?/;

  const forward = (url, body) => {
    if (typeof body === "string" && body) {
      window.postMessage({ source: "mise-overlay-capture", url, body }, window.location.origin);
    }
  };

  const originalFetch = window.fetch;
  window.fetch = async function (...args) {
    const response = await originalFetch.apply(this, args);
    try {
      if (ODDS_QUERY.test(response.url)) {
        response.clone().text().then((body) => forward(response.url, body), () => {});
      }
    } catch (_error) {
      // Never let the overlay break the page.
    }
    return response;
  };

  const originalSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function (...args) {
    this.addEventListener("load", () => {
      try {
        if (!ODDS_QUERY.test(this.responseURL)) return;
        const url = this.responseURL;
        if (this.responseType === "" || this.responseType === "text") {
          forward(url, this.responseText);
        } else if (this.responseType === "json") {
          forward(url, JSON.stringify(this.response));
        } else if (this.responseType === "blob" && this.response) {
          // The sportsbook's fetch polyfill runs over XHR and asks for a blob.
          this.response.text().then((body) => forward(url, body), () => {});
        }
      } catch (_error) {
        // Never let the overlay break the page.
      }
    });
    return originalSend.apply(this, args);
  };
})();
