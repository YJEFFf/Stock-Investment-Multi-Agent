/* 금융 데이터와 로그인 응답은 캐시하지 않는다. 캐시에는 공개 앱 외형만 들어간다. */
const CACHE = 'sima-shell-v4';
const SHELL = ['/', '/assets/app.css', '/assets/app.js', '/assets/icon.svg', '/assets/icon-192.png', '/assets/apple-touch-icon.png'];
self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin || !SHELL.includes(url.pathname)) return;
  event.respondWith(fetch(event.request).then(response => {
    if (response.ok) { const copy = response.clone(); caches.open(CACHE).then(cache => cache.put(event.request, copy)); }
    return response;
  }).catch(() => caches.match(url.pathname)));
});
self.addEventListener('push', event => {
  let data = {title: 'SIMA 알림', body: '앱에서 새로운 소식을 확인하세요.', url: '/#alerts'};
  try { Object.assign(data, event.data.json()); } catch (_) { /* 본문이 없어도 사용자에게 알린다. */ }
  event.waitUntil(self.registration.showNotification(String(data.title), {
    body: String(data.body || ''), icon: '/assets/icon-192.png', badge: '/assets/icon-192.png',
    tag: String(data.id || 'sima'), data: {url: data.url},
  }));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || '/#alerts', self.location.origin);
  if (target.origin !== self.location.origin) return;
  event.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(async windows => {
    for (const client of windows) {
      if (new URL(client.url).origin === target.origin) {
        await client.navigate(target.href);
        return client.focus();
      }
    }
    return self.clients.openWindow(target.href);
  }));
});
