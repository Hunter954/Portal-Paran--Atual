import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { createRadarGroups } from './radarGroups.js';

const available = '12345678@g.us';
const reserved = '87654321@g.us';
const missing = '88888888@g.us';

test('selection persists, keeps portal reservations and limits dispatch to current settings', async () => {
  const folder = await mkdtemp(path.join(os.tmpdir(), 'pa-group-'));
  try {
    let connected = true;
    let current = new Date('2026-11-24T15:00:00Z');
    const config = { authDir: folder, adminMenuFozTotalGroupId: reserved };
    const deps = { getStatus: () => ({connected}), clock: () => current,
      listGroups: async () => ({ groups: [{id: available, name: 'Redação'},
                                       {id: reserved, name: 'Outro portal'}] }) };
    const first = createRadarGroups(config, deps);
    await first.load();
    const options = await first.groups();
    assert.deepEqual(options.map(g => g.available), [true, false]);
    const values = { group_id: reserved, enabled: true, news: true, alerts: false, minimum: 80, revision: '' };
    await assert.rejects(first.save(values), { status: 409 });
    await assert.rejects(first.save({ ...values, group_id: missing }), { status: 409 });
    const saved = await first.save({ ...values, group_id: available });
    assert.equal(saved.group_name, 'Redação');
    assert.equal(saved.news_since, current.toISOString());
    assert.equal(first.allows({ group_id: available, revision: saved.revision, kind: 'article' }), true);
    assert.equal(first.allows({ group_id: reserved, revision: saved.revision, kind: 'article' }), false);
    assert.equal(first.allows({ group_id: available, revision: saved.revision, kind: 'alert' }), false);
    const restored = createRadarGroups({ authDir: folder, adminMenuFozTotalGroupId: reserved }, deps);
    await restored.load();
    assert.equal(restored.state().group_id, available);
    await assert.rejects(restored.save({ ...values, group_id: available }), { status: 409 });
    current = new Date('2026-11-24T15:30:00Z');
    const updated = await restored.save({ ...values, group_id: available, alerts: true, revision: saved.revision });
    assert.equal(updated.news_since, saved.news_since);
    assert.equal(updated.alerts_since, current.toISOString());
    assert.equal(restored.allows({ group_id: available, revision: saved.revision, kind: 'article' }), false);
    connected = false;
    const paused = await restored.save({ ...values, enabled: false, group_id: available, revision: updated.revision });
    assert.equal(paused.news, false);
    assert.equal(paused.group_id, available);
    assert.equal(restored.handles(available), false);
  } finally {
    await rm(folder, { recursive: true, force: true });
  }
});

