from flask import Flask, request, jsonify
from flask_cors import CORS
import uuid, os
from datetime import datetime, timezone
from collections import defaultdict

app = Flask(__name__)
CORS(app)

agents = {}
commands = defaultdict(list)
screenshots = {}
SECRET_KEY = os.environ.get("SECRET_KEY", "rdp-manager-secret-2024")

def now():
    return datetime.now(timezone.utc).isoformat()

PANEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel.html")

@app.route("/")
def admin_panel():
    with open(PANEL_PATH, "r", encoding="utf-8") as f:
        return f.read()

@app.route("/agent/register", methods=["POST"])
def agent_register():
    data = request.json or {}
    if data.get("secret") != SECRET_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    agent_id = data.get("agent_id") or str(uuid.uuid4())[:8]
    agents[agent_id] = {
        "id": agent_id, "hostname": data.get("hostname", "Unknown"),
        "ip": data.get("ip", request.remote_addr), "os": data.get("os", "Unknown"),
        "username": data.get("username", "Unknown"), "status": "online",
        "last_seen": now(), "cpu": 0, "ram": 0, "disk": 0,
        "registered_at": now(), "last_result": None,
    }
    return jsonify({"agent_id": agent_id, "message": "Registered"})

@app.route("/agent/heartbeat", methods=["POST"])
def agent_heartbeat():
    data = request.json or {}
    if data.get("secret") != SECRET_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    agent_id = data.get("agent_id")
    if agent_id not in agents:
        return jsonify({"error": "not_found"}), 404
    agents[agent_id].update({
        "status": "online", "last_seen": now(),
        "cpu": data.get("cpu", 0), "ram": data.get("ram", 0), "disk": data.get("disk", 0),
    })
    pending = commands[agent_id].copy()
    commands[agent_id].clear()
    return jsonify({"commands": pending})

@app.route("/agent/screenshot", methods=["POST"])
def agent_screenshot():
    data = request.json or {}
    if data.get("secret") != SECRET_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    agent_id = data.get("agent_id")
    screenshots[agent_id] = {"data": data.get("image"), "timestamp": now()}
    return jsonify({"message": "ok"})

@app.route("/agent/result", methods=["POST"])
def agent_result():
    data = request.json or {}
    agent_id = data.get("agent_id")
    if agent_id in agents:
        agents[agent_id]["last_result"] = {
            "cmd": data.get("cmd"), "output": data.get("output"),
            "success": data.get("success"), "time": now()
        }
    return jsonify({"message": "ok"})

@app.route("/api/agents", methods=["GET"])
def get_agents():
    for a in agents.values():
        try:
            diff = (datetime.now(timezone.utc) - datetime.fromisoformat(a["last_seen"])).total_seconds()
            a["status"] = "online" if diff < 30 else "offline"
        except:
            a["status"] = "offline"
    return jsonify(list(agents.values()))

@app.route("/api/command", methods=["POST"])
def send_command():
    data = request.json or {}
    target = data.get("target", "all")
    cmd = {"id": str(uuid.uuid4())[:8], "type": data.get("type"),
           "payload": data.get("payload", {}), "issued_at": now()}
    if target == "all":
        for aid in agents: commands[aid].append(cmd)
        count = len(agents)
    else:
        commands[target].append(cmd)
        count = 1
    return jsonify({"message": f"Sent to {count} agent(s)", "cmd": cmd})

@app.route("/api/screenshot/<agent_id>", methods=["GET"])
def get_screenshot(agent_id):
    ss = screenshots.get(agent_id)
    if not ss: return jsonify({"error": "No screenshot"}), 404
    return jsonify(ss)

@app.route("/api/request_screenshot", methods=["POST"])
def request_screenshot():
    data = request.json or {}
    target = data.get("target", "all")
    cmd = {"id": str(uuid.uuid4())[:8], "type": "screenshot", "payload": {}, "issued_at": now()}
    if target == "all":
        for aid in agents: commands[aid].append(cmd)
    else:
        commands[target].append(cmd)
    return jsonify({"message": "Screenshot requested"})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "agents": len(agents)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
