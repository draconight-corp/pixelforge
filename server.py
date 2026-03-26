#!/usr/bin/env python3
"""Local proxy server - serves static files + proxies /API/* & /View/* to SwarmUI.
   Auto-launches SwarmUI if not already running, then opens the browser."""

from http.server import HTTPServer, SimpleHTTPRequestHandler
import urllib.request
import urllib.error
import json
import os
import subprocess
import time
import webbrowser
import threading
import base64
import secrets
import hashlib

SWARM_URL = "http://localhost:7801"
SWARM_LAUNCH = os.path.expanduser("~/Downloads/SwarmUI/launch-windows.bat")
OLLAMA_URL = "http://localhost:11434"
PORT = 7880
DIRECTORY = os.path.dirname(os.path.abspath(__file__))
AUTH_FILE = os.path.join(DIRECTORY, ".auth")
STORAGE_DIR = os.path.join(DIRECTORY, "data")
ALLOWED_STORAGE_KEYS = {"project", "presets", "ai_settings"}
os.makedirs(STORAGE_DIR, exist_ok=True)


def load_or_create_auth():
    """Load credentials from .auth file, or generate them on first run."""
    if os.path.exists(AUTH_FILE):
        with open(AUTH_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line and ":" in line and not line.startswith("#"):
                    user, pwd_hash = line.split(":", 1)
                    return user, pwd_hash
    # Generate new credentials
    password = secrets.token_urlsafe(12)
    pwd_hash = hashlib.sha256(password.encode()).hexdigest()
    with open(AUTH_FILE, "w") as f:
        f.write("# PixelForge Auth - username:sha256(password)\n")
        f.write(f"# Your password is: {password}\n")
        f.write(f"# Share this password with people you trust.\n")
        f.write(f"admin:{pwd_hash}\n")
    print(f"[AUTH] Created .auth file with credentials:")
    print(f"[AUTH]   Username: admin")
    print(f"[AUTH]   Password: {password}")
    print(f"[AUTH]   (saved in {AUTH_FILE})")
    return "admin", pwd_hash


AUTH_USER, AUTH_HASH = load_or_create_auth()


def is_swarm_running():
    try:
        req = urllib.request.Request(f"{SWARM_URL}/API/GetNewSession", data=b"{}", method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=5):
            return True
    except Exception:
        return False


def launch_swarm():
    """Launch SwarmUI if not running."""
    if is_swarm_running():
        print("[SWARM] Already running.")
        return
    if not os.path.exists(SWARM_LAUNCH):
        print(f"[SWARM] launch script not found: {SWARM_LAUNCH}")
        return
    print(f"[SWARM] Starting SwarmUI...")
    subprocess.Popen(
        ["cmd", "/c", SWARM_LAUNCH],
        cwd=os.path.dirname(SWARM_LAUNCH),
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    # Wait up to 60s for SwarmUI to be ready
    for i in range(60):
        time.sleep(1)
        if is_swarm_running():
            print(f"[SWARM] Ready after {i+1}s")
            return
    print("[SWARM] Timeout waiting for SwarmUI to start.")


def is_ollama_running():
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=5):
            return True
    except Exception:
        return False


def launch_ollama():
    """Launch Ollama if not running."""
    if is_ollama_running():
        print("[OLLAMA] Already running.")
        return
    # Try to find ollama in PATH
    ollama_cmd = "ollama"
    try:
        subprocess.run([ollama_cmd, "--version"], capture_output=True, timeout=5)
    except FileNotFoundError:
        print("[OLLAMA] 'ollama' not found in PATH, skipping.")
        return
    except Exception:
        pass

    print("[OLLAMA] Starting Ollama serve...")
    subprocess.Popen(
        [ollama_cmd, "serve"],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    # Wait up to 30s for Ollama to be ready
    for i in range(30):
        time.sleep(1)
        if is_ollama_running():
            print(f"[OLLAMA] Ready after {i+1}s")
            return
    print("[OLLAMA] Timeout waiting for Ollama to start.")


class ProxyHandler(SimpleHTTPRequestHandler):

    def _is_local(self):
        """Check if request comes from localhost."""
        client = self.client_address[0]
        return client in ("127.0.0.1", "::1", "localhost")

    def _check_auth(self):
        """Verify HTTP Basic Auth. Returns True if authorized."""
        if self._is_local():
            return True
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Basic "):
            self._send_auth_required()
            return False
        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            user, password = decoded.split(":", 1)
            pwd_hash = hashlib.sha256(password.encode()).hexdigest()
            if user == AUTH_USER and pwd_hash == AUTH_HASH:
                return True
        except Exception:
            pass
        self._send_auth_required()
        return False

    def _send_auth_required(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="PixelForge"')
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<h1>401 - Login required</h1>")

    def translate_path(self, path):
        from urllib.parse import unquote
        path = unquote(path.split("?", 1)[0].split("#", 1)[0])
        parts = path.strip("/").split("/")
        result = DIRECTORY
        for part in parts:
            if part and part != "..":
                result = os.path.join(result, part)
        return result

    def _is_swarm_path(self):
        return self.path.startswith("/API/") or self.path.startswith("/View/")

    def do_GET(self):
        if not self._check_auth():
            return
        if self.path.startswith("/storage/"):
            self._storage_load()
        elif self.path == "/ai/ollama/models":
            self._proxy_ollama_models()
        elif self._is_swarm_path():
            self._proxy_to_swarm()
        else:
            super().do_GET()

    def do_POST(self):
        if not self._check_auth():
            return
        if self.path.startswith("/storage/"):
            self._storage_save()
        elif self.path == "/ai/command":
            self._proxy_to_anthropic()
        elif self.path == "/ai/ollama":
            self._proxy_to_ollama()
        elif self._is_swarm_path():
            self._proxy_to_swarm()
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _get_storage_key(self):
        """Extract and validate storage key from path."""
        key = self.path.split("/storage/", 1)[1].split("?")[0]
        if key not in ALLOWED_STORAGE_KEYS:
            return None
        return key

    def _storage_load(self):
        """GET /storage/<key> - load shared data."""
        key = self._get_storage_key()
        if not key:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Invalid key"}).encode())
            return
        filepath = os.path.join(STORAGE_DIR, f"{key}.json")
        if not os.path.exists(filepath):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b"null")
            return
        with open(filepath, "r", encoding="utf-8") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data.encode("utf-8"))

    def _storage_save(self):
        """POST /storage/<key> - save shared data."""
        key = self._get_storage_key()
        if not key:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Invalid key"}).encode())
            return
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b"{}"
        # Validate JSON
        try:
            json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Invalid JSON"}).encode())
            return
        filepath = os.path.join(STORAGE_DIR, f"{key}.json")
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(body.decode("utf-8"))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True}).encode())

    def _proxy_to_swarm(self):
        target = SWARM_URL + self.path
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""

        req = urllib.request.Request(target, data=body if body else None, method=self.command)
        # Forward content type, default to json for API calls
        ct = self.headers.get("Content-Type", "application/json")
        req.add_header("Content-Type", ct)

        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                resp_body = resp.read()
                self.send_response(resp.status)
                self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(resp_body)
        except urllib.error.HTTPError as e:
            resp_body = e.read()
            self.send_response(e.code)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(resp_body)
        except Exception as e:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def _proxy_to_anthropic(self):
        """Proxy AI commands to the Anthropic Claude API."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Invalid JSON"}).encode())
            return

        # API key: from request body, or env var fallback
        api_key = data.pop("apiKey", None) or os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "No API key provided"}).encode())
            return

        target = "https://api.anthropic.com/v1/messages"
        payload = json.dumps(data).encode()
        req = urllib.request.Request(target, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("x-api-key", api_key)
        req.add_header("anthropic-version", "2023-06-01")

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                resp_body = resp.read()
                self.send_response(resp.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(resp_body)
        except urllib.error.HTTPError as e:
            resp_body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(resp_body)
        except Exception as e:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def _proxy_to_ollama(self):
        """Proxy AI commands to the local Ollama API."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b"{}"
        target = "http://localhost:11434/api/chat"
        req = urllib.request.Request(target, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                resp_body = resp.read()
                self.send_response(resp.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(resp_body)
        except urllib.error.HTTPError as e:
            resp_body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(resp_body)
        except Exception as e:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"error": f"Ollama not running? {e}"}).encode())

    def _proxy_ollama_models(self):
        """Get list of locally available Ollama models."""
        target = "http://localhost:11434/api/tags"
        req = urllib.request.Request(target, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp_body = resp.read()
                self.send_response(resp.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(resp_body)
        except Exception as e:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"error": f"Ollama not running? {e}"}).encode())

    def end_headers(self):
        super().end_headers()

    def log_message(self, format, *args):
        print(f"[PROXY] {args[0]}" if args else "")


if __name__ == "__main__":
    # Launch SwarmUI and Ollama in background threads
    swarm_thread = threading.Thread(target=launch_swarm, daemon=True)
    swarm_thread.start()
    ollama_thread = threading.Thread(target=launch_ollama, daemon=True)
    ollama_thread.start()

    print(f"[SERVER] http://localhost:{PORT}")
    print(f"[SERVER] Proxying /API/* & /View/* -> {SWARM_URL}")
    print(f"[AUTH] Remote access requires login (see .auth file for credentials)")
    print(f"[AUTH] Local access (localhost) bypasses authentication")

    server = HTTPServer(("", PORT), ProxyHandler)

    # Open browser after a short delay
    def open_browser():
        time.sleep(1)
        webbrowser.open(f"http://localhost:{PORT}/sprite_generator.html")
    threading.Thread(target=open_browser, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
