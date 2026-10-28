"use strict";

const SHELL_CACHE = "token-tracker-phone-shell-v3";
const SHELL_ASSETS = [
  "/phone.html",
  "/phone.css",
  "/phone.js",
  "/app.js",
  "/runway.css",
  "/phone-icon.svg",
  "/phone-icon-192.png",
  "/phone-icon-512.png",
  "/apple-touch-icon.png",
  "/manifest.webmanifest"
];

self.addEventListener("install", event => {
  event.waitUntil(caches.open(SHELL_CACHE).then(async cache => {
    for (const path of SHELL_ASSETS) {
      const response = await fetch(new Request(path, { cache: "reload", credentials: "same-origin" }));
      if (!isCacheableShellResponse(path, response)) throw new Error("Phone shell asset unavailable: " + path);
      await cache.put(path, response);
    }
  }).then(() => self.skipWaiting()));
});

self.addEventListener("activate", event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(
    keys.filter(key => key.startsWith("token-tracker-phone-shell-") && key !== SHELL_CACHE)
      .map(key => caches.delete(key))
  )).then(() => self.clients.claim()));
});

self.addEventListener("fetch", event => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  // Never persist live or other API data, and never synthesize an offline roster.
  if (url.pathname.startsWith("/api/")) return;

  if (request.mode === "navigate" && url.pathname !== "/phone.html") return;
  if (request.mode === "navigate" || SHELL_ASSETS.includes(url.pathname)) {
    const path = request.mode === "navigate" ? "/phone.html" : url.pathname;
    event.respondWith(fetch(request).then(async response => {
      if (isCacheableShellResponse(path, response)) {
        const cache = await caches.open(SHELL_CACHE);
        await cache.put(path, response.clone());
      }
      return response;
    }).catch(async () => {
      const shell = await caches.match(path);
      return shell || Response.error();
    }));
  }
});

self.addEventListener("push", event => {
  let payload = {};
  try {
    const value = event.data?.json();
    if (value && typeof value === "object" && !Array.isArray(value)) payload = value;
  } catch {}

  const title = typeof payload.title === "string" && payload.title.trim()
    ? payload.title.trim().slice(0, 100)
    : "Tracker update";
  const body = typeof payload.body === "string" ? payload.body.slice(0, 240) : "";
  const target = safeRosterUrl(payload.url);
  const tag = typeof payload.tag === "string" ? payload.tag.slice(0, 80) : "tracker-update";
  event.waitUntil(self.registration.showNotification(title, {
    body,
    tag,
    data: { url: target }
  }));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const target = safeRosterUrl(event.notification.data?.url);
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(async clients => {
    for (const client of clients) {
      let url;
      try { url = new URL(client.url); } catch { continue; }
      if (url.origin !== self.location.origin || url.pathname !== "/phone.html") continue;
      const focused = await client.navigate(target).catch(() => client);
      return (focused || client).focus();
    }
    return self.clients.openWindow(target);
  }));
});

function safeRosterUrl(value) {
  if (typeof value !== "string") return "/phone.html";
  try {
    const url = new URL(value, self.location.origin);
    if (url.origin === self.location.origin && url.pathname === "/phone.html") {
      return url.pathname + url.search;
    }
  } catch {}
  return "/phone.html";
}

function isCacheableShellResponse(path, response) {
  if (!response?.ok || response.type !== "basic" || response.redirected) return false;
  try {
    const url = new URL(response.url);
    return url.origin === self.location.origin && url.pathname === path;
  } catch {
    return false;
  }
}
