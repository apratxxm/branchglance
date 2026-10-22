import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import vm from "node:vm";

const phoneJs = await readFile(new URL("../token_tracker/static/phone.js", import.meta.url), "utf8");
const phoneSw = await readFile(new URL("../token_tracker/static/phone-sw.js", import.meta.url), "utf8");

function response(status, value = {}) {
  return { ok: status >= 200 && status < 300, status, json: async () => value };
}

function makeSubscription({ key = [1, 2, 3, 4], unsubscribeResult = true } = {}) {
  const state = { unsubscribed: false };
  return {
    state,
    options: { applicationServerKey: Uint8Array.from(key).buffer },
    toJSON: () => ({ endpoint: "https://push.example/subscription", keys: { p256dh: "p", auth: "a" } }),
    async unsubscribe() { state.unsubscribed = true; return unsubscribeResult; }
  };
}

function phoneHarness({ config = { pushReady: true, publicKey: "AQIDBA" }, subscription = null, fetcher } = {}) {
  const calls = [];
  const button = { disabled: true, textContent: "Alerts", listeners: {}, addEventListener(type, callback) { this.listeners[type] = callback; } };
  const status = { textContent: "Checking alert setup…" };
  const registerCalls = [];
  let permissionCalls = 0;
  let loadHandler = null;
  const currentSubscription = { value: subscription };
  const registration = {
    pushManager: {
      async getSubscription() { return currentSubscription.value; },
      async subscribe() { return makeSubscription(); }
    }
  };
  const notifications = {
    permission: "default",
    async requestPermission() { permissionCalls += 1; this.permission = "granted"; return "granted"; }
  };
  const fetchCalls = [];
  const fetchMock = async (url, options = {}) => {
    const call = { url, options };
    fetchCalls.push(call);
    if (fetcher) return fetcher(call, { calls, currentSubscription });
    if (url === "/api/phone/config") return response(200, config);
    if (options.method === "POST") return response(200);
    if (options.method === "DELETE") return response(200);
    return response(404);
  };
  const navigator = {
    serviceWorker: {
      ready: Promise.resolve(registration),
      async register(...args) { registerCalls.push(args); return registration; }
    }
  };
  const window = {
    isSecureContext: true,
    PushManager: function PushManager() {},
    Notification: notifications,
    addEventListener(type, callback) { if (type === "load") loadHandler = callback; }
  };
  const context = {
    document: { querySelector(selector) { return selector === "#phone-notifications" ? button : selector === "#phone-setup-status" ? status : null; } },
    navigator, window, location: { hostname: "localhost" }, Notification: notifications,
    fetch: fetchMock,
    atob: value => Buffer.from(value, "base64").toString("binary"),
    Uint8Array, URL, Buffer, console
  };
  vm.runInNewContext(phoneJs, context, { filename: "phone.js" });
  return {
    button, status, notifications, navigator, registration, registerCalls, fetchCalls,
    currentSubscription, permissionCalls: () => permissionCalls,
    async load() { await loadHandler(); await new Promise(resolve => setImmediate(resolve)); },
    async click() { await button.listeners.click(); }
  };
}

function serviceWorkerHarness(networkFetch = async request => ({
  ok: true, status: 200, type: "basic", redirected: false, url: request.url,
  clone() { return this; }
})) {
  const handlers = new Map();
  const puts = [];
  const cached = new Map();
  const opened = [];
  const shown = [];
  const self = {
    location: { origin: "https://tracker.example" },
    registration: { async showNotification(title, options) { shown.push({ title, options }); } },
    addEventListener(type, listener) { handlers.set(type, listener); },
    clients: {
      async matchAll() { return []; },
      async openWindow(url) { opened.push(url); return null; },
      async claim() {}
    },
    skipWaiting: async () => {}
  };
  const caches = {
    async open() { return { async put(key, value) { puts.push(key); cached.set(key, value); } }; },
    async match(key) { return cached.get(typeof key === "string" ? key : key.url) || null; },
    async keys() { return []; }, async delete() { return true; }
  };
  const context = {
    self, caches, URL, Request, Response, Promise,
    fetch: networkFetch,
    console
  };
  vm.runInNewContext(phoneSw, context, { filename: "phone-sw.js" });
  return {
    handlers, puts, cached, opened, shown,
    async fetch(request) {
      let promise = null;
      const event = { request, respondWith(value) { promise = Promise.resolve(value); } };
      handlers.get("fetch")(event);
      return promise ? await promise : null;
    },
    async push(payload) {
      let promise = null;
      handlers.get("push")({ data: { json: () => payload }, waitUntil(value) { promise = value; } });
      await promise;
    },
    async click(url) {
      let promise = null;
      const event = { notification: { data: { url }, close() {} }, waitUntil(value) { promise = value; } };
      handlers.get("notificationclick")(event);
      await promise;
    }
  };
}

