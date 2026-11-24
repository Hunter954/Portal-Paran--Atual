import { createHash, timingSafeEqual } from 'node:crypto';

const phonePattern = /^\+[1-9][0-9]{7,14}$/;
const hash = value => createHash('sha256').update(value).digest();

export function installRadarRoutes(app, { getStatus, sendPrivate }) {
  const authorize = (req, res, next) => {
    const token = process.env.RADAR_WHATSAPP_TOKEN;
    const authorization = req.get('Authorization') || '';
    if (!token || !timingSafeEqual(hash(authorization), hash('Bearer ' + token))) {
      return res.sendStatus(401);
    }
    return next();
  };
  app.get('/radar/status', authorize, (_req, res) => {
    res.json({ connected: getStatus().state === 'connected' });
  });
  app.post('/radar/send', authorize, async (req, res) => {
    const { phone, text, id } = req.body || {};
    if (typeof phone !== 'string' || !phonePattern.test(phone) ||
        typeof text !== 'string' || !text.length || text.length > 3500 ||
        typeof id !== 'string' || !/^3EB0[A-F0-9]{18}$/.test(id)) {
      return res.sendStatus(400);
    }
    try {
      return res.json({ accepted: await sendPrivate(phone, { text }, id) });
    } catch {
      return res.sendStatus(503);
    }
  });
}

export async function handleRadarMessage(message, text, reply) {
  const url = process.env.RADAR_WHATSAPP_WEBHOOK;
  const token = process.env.RADAR_WHATSAPP_TOKEN;
  const remote = message?.key?.remoteJid || '';
  if (!url || !token || message?.key?.fromMe || !/^\s*RADAR(?:\s|$)/i.test(text || '') ||
      !/@(?:lid|s\.whatsapp\.net)$/.test(remote)) return false;
  const jid = [remote, message.key.remoteJidAlt].find(value => /^\d+@s\.whatsapp\.net$/.test(value || ''));
  const id = message.key.id || '';
  if (!jid || !/^[A-Za-z0-9_-]{8,90}$/.test(id) || text.length > 160) return true;
  try {
    const response = await fetch(url, { method: 'POST', redirect: 'error', signal: AbortSignal.timeout(5000),
      headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone: '+' + jid.split('@')[0], id, text }),
    });
    if (!response.ok) return true;
    const data = await response.json();
    if (typeof data.reply === 'string' && data.reply.length <= 6000) await reply(data.reply);
  } catch {
    // O restante do gateway continua disponível durante falhas do Radar.
  }
  return true;
}

