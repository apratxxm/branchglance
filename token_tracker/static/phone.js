(() => {
  "use strict";

  const button = document.querySelector("#phone-notifications");
  const status = document.querySelector("#phone-setup-status");
  if (!button || !status) return;

  const setStatus = message => { status.textContent = message; };
  const setEnabled = (enabled, label) => {
    button.disabled = !enabled;
    if (label) button.textContent = label;
  };
  const secure = window.isSecureContext || ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);
  let registration = null;
  let config = null;
  let subscription = null;
  let keyMismatch = false;
  let serverConfirmed = false;
  let busy = false;

  function decodeApplicationServerKey(value) {
    if (typeof value !== "string" || !value) throw new Error("The tracker server has no push key.");
    const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
    const raw = atob(normalized + "=".repeat((4 - normalized.length % 4) % 4));
    return Uint8Array.from(raw, character => character.charCodeAt(0));
  }

  async function requestConfig() {
    const response = await fetch("/api/phone/config", { cache: "no-store", credentials: "same-origin" });
    if (!response.ok) throw new Error("The paired tracker server is unavailable.");
    const value = await response.json();
    if (!value || typeof value !== "object" || typeof value.pushReady !== "boolean") {
      throw new Error("The tracker server returned an invalid notification setup response.");
    }
    if (value.pushReady && (typeof value.publicKey !== "string" || !value.publicKey)) {
      throw new Error("The tracker server is missing its push public key.");
    }
    return value;
  }

  async function readSubscription() {
    registration = await navigator.serviceWorker.ready;
    subscription = await registration.pushManager.getSubscription();
  }

  function subscriptionMatchesCurrentKey(value) {
    const existingKey = value?.options?.applicationServerKey;
    if (!existingKey || !config?.publicKey) return true;
    const currentKey = decodeApplicationServerKey(config.publicKey);
    const previousKey = new Uint8Array(existingKey);
    return currentKey.length === previousKey.length && currentKey.every((byte, index) => byte === previousKey[index]);
  }

  async function saveSubscription(value) {
    const response = await fetch("/api/phone/subscription", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      cache: "no-store",
      body: JSON.stringify({ subscription: value.toJSON() })
    });
    if (!response.ok) throw new Error("The tracker server could not save this phone subscription.");
  }

  async function removeSubscription(value) {
    const response = await fetch("/api/phone/subscription", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      cache: "no-store",
      body: JSON.stringify({ subscription: value.toJSON() })
    });
    if (!response.ok) throw new Error("The tracker server could not remove this phone subscription.");
    const removed = await value.unsubscribe();
    if (removed === false) throw new Error("The server removed its subscription, but this browser could not remove the local one.");
  }

  async function refreshSetup() {
    if (!secure) {
      setEnabled(false, "Alerts");
      setStatus("Notifications need HTTPS or a local secure connection.");
      return;
    }
    if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
      setEnabled(false, "Alerts");
      setStatus("This browser does not support push notifications.");
      return;
    }

    try {
      await navigator.serviceWorker.register("/phone-sw.js", { scope: "/" });
      await readSubscription();
    } catch {
      setEnabled(false, "Alerts");
      setStatus("The phone shell could not prepare its notification service.");
      return;
    }

    try {
      config = await requestConfig();
    } catch (error) {
      config = null;
      serverConfirmed = false;
      keyMismatch = false;
      setEnabled(Boolean(subscription), subscription ? "Turn alerts off" : "Alerts");
      setStatus(subscription
        ? (error instanceof Error ? error.message + " Existing alerts can still be removed." : "Server status is unavailable. Existing alerts can still be removed.")
        : (error instanceof Error ? error.message : "Notification setup is unavailable."));
      return;
    }

    if (!config.pushReady) {
      serverConfirmed = false;
      keyMismatch = false;
      setEnabled(Boolean(subscription), subscription ? "Turn alerts off" : "Alerts");
      setStatus(subscription
        ? "Push setup is off on the server. You can remove this phone's existing subscription."
        : "Push notifications are not configured on this tracker server yet.");
      return;
    }

    if (subscription) {
      try {
        keyMismatch = !subscriptionMatchesCurrentKey(subscription);
      } catch {
        keyMismatch = true;
      }
      if (keyMismatch) {
        serverConfirmed = false;
        setEnabled(true, "Turn alerts off");
        setStatus("The server push key changed. Turn alerts off, then enable them again.");
        return;
      }
      try {
        await saveSubscription(subscription);
        serverConfirmed = true;
        setEnabled(true, "Turn alerts off");
        setStatus("Notifications are enabled for this phone.");
      } catch (error) {
        serverConfirmed = false;
        setEnabled(true, "Finish alert setup");
        setStatus(error instanceof Error ? error.message : "The server has not confirmed this subscription.");
      }
      return;
    }

    serverConfirmed = false;
    keyMismatch = false;
    if (Notification.permission === "denied") {
      setEnabled(false, "Alerts blocked");
      setStatus("Notifications are blocked in browser settings.");
    } else {
      setEnabled(true, "Enable alerts");
      setStatus("Alerts are off. Choose Enable alerts to allow notifications.");
    }
  }

  async function enableAlerts() {
    if (!config?.pushReady) throw new Error("Push notifications are not configured on this tracker server yet.");
    const permission = await Notification.requestPermission();
    if (permission !== "granted") {
      setStatus(permission === "denied" ? "Notifications were blocked in browser settings." : "Notification permission was not granted.");
      if (permission === "denied") setEnabled(false, "Alerts blocked");
      return;
    }
    const applicationServerKey = decodeApplicationServerKey(config.publicKey);
    const activeRegistration = registration || await navigator.serviceWorker.ready;
    const value = await activeRegistration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey });
    try {
      await saveSubscription(value);
    } catch (error) {
      await value.unsubscribe().catch(() => {});
      throw error;
    }
    subscription = value;
    keyMismatch = false;
    serverConfirmed = true;
    setStatus("Notifications are enabled for this phone.");
    setEnabled(true, "Turn alerts off");
  }

  button.addEventListener("click", async () => {
    if (busy) return;
    busy = true;
    setEnabled(false, subscription ? "Updating alerts…" : "Enabling…");
    try {
      if (subscription && (keyMismatch || !config?.pushReady || serverConfirmed)) {
        await removeSubscription(subscription);
        subscription = null;
        keyMismatch = false;
        serverConfirmed = false;
        if (config?.pushReady) {
          setEnabled(true, "Enable alerts");
          setStatus("Alerts are off. Choose Enable alerts to set up this phone again.");
        } else {
          setEnabled(false, "Alerts");
          setStatus("This phone's subscription is removed. Push setup is not ready on the server.");
        }
      } else if (subscription) {
        await saveSubscription(subscription);
        serverConfirmed = true;
        setEnabled(true, "Turn alerts off");
        setStatus("Notifications are enabled for this phone.");
      } else {
        await enableAlerts();
      }
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Notification setup failed.");
      const canRetry = Boolean(subscription || config?.pushReady) && !(Notification.permission === "denied" && !subscription);
      setEnabled(canRetry, subscription
        ? (serverConfirmed || keyMismatch || !config?.pushReady ? "Turn alerts off" : "Finish alert setup")
        : "Try again");
    } finally {
      busy = false;
    }
  });

  window.addEventListener("load", () => { void refreshSetup(); }, { once: true });
})();