test("permission is requested only after the explicit enable click", async () => {
  const app = phoneHarness();
  await app.load();
  assert.equal(app.permissionCalls(), 0);
  assert.equal(app.button.disabled, false);
  assert.equal(app.button.textContent, "Enable alerts");
  await app.click();
  assert.equal(app.permissionCalls(), 1);
  assert.equal(app.fetchCalls.some(call => call.options.method === "POST"), true);
});

test("an existing browser subscription is not reported enabled until the server saves it", async () => {
  const subscription = makeSubscription();
  let posts = 0;
  const app = phoneHarness({ subscription, fetcher: async ({ url, options }) => {
    if (url === "/api/phone/config") return response(200, { pushReady: true, publicKey: "AQIDBA" });
    if (options.method === "POST") return ++posts === 1 ? response(503) : response(200);
    return response(200);
  } });
  await app.load();
  assert.match(app.status.textContent, /could not save/i);
  assert.equal(app.status.textContent.includes("Notifications are enabled"), false);
  assert.equal(app.button.textContent, "Finish alert setup");
  assert.equal(app.permissionCalls(), 0);
  await app.click();
  assert.equal(app.status.textContent, "Notifications are enabled for this phone.");
  assert.equal(posts, 2);
});

test("VAPID rotation requires an explicit remove then enable sequence", async () => {
  const subscription = makeSubscription({ key: [9, 9, 9, 9] });
  const app = phoneHarness({ subscription });
  await app.load();
  assert.match(app.status.textContent, /key changed/i);
  assert.equal(app.button.textContent, "Turn alerts off");
  assert.equal(app.fetchCalls.some(call => call.options.method === "POST"), false);
  await app.click();
  assert.equal(subscription.state.unsubscribed, true);
  assert.equal(app.button.textContent, "Enable alerts");
  assert.equal(app.permissionCalls(), 0);
});

test("an existing subscription can be removed while pushReady is false", async () => {
  const subscription = makeSubscription();
  const app = phoneHarness({ config: { pushReady: false }, subscription });
  await app.load();
  assert.equal(app.button.disabled, false);
  await app.click();
  const deletion = app.fetchCalls.find(call => call.options.method === "DELETE");
  assert.ok(deletion);
  assert.deepEqual(JSON.parse(deletion.options.body), { subscription: subscription.toJSON() });
  assert.equal(subscription.state.unsubscribed, true);
  assert.match(app.status.textContent, /subscription is removed/i);
});

test("a failed server unsubscribe does not claim that alerts are off", async () => {
  const subscription = makeSubscription();
  const app = phoneHarness({ config: { pushReady: false }, subscription, fetcher: async ({ url }) =>
    url === "/api/phone/config" ? response(200, { pushReady: false }) : response(503)
  });
  await app.load();
  await app.click();
  assert.match(app.status.textContent, /could not remove/i);
  assert.equal(app.status.textContent.includes("subscription is removed"), false);
  assert.equal(subscription.state.unsubscribed, false);
  assert.equal(app.button.textContent, "Turn alerts off");
});

