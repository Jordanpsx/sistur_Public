"use strict";

const DB_NAME = "pdv";
const DB_VERSION = 2;
const QUEUE_STORE = "sync_queue";
const API_BASE = "/api/v1/pdv";
const SHELL_PATH = "/reservas/pdv";
const SHELL_CACHE = "pdv-shell-v3";
const ASSET_CACHE = "pdv-assets-v1";
const KNOWN_CACHES = [SHELL_CACHE, ASSET_CACHE];

// O shell depende destes hosts em runtime: o Tailwind Play CDN gera TODAS as
// classes utilitárias (hidden/flex/grid...) via JS no navegador — sem ele em
// cache, o F5 offline carrega o HTML mas a página renderiza quebrada (bug
// validado em staging). Fontes ficam com fallback de sistema se faltarem.
const ASSET_HOSTS = [
  "cdn.tailwindcss.com",
  "fonts.googleapis.com",
  "fonts.gstatic.com",
];

const WARM_ASSETS = [
  "https://cdn.tailwindcss.com/",
  "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=DM+Mono:wght@400;500&display=swap",
];

function openDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains("catalog")) db.createObjectStore("catalog");
      if (!db.objectStoreNames.contains(QUEUE_STORE)) db.createObjectStore(QUEUE_STORE, { keyPath: "id" });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
    req.onblocked = () => reject(new Error("IndexedDB upgrade blocked"));
  });
}

async function queueAll() {
  const db = await openDb();
  return await new Promise((resolve, reject) => {
    const req = db.transaction(QUEUE_STORE).objectStore(QUEUE_STORE).getAll();
    req.onsuccess = () => resolve((req.result || []).sort((a, b) => (a.enqueued_at || 0) - (b.enqueued_at || 0)));
    req.onerror = () => reject(req.error);
  });
}

async function queueDelete(id) {
  const db = await openDb();
  await new Promise((resolve, reject) => {
    const tx = db.transaction(QUEUE_STORE, "readwrite");
    tx.objectStore(QUEUE_STORE).delete(id);
    tx.oncomplete = resolve;
    tx.onerror = () => reject(tx.error);
  });
}

async function notifyClients(type) {
  const clientsList = await clients.matchAll({ type: "window", includeUncontrolled: true });
  for (const client of clientsList) client.postMessage({ type });
}

async function drainQueue() {
  const jobs = await queueAll();
  for (const job of jobs) {
    let response;
    try {
      response = await fetch(API_BASE + job.path, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(job.payload),
      });
    } catch {
      break;
    }

    if (!response.ok && ![400, 409].includes(response.status)) break;
    await queueDelete(job.id);
    await notifyClients("PDV_QUEUE_CHANGED");
  }
}

function cacheableResponse(response) {
  // Respostas opacas (script/link no-cors de CDN) têm ok=false e status=0,
  // mas são exatamente o que o navegador consome — devem ser cacheadas.
  return response && (response.ok || response.type === "opaque");
}

async function cacheAssets() {
  const cache = await caches.open(ASSET_CACHE);
  for (const url of WARM_ASSETS) {
    try {
      const response = await fetch(url, { mode: "no-cors" });
      if (cacheableResponse(response)) await cache.put(url, response.clone());
      // As URLs dos .woff2 vivem dentro do CSS das fontes — extrai e aquece
      // também, senão o offline cai para fonte de sistema (aceitável, mas
      // evitável quando o CSS é legível/CORS).
      if (response.ok && url.includes("fonts.googleapis.com")) {
        const css = await response.clone().text();
        const fontUrls = [...css.matchAll(/url\((https:\/\/fonts\.gstatic\.com[^)]+)\)/g)].map((m) => m[1]);
        for (const fontUrl of fontUrls.slice(0, 12)) {
          try {
            const fontRes = await fetch(fontUrl, { mode: "no-cors" });
            if (cacheableResponse(fontRes)) await cache.put(fontUrl, fontRes.clone());
          } catch {}
        }
      }
    } catch {}
  }
}

async function assetCacheFirst(request) {
  const cached = await caches.match(request, { ignoreVary: true });
  if (cached) {
    // Revalida em background sem bloquear a resposta
    fetch(request).then(async (fresh) => {
      if (cacheableResponse(fresh)) {
        const cache = await caches.open(ASSET_CACHE);
        await cache.put(request, fresh.clone());
      }
    }).catch(() => {});
    return cached;
  }
  const response = await fetch(request);
  if (cacheableResponse(response)) {
    const cache = await caches.open(ASSET_CACHE);
    await cache.put(request, response.clone());
  }
  return response;
}

async function cacheShell(request = null) {
  // Timeout de 4s: em rede "zumbi" (conectada mas sem resposta) a navegação
  // não pode ficar pendurada — cai para o shell em cache via navigationFallback.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 4000);
  try {
    const req = request || new Request(SHELL_PATH, { credentials: "include" });
    const response = await fetch(req, { signal: controller.signal });
    if (response.ok && response.headers.get("content-type")?.includes("text/html")) {
      const cache = await caches.open(SHELL_CACHE);
      await cache.put(SHELL_PATH, response.clone());
    }
    return response;
  } finally {
    clearTimeout(timer);
  }
}

async function navigationFallback(request) {
  try {
    return await cacheShell(request);
  } catch {
    const cached = await caches.match(SHELL_PATH);
    if (cached) return cached;
    return new Response("PDV indisponível offline. Abra o PDV uma vez online antes de usar o modo offline.", {
      status: 503,
      headers: { "Content-Type": "text/plain; charset=utf-8" },
    });
  }
}

self.addEventListener("install", (event) => {
  event.waitUntil(Promise.all([cacheAssets(), self.skipWaiting()]));
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    // Remove caches de versões antigas do PDV (ex.: pdv-shell-v1)
    const names = await caches.keys();
    await Promise.all(
      names
        .filter((n) => n.startsWith("pdv-") && !KNOWN_CACHES.includes(n))
        .map((n) => caches.delete(n))
    );
    await clients.claim();
  })());
});

self.addEventListener("sync", (event) => {
  if (event.tag === "pdv-sale-sync") event.waitUntil(drainQueue());
});

self.addEventListener("message", (event) => {
  if (event.data?.type === "PDV_QUEUE_DRAIN") event.waitUntil(drainQueue());
  if (event.data?.type === "PDV_CACHE_SHELL") event.waitUntil(Promise.all([cacheShell(), cacheAssets()]));
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // Shell HTML: network-first com fallback ao cache (F5 offline)
  if (event.request.mode === "navigate" && url.origin === location.origin && url.pathname === SHELL_PATH) {
    event.respondWith(navigationFallback(event.request));
    return;
  }

  // Assets externos do shell (Tailwind CDN, fontes): cache-first com
  // revalidação em background — é isto que faz o F5 offline renderizar
  // a página inteira, não só o HTML.
  if (event.request.method === "GET" && ASSET_HOSTS.includes(url.hostname)) {
    event.respondWith(assetCacheFirst(event.request));
  }
});
