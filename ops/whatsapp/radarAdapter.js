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

export async function getRadarAdminJids() {
  const base = process.env.PARANAATUAL_AD_REPORTS_URL;
  const token = process.env.PARANAATUAL_AD_REPORTS_TOKEN;
  if (!base || !token) return [];
  const response = await fetch(base.replace(/\/+$/, '') + '/v1/admin-jids', {
    headers: { Authorization: 'Bearer ' + token },
    redirect: 'error', signal: AbortSignal.timeout(3000),
  });
  if (!response.ok) throw new Error('Cadastro de administradores indisponível.');
  const data = await response.json();
  return Array.isArray(data.jids) ? data.jids.filter(isPhoneJid) : [];
}

function senderIds(key, group) {
  const knownPhone = group ? key.participantPn : key.senderPn;
  const phone = typeof knownPhone === 'string' && /^[1-9]\d{7,14}$/.test(knownPhone)
    ? knownPhone + '@s.whatsapp.net' : knownPhone;
  const aliases = group ? [key.participant, key.participantAlt, key.participantLid]
    : [key.remoteJid, key.remoteJidAlt, key.senderLid];
  return [...new Set([phone, ...aliases].filter(value =>
    /^\d+(?::\d+)?@(?:s\.whatsapp\.net|lid)$/.test(value || '')
  ).map(value => value.replace(/:\d+(?=@)/, '')))];
}

async function boundedAliases(resolveAliases, candidates, groupId) {
  let timer;
  try {
    return await Promise.race([
      resolveAliases(candidates, groupId),
      new Promise(resolve => { timer = setTimeout(() => resolve([]), 5500); }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

export async function handleRadarMessage(message, text, reply, resolveAliases) {
  const url = process.env.RADAR_WHATSAPP_WEBHOOK;
  const token = process.env.RADAR_WHATSAPP_TOKEN;
  const messageKey = message?.key || {};
  const remote = messageKey.remoteJid || '';
  const group = remote.endsWith('@g.us');
  const radar = /^\s*RADAR(?:\s|$)/i.test(text || '');
  const menu = /^\s*\/?menu\s*$/i.test(text || '');
  const choice = /^\s*[1-9]\s*$/.test(text || '');
  if (group && !text && message?.messageStubType === 2 && groupStore?.handles(remote)) {
    console.info('Radar recebeu mensagem de grupo sem conteúdo descriptografado.');
  }
  if (!url || !token ||
      (group ? !(radar || menu || choice) || !groupStore?.handles(remote)
        : !radar || !/@(?:lid|s\.whatsapp\.net)$/.test(remote))) return false;

  if (messageKey.fromMe) {
    if (group && menu) console.info('Radar menu recebido da conta vinculada.');
    return false;
  }
  const id = messageKey.id || '';
  if (!/^[A-Za-z0-9_-]{8,90}$/.test(id) || text.length > 160) {
    if (group && menu) console.warn('Radar menu descartado por formato inválido.');
    return true;
  }

  const candidates = senderIds(messageKey, group);
  const sessionKeys = candidates.map(jid => remote + ':' + jid);
  const current = Date.now();
  if (group && choice && !sessionKeys.some(key => (menuSessions.get(key) || 0) > current)) return true;

  let status;
  const tried = new Set();
  async function forward(jids) {
    for (const jid of jids) {
      if (!isPhoneJid(jid)) continue;
      const phone = '+' + jid.split(/[:@]/)[0];
      if (tried.has(phone) || tried.size >= 5) continue;
      tried.add(phone);
      const response = await fetch(url, { method: 'POST', redirect: 'error', signal: AbortSignal.timeout(5000),
        headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
        body: JSON.stringify({ phone, id, text, ...(group ? { group_id: remote } : {}) }),
      });
      status = response.status;
      if (status === 403) continue;
      if (!response.ok) return null;
      return response.json();
    }
    return null;
  }

  try {
    let data = await forward(candidates);
    if (!data && (status === undefined || status === 403) && candidates.length &&
        typeof resolveAliases === 'function') {
      const aliases = await boundedAliases(resolveAliases, candidates, group ? remote : null);
      data = await forward(aliases);
    }
    if (!data) {
      if (group && menu) console.info('Radar menu sem resposta.', status ?? 'remetente não identificado');
      else if (status >= 500) console.warn('Webhook do Radar indisponível.', status);
      return true;
    }
    if (typeof data.reply === 'string' && data.reply.length <= 6000 &&
        (!group || groupStore.handles(remote))) {
      if (group && (menu || choice)) {
        for (const key of sessionKeys) {
          if (menuSessions.size >= 500) {
            for (const [entry, expiry] of menuSessions) if (expiry <= current) menuSessions.delete(entry);
            if (menuSessions.size >= 500) menuSessions.delete(menuSessions.keys().next().value);
          }
          menuSessions.set(key, current + 5 * 60 * 1000);
        }
      }
      await reply(data.reply);
      if (group && menu) console.info('Radar menu respondido.');
    }
  } catch (error) {
    console.warn('Falha ao processar comando do Radar.', error?.name || 'Error');
  }
  return true;
}