test("service worker never intercepts or caches API requests", async () => {
  const sw = serviceWorkerHarness();
  const result = await sw.fetch({ method: "GET", url: "https://tracker.example/api/live", mode: "cors" });
  assert.equal(result, null);
  assert.deepEqual(sw.puts, []);
  const post = await sw.fetch({ method: "POST", url: "https://tracker.example/api/phone/subscription", mode: "cors" });
  assert.equal(post, null);
});

test("static shell 401 and server errors are returned and never cached", async () => {
  for (const status of [401, 500]) {
    const sw = serviceWorkerHarness(async request => ({
      ok: false, status, type: "basic", redirected: false, url: request.url,
      clone() { return this; }
    }));
    const path = status === 401 ? "/phone.html" : "/phone.css";
    const mode = status === 401 ? "navigate" : "same-origin";
    const result = await sw.fetch({ method: "GET", url: `https://tracker.example${path}`, mode });
    assert.equal(result.status, status);
    assert.deepEqual(sw.puts, []);
    assert.equal(sw.cached.has(path), false);
  }
});

test("network failure may use only the previously cached phone shell", async () => {
  const sw = serviceWorkerHarness(async () => { throw new Error("offline"); });
  const shell = { shell: true };
  sw.cached.set("/phone.html", shell);
  assert.equal(await sw.fetch({ method: "GET", url: "https://tracker.example/phone.html", mode: "navigate" }), shell);
  assert.equal(await sw.fetch({ method: "GET", url: "https://tracker.example/api/live", mode: "cors" }), null);
});

test("push notification external destinations are replaced with the phone roster", async () => {
  const sw = serviceWorkerHarness();
  await sw.push({ title: "Tracker update", body: "Ready", url: "https://attacker.example/" });
  assert.equal(sw.shown[0].title, "Tracker update");
  assert.equal(sw.shown[0].options.data.url, "/phone.html");
  await sw.click(sw.shown[0].options.data.url);
  assert.deepEqual(sw.opened, ["/phone.html"]);
});

test("manifest icon sizes and phone shell hooks are present", async () => {
  const manifest = JSON.parse(await readFile(new URL("../token_tracker/static/manifest.webmanifest", import.meta.url), "utf8"));
  const html = await readFile(new URL("../token_tracker/static/phone.html", import.meta.url), "utf8");
  const icon192 = await readFile(new URL("../token_tracker/static/phone-icon-192.png", import.meta.url));
  const icon512 = await readFile(new URL("../token_tracker/static/phone-icon-512.png", import.meta.url));
  const appleIcon = await readFile(new URL("../token_tracker/static/apple-touch-icon.png", import.meta.url));
  assert.ok(manifest.icons.some(icon => icon.sizes === "192x192" && icon.type === "image/png"));
  assert.ok(manifest.icons.some(icon => icon.sizes === "512x512" && icon.type === "image/png"));
  assert.deepEqual([icon192.readUInt32BE(16), icon192.readUInt32BE(20)], [192, 192]);
  assert.deepEqual([icon512.readUInt32BE(16), icon512.readUInt32BE(20)], [512, 512]);
  assert.deepEqual([appleIcon.readUInt32BE(16), appleIcon.readUInt32BE(20)], [180, 180]);
  assert.ok(html.includes("rel=\"apple-touch-icon\""));
  for (const id of ["brand-name", "sync-age", "notch-counts", "strip", "resets", "runway-greeter-status", "analytics", "chat-search", "chat-list", "popover", "phone-notifications", "phone-setup-status"]) {
    assert.ok(html.includes(`id="${id}"`), `missing phone shell ID: ${id}`);
  }
  assert.match(html, /<div id="notch-counts" class="notch-counts phone-counts" aria-label="Live chat counts" aria-live="polite"><\/div>/);
  assert.doesNotMatch(html, /id="notch-counts"[^>]*>\s*0\b/);
  assert.ok(html.indexOf("class=\"phone-settings-bar\"") < html.indexOf("<main class=\"widget\">"));
  assert.ok(html.includes("window.__TOKEN_TRACKER_RUNWAY__ = true"));
  assert.ok(html.includes("window.__TOKEN_TRACKER_PHONE__ = true"));
});
