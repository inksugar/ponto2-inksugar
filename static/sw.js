// Service worker mínimo: só existe para o app poder ser instalado.
// Não guarda cache, para que a tela sempre mostre o horário real do servidor.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
