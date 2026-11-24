import { createHash, timingSafeEqual } from 'node:crypto';

const phonePattern = /^\+[1-9][0-9]{7,14}$/;
const messageId = /^3EB0[A-F0-9]{18}$/;
const hash = value => createHash('sha256').update(value).digest();
const isPhoneJid = value => /^\d+(?::\d+)?@s\.whatsapp\.net$/.test(value || '');
let groupStore;
function validDelivery(messages) {
  return Array.isArray(messages) && messages.length > 0 && messages.length <= 6 &&
    messages.every(entry => entry && typeof entry.text === 'string' && entry.text.length > 0 &&
      entry.text.length <= 3500 && typeof entry.id === 'string' && messageId.test(entry.id));
}

async function portalCover(value) {
  if (typeof value !== 'string' || value.length > 800) return null;
  try {
    const url = new URL(value);
    if (url.origin !== 'https://paranaatual.com.br' || url.username || url.password ||
        !url.pathname.startsWith('/media/') || url.search || url.hash) return null;
    let download = url;
    const headers = {};
    if (/^\/media\/radar\/[a-f0-9]{64}\.(?:jpg|png|webp)$/.test(url.pathname)) {
      const webhook = process.env.RADAR_WHATSAPP_WEBHOOK;
      const token = process.env.RADAR_WHATSAPP_TOKEN;
      if (!webhook || !token) return null;
      download = new URL(url.pathname, webhook);
      headers.Authorization = 'Bearer ' + token;
    }
    const response = await fetch(download, { headers, redirect: 'error', signal: AbortSignal.timeout(2500) });
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
  app.post('/radar/group/reply', authorize, action(async (req, res) => {
    const { group_id, revision, messages } = req.body || {};
    const isCurrent = () => store.handles(group_id) && store.state().revision === revision;
    if (!isCurrent()) return res.status(409).json({ message: 'O grupo do Radar mudou.' });
    if (!validDelivery(messages)) return res.sendStatus(400);
    for (const { text, id, cover } of messages) {
      const image = await portalCover(cover);
      if (!isCurrent() || !await sendGroup(group_id, text, id, image)) return res.json({ accepted: false });
    }
    return res.json({ accepted: true });
  }));
  app.post('/radar/reply', authorize, action(async (req, res) => {
    const { phone, messages } = req.body || {};
    if (!phonePattern.test(phone || '') || !validDelivery(messages)) return res.sendStatus(400);
    for (const { text, id, cover } of messages) {
      const image = await portalCover(cover);
      if (!await sendPrivate(phone, image ? { image, caption: text } : { text }, id)) return res.json({ accepted: false });
    }
    return res.json({ accepted: true });
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
  const root = /^\s*\/?menu\s*$/i.test(text || '');
  const radar = /^\s*RADAR(?:\s|$)/i.test(text || '');
  const consoleCommand = /^\s*(?:CMS|SEO|PUBLICIDADE|USU[ÁA]RIOS|INSIGHTS)(?:\s|$)/i.test(text || '');
  const directAction = /^\s*(?:POSTAR|PUBLICAR|CONFIRMAR|VER|FONTES|FOTO|GERAR|REFAZER|APROVAR|DESCARTAR|RESTAURAR|APURAR|APURADO|T[ÍI]TULO|TEXTO|RESUMO)(?:\s|$)/i.test(text || '');
  const extra = /^\s*(?:[0-9]{1,4}|MAIS)\s*$/i.test(text || '');
  const stanza = message?.message?.extendedTextMessage?.contextInfo?.stanzaId;
  const quote = typeof stanza === 'string' && /^[A-Za-z0-9_-]{8,90}$/.test(stanza) ? stanza : undefined;
  if (group && !text && message?.messageStubType === 2 && groupStore?.handles(remote)) {
    console.info('Radar recebeu mensagem de grupo sem conteúdo descriptografado.');
  }
  if (!url || !token ||
      (group ? !(root || radar || consoleCommand || directAction || extra) || !groupStore?.handles(remote)
        : !(radar || consoleCommand || (directAction && quote)) || !/@(?:lid|s\.whatsapp\.net)$/.test(remote))) return false;

  if (messageKey.fromMe) {
    if (group && root) console.info('Radar menu recebido da conta vinculada.');
    return false;
  }
  const id = messageKey.id || '';
  if (!/^[A-Za-z0-9_-]{8,90}$/.test(id) || text.length > 11500) {
    if (group && root) console.warn('Radar menu descartado por formato inválido.');
    return true;
  }

  const candidates = senderIds(messageKey, group);

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
        body: JSON.stringify({ phone, id, text, ...(group ? { group_id: remote } : {}),
        ...(quote ? { quote_id: quote } : {}) }),
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
      if (group && root) console.info('Radar menu sem resposta.', status ?? 'remetente não identificado');
      else if (status >= 500) console.warn('Webhook do Radar indisponível.', status);
      return true;
    }
    const outgoing = [];
    if (typeof data.reply === 'string' && data.reply.length > 0 && data.reply.length <= 6000) {
      outgoing.push({ text: data.reply });
    }
    if (Array.isArray(data.messages)) {
      for (const entry of data.messages.slice(0, 12)) {
        if (entry && typeof entry.text === 'string' && entry.text.length > 0 &&
            entry.text.length <= 3500 && (entry.id === undefined || messageId.test(entry.id))) outgoing.push(entry);
      }
    }
    for (const { text: responseText, id: responseId, cover } of outgoing) {
      const image = await portalCover(cover);
      if (group && !groupStore.handles(remote)) break;
      await reply(responseText, responseId, image);
    }
    if (group && root && outgoing.length) console.info('Radar menu respondido.');
  } catch (error) {
    console.warn('Falha ao processar comando do Radar.', error?.name || 'Error');
  }
  return true;
}
