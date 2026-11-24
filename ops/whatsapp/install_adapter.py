"""Aplica o adaptador em uma cópia de build do gateway Baileys."""

import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).parent
SERVER_IMPORT = "import { installRadarRoutes } from './radarAdapter.js';"
STORE_IMPORT = "import { createRadarGroups } from './radarGroups.js';"
CLIENT_IMPORT = "import { handleRadarMessage } from './radarAdapter.js';"
ROUTES = """const radarStore = createRadarGroups(config, { getStatus, listGroups });
await radarStore.load();
installRadarRoutes(app, { getStatus, sendPrivate: sendLivoraPrivate, sendGroup: sendRadarGroup, store: radarStore });"""
CLIENT_SWEEP = ("        if (await handleRadarMessage(message, text, value => replyMessage(remoteJid, value),"
                " lid => socket.signalRepository?.lidMapping?.getPNForLID(lid))) continue;")
SEND_GROUP = """export async function sendRadarGroup(groupId, text, id, cover) {
  ensureConnected();
  const socket = sock;
  const content = cover ? { image: cover, caption: text } : { text };
  if (socket !== sock || !connected) return false;
  const response = await socket.sendMessage(groupId, content, { messageId: id });
  return Boolean(response?.key?.id);
}
"""


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise SystemExit("Gateway incompatível com o adaptador do Paraná Atual.")
    return text.replace(old, new, 1)


def main():
    source = Path(sys.argv[1]).resolve() / "src"
    server, client, foz = (source / name for name in ("server.js", "baileysClient.js", "fozTotalManagement.js"))
    server_text, client_text, foz_text = (path.read_text(encoding="utf-8") for path in (server, client, foz))
    if ROUTES not in server_text:
        old = "installRadarRoutes(app, { getStatus, sendPrivate: sendLivoraPrivate });"
        if SERVER_IMPORT not in server_text:
            server_text = SERVER_IMPORT + "\n" + server_text
            server_text = replace_once(server_text, "app.get('/health',", ROUTES + "\n\napp.get('/health',")
        else:
            server_text = replace_once(server_text, old, ROUTES)
        server_text = STORE_IMPORT + "\n" + server_text
        server_text = replace_once(server_text, "  sendLivoraPrivate,", "  sendLivoraPrivate,\n  sendRadarGroup,")
    if CLIENT_SWEEP not in client_text:
        old = "        if (await handleRadarMessage(message, text, value => replyMessage(remoteJid, value))) continue;"
        receive = "        const text = textFromMessage(message);"
        if CLIENT_IMPORT not in client_text:
            client_text = CLIENT_IMPORT + "\n" + client_text
            client_text = replace_once(client_text, receive, receive + "\n" + CLIENT_SWEEP)
        else:
            client_text = replace_once(client_text, old, CLIENT_SWEEP)
        client_text = replace_once(client_text, "export async function sendLivoraPrivate",
                                   SEND_GROUP + "\nexport async function sendLivoraPrivate")
    if "config.adminMenuParanaAtualGroupId" not in foz_text:
        foz_text = replace_once(foz_text, "return new Set([config.adminMenuGroupId,",
                                "return new Set([config.adminMenuParanaAtualGroupId, config.adminMenuGroupId,")
    for path, text in zip((server, client, foz), (server_text, client_text, foz_text)):
        path.write_text(text, encoding="utf-8")
    for name in ("radarAdapter.js", "radarGroups.js"):
        shutil.copyfile(ROOT / name, source / name)


if __name__ == "__main__":
    main()

