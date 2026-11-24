import { createHash, timingSafeEqual } from 'node:crypto';

const phonePattern = /^\+[1-9][0-9]{7,14}$/;
const messageId = /^3EB0[A-F0-9]{18}$/;
const hash = value => createHash('sha256').update(value).digest();
const isPhoneJid = value => /^\d+(?::\d+)?@s\.whatsapp\.net$/.test(value || '');
let groupStore;
const menuSessions = new Map();

async function portalCover(value) {
  if (typeof value !== 'string' || value.length > 800) return null;
  try {
    const url = new URL(value);
    if (url.origin !== 'https://paranaatual.com.br' || url.username || url.password ||
        !url.pathname.startsWith('/media/') || url.search || url.hash) return null;
    const response = await fetch(url, { redirect: 'error', signal: AbortSignal.timeout(2500) });
    const limit = 5 * 1024 * 1024;
    if (!response.ok || !/^image\/(?:jpeg|png|webp)(?:;|$)/i.test(response.headers.get('content-type') || '') ||
        Number(response.headers.get('content-length')) > limit || !response.body) return null;
    const reader = response.body.getReader();
    try {
      const chunks = [];
      let size = 0;
      while (true) {
        const { done, value: bytes } = await reader.read();
        if (done) return size ? Buffer.concat(chunks, size) : null;
        size += bytes.byteLength;
        if (size > limit) return null;
        chunks.push(Buffer.from(bytes));
      }
    } finally {
      await reader.cancel().catch(() => {});
    }
  } catch { return null; }
}

export function installRadarRoutes(app, { getStatus, sendPrivate, sendGroup, store }) {
  groupStore = store;
  const authorize = (req, res, next) => {
    const token = process.env.RADAR_WHATSAPP_TOKEN;
    const authorization = req.get('Authorization') || '';
    if (!token || !timingSafeEqual(hash(authorization), hash('Bearer ' + token))) {
      return res.sendStatus(401);
    }
    res.set('Cache-Control', 'no-store');
    return next();
  };
  const action = handler => async (req, res) => {
    try { await handler(req, res); }
    catch (error) {
      const status = [400, 409].includes(error.status) ? error.status : 503;
      res.status(status).json({ message: status === 503 ? 'WhatsApp indisponível no momento.' : error.message });
    }
  };
  app.get('/radar/status', authorize, (_req, res) => {
    res.json({ connected: getStatus().state === 'connected' });
  });
  app.get('/radar/group/settings', authorize, (_req, res) => res.json(store.state()));
  app.get('/radar/group/list', authorize, action(async (_req, res) => res.json({ groups: await store.groups() })));
  app.post('/radar/group/settings', authorize, action(async (req, res) => res.json(await store.save(req.body))));
  app.post('/radar/group/send', authorize, action(async (req, res) => {
    const data = req.body || {};
    if (!store.allows(data)) return res.status(409).json({ message: 'O grupo ou a automação mudou.' });
    if (typeof data.text !== 'string' || !data.text.length || data.text.length > 3500 ||
        typeof data.id !== 'string' || !messageId.test(data.id)) return res.sendStatus(400);
    const cover = data.kind === 'article' ? await portalCover(data.cover) : null;
    if (!store.allows(data)) return res.status(409).json({ message: 'O grupo ou a automação mudou.' });
    const accepted = await sendGroup(data.group_id, data.text, data.id, cover);
    return res.json({ accepted });
  }));
  app.post('/radar/send', authorize, async (req, res) => {
    const { phone, text, id } = req.body || {};
    if (typeof phone !== 'string' || !phonePattern.test(phone) ||
        typeof text !== 'string' || !text.length || text.length > 3500 ||
        typeof id !== 'string' || !messageId.test(id)) {
      return res.sendStatus(400);
    }
    try {
      return res.json({ accepted: await sendPrivate(phone, { text }, id) });
    } catch {
      return res.sendStatus(503);
    }
  });
}

export async function handleRadarMessage(message, text, reply, resolvePhone) {
  const url = process.env.RADAR_WHATSAPP_WEBHOOK;
  const token = process.env.RADAR_WHATSAPP_TOKEN;
  const key = message?.key || {};
  const remote = key.remoteJid || '';
  const group = remote.endsWith('@g.us');
  const radar = /^\s*RADAR(?:\s|$)/i.test(text || '');
  const menu = /^\s*\/?menu\s*$/i.test(text || '');
  const choice = /^\s*[1-9]\s*$/.test(text || '');
  const groupCommand = menu || choice;
  if (!url || !token || key.fromMe ||
      (group ? !(radar || groupCommand) || !groupStore?.handles(remote)
        : !radar || !/@(?:lid|s\.whatsapp\.net)$/.test(remote))) return false;
  const id = key.id || '';
  if (!/^[A-Za-z0-9_-]{8,90}$/.test(id) || text.length > 160) return true;
  try {
    const candidates = group ? [key.participant, key.participantAlt] : [remote, key.remoteJidAlt];
    let jid = candidates.find(isPhoneJid);
    if (!jid && typeof resolvePhone === 'function') {
      const lid = candidates.find(value => /^\d+(?::\d+)?@lid$/.test(value || ''));
      if (lid) jid = await resolvePhone(lid);
    }
    if (!isPhoneJid(jid)) return true;
    const phone = '+' + jid.split(/[:@]/)[0];
    const key = remote + ':' + phone;
    const current = Date.now();
    if (group && choice && (menuSessions.get(key) || 0) <= current) return true;
    const response = await fetch(url, { method: 'POST', redirect: 'error', signal: AbortSignal.timeout(5000),
      headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone, id, text, ...(group ? { group_id: remote } : {}) }),
    });
    if (!response.ok) return true;
    const data = await response.json();
    if (typeof data.reply === 'string' && data.reply.length <= 6000 &&
        (!group || groupStore.handles(remote))) {
      if (group && (menu || choice)) {
        if (menuSessions.size >= 500) {
          for (const [entry, expiry] of menuSessions) if (expiry <= current) menuSessions.delete(entry);
          if (menuSessions.size >= 500) menuSessions.delete(menuSessions.keys().next().value);
        }
        menuSessions.set(key, current + 5 * 60 * 1000);
      }
      await reply(data.reply);
    }
  } catch {
    // O gateway compartilhado continua disponível durante falhas do portal.
  }
  return true;
}

