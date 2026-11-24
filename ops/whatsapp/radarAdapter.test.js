import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { once } from 'node:events';
import test from 'node:test';
import { handleRadarMessage, installRadarRoutes } from './radarAdapter.js';

const selectedGroup = '12345678@g.us';
const administrator = '5545990000011';

test('passes selected group commands through the authenticated webhook and replies to the group', async t => {
  const received = [];
  const replies = [];
  const routes = { get() {}, post() {} };
  installRadarRoutes(routes, { store: { handles: id => id === selectedGroup } });

  const server = createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const data = JSON.parse(Buffer.concat(chunks));
    received.push({ auth: request.headers.authorization, ...data });
    response.writeHead(data.phone === '+' + administrator ? 200 : 403, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ reply: 'Menu da redação' }));
  }).listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(() => {
    server.close();
    server.closeAllConnections();
  });
  const fields = { RADAR_WHATSAPP_WEBHOOK: 'http://127.0.0.1:' + server.address().port,
    RADAR_WHATSAPP_TOKEN: 'qa-adapter-token' };
  for (const [name, value] of Object.entries(fields)) {
    const previous = process.env[name];
    process.env[name] = value;
    t.after(() => { if (previous === undefined) delete process.env[name]; else process.env[name] = previous; });
  }

  const key = { remoteJid: selectedGroup, participant: '987654321@lid',
    participantAlt: administrator + ':2@s.whatsapp.net', fromMe: false, id: 'MENU_TEST_01' };
  const reply = text => { replies.push(text); };
  assert.equal(await handleRadarMessage({ key: { ...key, remoteJid: '87654321@g.us' } }, 'menu', reply), false);
  assert.equal(await handleRadarMessage({ key: { ...key, fromMe: true } }, 'menu', reply), false);
  assert.equal(await handleRadarMessage({ key }, '9', reply), true);
  assert.equal(received.length, 0);
  assert.equal(await handleRadarMessage({ key }, 'menu', reply), true);
  assert.deepEqual(received, [{ auth: 'Bearer qa-adapter-token', phone: '+' + administrator,
    id: 'MENU_TEST_01', text: 'menu', group_id: selectedGroup }]);
  assert.deepEqual(replies, ['Menu da redação']);

  await handleRadarMessage({ key: { ...key, id: 'MENU_TEST_02' } }, '9', reply);
  assert.equal(received[1].text, '9');
  assert.equal(replies.length, 2);

  const direct = { remoteJid: administrator + '@s.whatsapp.net', id: 'PRIVATE_TEST_01' };
  assert.equal(await handleRadarMessage({ key: direct }, 'RADAR AJUDA', reply), true);
  assert.equal(received[2].phone, '+' + administrator);
  assert.equal('group_id' in received[2], false);

  const unregistered = { ...key, participantAlt: '5545990000022@s.whatsapp.net', id: 'OTHER_TEST_01' };
  await handleRadarMessage({ key: unregistered }, 'menu', reply);
  assert.equal(received.length, 4);
  assert.equal(replies.length, 3);

  // The installed Baileys version puts the phone in participantPn.
  const legacy = { remoteJid: selectedGroup, participant: '987654321@lid',
    participantPn: administrator + '@s.whatsapp.net', id: 'LEGACY_TEST_01' };
  await handleRadarMessage({ key: legacy }, 'menu', reply);
  assert.equal(received[4].phone, '+' + administrator);
  assert.equal(replies.length, 4);

  const onlyLid = { ...key, participantAlt: null, id: 'LID_TEST_001' };
  await handleRadarMessage({ key: onlyLid }, 'RADAR AJUDA', reply, async lid => {
    assert.deepEqual(lid, [key.participant]);
    return [administrator + '@s.whatsapp.net'];
  });
  assert.equal(received[5].phone, '+' + administrator);
  assert.equal(replies.length, 5);

  const canonical = { ...legacy, participantPn: '554590000011@s.whatsapp.net', id: 'CANON_TEST_01' };
  await handleRadarMessage({ key: canonical }, 'menu', reply, async (aliases, room) => {
    assert.equal(room, selectedGroup);
    assert.ok(aliases.includes('987654321@lid'));
    return [...aliases, administrator + '@s.whatsapp.net'];
  });
  assert.deepEqual(received.slice(6).map(item => item.phone), ['+554590000011', '+' + administrator]);
  assert.equal(replies.length, 6);
});

