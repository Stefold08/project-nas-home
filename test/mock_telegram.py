"""Finto server di Telegram per le prove: getMe, getUpdates, sendMessage, più due comandi di controllo."""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "123456789:AAH" + "x" * 32
stato = {"aggiornamenti": [], "messaggi": [], "id": 100}


class Gestore(BaseHTTPRequestHandler):
    def log_message(self, *argomenti):
        pass

    def rispondi(self, oggetto, codice=200):
        dati = json.dumps(oggetto).encode()
        self.send_response(codice)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(dati)))
        self.end_headers()
        self.wfile.write(dati)

    def do_GET(self):
        if self.path == "/_messaggi":
            return self.rispondi(stato["messaggi"])
        self.rispondi({"ok": False}, 404)

    def do_POST(self):
        lunghezza = int(self.headers.get("Content-Length") or 0)
        corpo = json.loads(self.rfile.read(lunghezza) or b"{}")
        if self.path == "/_start":
            stato["id"] += 1
            stato["aggiornamenti"].append({"update_id": stato["id"], "message": {
                "message_id": stato["id"], "date": int(time.time()), "text": "/start",
                "chat": {"id": 4242, "type": "private"}, "from": {"id": 4242, "first_name": "Daniele"}}})
            return self.rispondi({"ok": True})
        parti = self.path.split("/")
        if len(parti) < 3 or not parti[1].startswith("bot"):
            return self.rispondi({"ok": False}, 404)
        if parti[1][3:] != TOKEN:
            return self.rispondi({"ok": False, "error_code": 401, "description": "Unauthorized"}, 401)
        metodo = parti[2]
        if metodo == "getMe":
            return self.rispondi({"ok": True, "result": {"id": 1, "is_bot": True, "username": "rete_casa_bot"}})
        if metodo == "getUpdates":
            offset = corpo.get("offset")
            if offset:
                stato["aggiornamenti"] = [u for u in stato["aggiornamenti"] if u["update_id"] >= offset]
            return self.rispondi({"ok": True, "result": list(stato["aggiornamenti"])})
        if metodo == "sendMessage":
            stato["messaggi"].append({"chat_id": corpo.get("chat_id"), "text": corpo.get("text")})
            return self.rispondi({"ok": True, "result": {"message_id": len(stato["messaggi"])}})
        self.rispondi({"ok": False, "error_code": 404, "description": "Not Found"}, 404)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Gestore).serve_forever()
