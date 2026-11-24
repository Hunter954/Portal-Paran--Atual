import { randomUUID } from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';

export const validGroup = id => typeof id === 'string' && /^\d{5,30}@g\.us$/.test(id);
const defaultSettings = { group_id: '', group_name: '', enabled: false, news: false, alerts: false,
  minimum: 75, news_since: null, alerts_since: null, revision: '' };
const failure = (status, message) => Object.assign(new Error(message), { status });

function isReserved(config, id) {
  return Object.entries(config).some(([name, value]) =>
    name !== 'adminMenuParanaAtualGroupId' && /groupid$/i.test(name) && value === id);
}

export function createRadarGroups(config, { getStatus, listGroups, clock = () => new Date() }) {
  const file = path.join(config.authDir, 'parana-atual-group.json');
  let settings = { ...defaultSettings };
  let serial = Promise.resolve();

  function state() {
    return { ...settings, connected: Boolean(getStatus().connected),
      available: !settings.group_id || !isReserved(config, settings.group_id) };
  }

  async function groups() {
    if (!getStatus().connected) throw failure(409, 'Conecte o WhatsApp para carregar os grupos.');
    const result = await listGroups();
    return (result.groups || []).filter(group => validGroup(group.id)).map(group => ({
      id: group.id, name: String(group.name || group.id).slice(0, 160),
      participants_count: Number(group.participants_count || 0), available: !isReserved(config, group.id)
    }));
  }

  return {
    state, groups,
    async load() {
      try {
        const saved = JSON.parse(await fs.readFile(file, 'utf8'));
        if (!saved || typeof saved.revision !== 'string' || (saved.group_id && !validGroup(saved.group_id))) {
          throw new Error('Configuração de grupo do Paraná Atual inválida.');
        }
        settings = { ...defaultSettings, ...saved };
        config.adminMenuParanaAtualGroupId = settings.group_id;
      } catch (error) {
        if (error.code !== 'ENOENT') throw error;
      }
    },
    save(payload) {
      const operation = serial.then(async () => {
        const { group_id, enabled, news, alerts, minimum, revision } = payload || {};
        if (revision !== settings.revision) throw failure(409, 'A configuração mudou. Atualize a página.');
        if ((group_id !== '' && !validGroup(group_id)) || (enabled && !group_id) ||
            [enabled, news, alerts].some(value => typeof value !== 'boolean') ||
            !Number.isInteger(minimum) || minimum < 75 || minimum > 100) {
          throw failure(400, 'Selecione um grupo válido e uma pontuação de 75 a 100.');
        }
        const changedGroup = group_id !== settings.group_id;
        let groupName = changedGroup ? '' : settings.group_name;
        if (group_id && (changedGroup || enabled)) {
          const selected = (await groups()).find(group => group.id === group_id);
          if (!selected || !selected.available || isReserved(config, group_id)) {
            throw failure(409, 'Esse grupo está indisponível ou já é utilizado por outro portal.');
          }
          groupName = selected.name;
        }
        const newTime = clock().toISOString();
        const sendNews = enabled && news;
        const sendAlerts = enabled && alerts;
        const next = { group_id, group_name: groupName, enabled, news: sendNews, alerts: sendAlerts, minimum,
          news_since: sendNews ? (changedGroup || !settings.news ? newTime : settings.news_since) : null,
          alerts_since: sendAlerts ? (changedGroup || !settings.alerts || minimum !== settings.minimum ?
            newTime : settings.alerts_since) : null, revision: randomUUID() };
        await fs.mkdir(config.authDir, { recursive: true });
        await fs.writeFile(file + '.tmp', JSON.stringify(next), { mode: 0o600 });
        if (enabled && isReserved(config, group_id)) throw failure(409, 'Outro portal selecionou esse grupo.');
        await fs.rename(file + '.tmp', file);
        settings = next;
        config.adminMenuParanaAtualGroupId = next.group_id;
        return state();
      });
      serial = operation.catch(() => {});
      return operation;
    },
    handles(groupId) {
      return Boolean(settings.enabled && settings.group_id === groupId && !isReserved(config, groupId));
    },
    allows({ group_id, revision, kind }) {
      return this.handles(group_id) && settings.revision === revision &&
        ((kind === 'article' && settings.news) || (kind === 'alert' && settings.alerts));
    }
  };
}

