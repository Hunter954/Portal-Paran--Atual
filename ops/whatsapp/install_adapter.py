"""Aplica o adaptador ao código do gateway Baileys em uma cópia de build."""

import shutil
import sys
from pathlib import Path


def main():
    source = Path(sys.argv[1]).resolve() / "src"
    server, client = source / "server.js", source / "baileysClient.js"
    server_text, client_text = (path.read_text(encoding="utf-8") for path in (server, client))
    if "installRadarRoutes" in server_text and "handleRadarMessage" in client_text:
        return
    routes = "const app = express();"
    receive = "        const text = textFromMessage(message);"
    server_mount = "app.get('/health',"
    if server_text.count(routes) != 1 or server_text.count(server_mount) != 1 or client_text.count(receive) != 1:
        raise SystemExit("Gateway incompatível. Verifique os pontos de entrada.")
    adapter = Path(__file__).with_name("radarAdapter.js")
    shutil.copyfile(adapter, source / adapter.name)
    server.write_text("import { installRadarRoutes } from './radarAdapter.js';\n" +
                      server_text.replace(server_mount, "installRadarRoutes(app, { getStatus, sendPrivate: sendLivoraPrivate });\n\n" + server_mount, 1), encoding="utf-8")
    client.write_text("import { handleRadarMessage } from './radarAdapter.js';\n" +
                      client_text.replace(receive, receive + "\n        if (await handleRadarMessage(message, text, value => replyMessage(remoteJid, value))) continue;", 1), encoding="utf-8")


if __name__ == "__main__":
    main()

