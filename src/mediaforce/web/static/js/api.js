// Lightweight fetch helpers for Mediaforce web.
(function () {
  async function requestJson(url, opts = {}) {
    const method = (opts.method || "GET").toUpperCase();
    const headers = Object.assign({}, opts.headers || {});
    const init = { method, headers };

    if (Object.prototype.hasOwnProperty.call(opts, "body")) {
      init.body = JSON.stringify(opts.body);
      if (!headers["Content-Type"]) headers["Content-Type"] = "application/json";
    }

    const resp = await fetch(url, init);
    let data;
    try {
      data = await resp.json();
    } catch (_) {
      data = undefined;
    }

    if (!resp.ok) {
      const error = (data && (data.error || data.detail)) || resp.statusText || "request failed";
      return { ok: false, status: resp.status, data, error };
    }
    return { ok: true, status: resp.status, data };
  }

  async function getJson(url, opts = {}) {
    return requestJson(url, Object.assign({}, opts, { method: "GET" }));
  }

  async function postJson(url, body, opts = {}) {
    return requestJson(url, Object.assign({}, opts, { method: "POST", body }));
  }

  window.mfApi = { requestJson, getJson, postJson };
})();
