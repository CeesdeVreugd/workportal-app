/* WorkPortal service worker: installeerbaar als app + pushmeldingen.
   Er wordt bewust niets gecachet, zodat je altijd de actuele gegevens ziet. */
self.addEventListener("install", function () { self.skipWaiting(); });
self.addEventListener("activate", function (e) { e.waitUntil(self.clients.claim()); });
self.addEventListener("fetch", function () { /* netwerk gaat gewoon door */ });

self.addEventListener("push", function (event) {
  var data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = { title: "WorkPortal", body: event.data ? event.data.text() : "" }; }
  event.waitUntil(self.registration.showNotification(data.title || "WorkPortal", {
    body: data.body || "",
    icon: "/static/img/wp-square-192.png",
    badge: "/static/img/wp-square-192.png",
    data: { url: data.url || "/" },
    requireInteraction: true,
    vibrate: [300, 150, 300]
  }));
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (list) {
    for (var i = 0; i < list.length; i++) {
      if ("focus" in list[i]) { list[i].navigate(url); return list[i].focus(); }
    }
    return self.clients.openWindow(url);
  }));
});
