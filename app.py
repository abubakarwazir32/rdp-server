from flask import Flask, request, jsonify
from flask_cors import CORS
import uuid, os, threading, time
from datetime import datetime, timezone
from collections import defaultdict

app = Flask(__name__)
CORS(app)

agents = {}
commands = defaultdict(list)
screenshots = {}
SECRET_KEY = os.environ.get("SECRET_KEY", "rdp-manager-secret-2024")
system_state = {"mode": "sleep"}

# Schedule storage
schedule_config = {}  # target -> config
schedule_timers = {}  # target -> timer thread

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
        "username": data.get("username", "Unknown"), "status": "sleep",
        "last_seen": now(), "cpu": 0, "ram": 0, "disk": 0,
        "registered_at": now(), "last_result": None,
    }
    return jsonify({"agent_id": agent_id, "message": "Registered", "mode": system_state["mode"]})

@app.route("/agent/poll", methods=["POST"])
def agent_poll():
    data = request.json or {}
    if data.get("secret") != SECRET_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    agent_id = data.get("agent_id")
    if agent_id not in agents:
        return jsonify({"error": "not_found"}), 404
    agents[agent_id].update({
        "last_seen": now(), "cpu": data.get("cpu", 0),
        "ram": data.get("ram", 0), "disk": data.get("disk", 0),
        "status": "online" if system_state["mode"] == "wake" else "sleep",
    })
    if system_state["mode"] == "wake":
        pending = commands[agent_id].copy()
        commands[agent_id].clear()
        return jsonify({"mode": "wake", "commands": pending})
    else:
        return jsonify({"mode": "sleep", "commands": []})

@app.route("/agent/heartbeat", methods=["POST"])
def agent_heartbeat():
    return agent_poll()

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
            if system_state["mode"] == "sleep":
                a["status"] = "sleep"
            else:
                a["status"] = "online" if diff < 400 else "offline"
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

@app.route("/api/wake", methods=["POST"])
def wake_all():
    system_state["mode"] = "wake"
    for a in agents.values(): a["status"] = "online"
    return jsonify({"message": "All agents WAKING UP", "mode": "wake"})

@app.route("/api/sleep", methods=["POST"])
def sleep_all():
    system_state["mode"] = "sleep"
    for a in agents.values(): a["status"] = "sleep"
    return jsonify({"message": "All agents SLEEPING", "mode": "sleep"})

@app.route("/api/mode", methods=["GET"])
def get_mode():
    return jsonify({"mode": system_state["mode"]})

@app.route("/api/remove_agent", methods=["POST"])
def remove_agent():
    data = request.json or {}
    agent_id = data.get("agent_id")
    if agent_id in agents:
        del agents[agent_id]
        if agent_id in commands: del commands[agent_id]
        if agent_id in screenshots: del screenshots[agent_id]
        return jsonify({"message": f"Agent {agent_id} removed"})
    return jsonify({"error": "Agent not found"}), 404

@app.route("/api/remove_offline", methods=["POST"])
def remove_offline():
    offline = [aid for aid, a in agents.items() if a.get("status") in ["offline", "sleep"]]
    for aid in offline:
        del agents[aid]
        if aid in commands: del commands[aid]
        if aid in screenshots: del screenshots[aid]
    return jsonify({"message": f"Removed {len(offline)} offline agents", "count": len(offline)})

# ── SCHEDULE API ──────────────────────────────────────────────

def run_schedule_job(key):
    """Background thread - schedule execute karta hai"""
    cfg = schedule_config.get(key)
    if not cfg: return

    target = cfg["target"]
    bot = cfg["bot"]
    delay_min = cfg["delayMin"]
    delay_max = cfg["delayMax"]

    # Restart command bhejo
    restart_cmd = {"id": str(uuid.uuid4())[:8], "type": "restart", "payload": {}, "issued_at": now()}
    if target == "all":
        for aid in agents: commands[aid].append(restart_cmd)
    else:
        commands[target].append(restart_cmd)

    if bot == "none":
        return

    # Random delay calculate karo
    import random
    random_sec = random.randint(delay_min, delay_max)
    total_wait = 120 + random_sec  # 2 min boot + random seconds

    time.sleep(total_wait)

    # Bot launch command bhejo
    if bot == "1.5":
        path = "Smartbot15\\Smartbot15\\Smart bot 1.5.exe"
    else:
        path = "Smartbot16\\Smartbot16\\Smart bot 1.6.exe"

    bot_cmd = {
        "id": str(uuid.uuid4())[:8],
        "type": "launch_and_enter",
        "payload": {"path": path, "wait1": 7, "wait2": 4},
        "issued_at": now()
    }
    if target == "all":
        for aid in agents: commands[aid].append(bot_cmd)
    else:
        commands[target].append(bot_cmd)

def schedule_loop(key):
    """Repeating schedule loop"""
    while key in schedule_config:
        cfg = schedule_config.get(key)
        if not cfg: break
        interval_sec = cfg["intervalMs"] / 1000
        time.sleep(interval_sec)
        if key in schedule_config:  # still active?
            t = threading.Thread(target=run_schedule_job, args=(key,))
            t.daemon = True
            t.start()

@app.route("/api/schedule/save", methods=["POST"])
def save_schedule():
    data = request.json or {}
    key = data.get("target", "all")
    schedule_config[key] = {
        "value": data.get("value", 2),
        "unit": data.get("unit", "hours"),
        "bot": data.get("bot", "1.5"),
        "delayMin": data.get("delayMin", 2),
        "delayMax": data.get("delayMax", 10),
        "target": data.get("target", "all"),
        "intervalMs": data.get("intervalMs", 7200000),
        "created_at": now(),
    }
    # Start loop thread
    if key in schedule_timers:
        del schedule_timers[key]  # old one will exit
    t = threading.Thread(target=schedule_loop, args=(key,))
    t.daemon = True
    t.start()
    schedule_timers[key] = t
    return jsonify({"message": "Schedule saved", "config": schedule_config[key]})

@app.route("/api/schedule/stop", methods=["POST"])
def stop_schedule():
    data = request.json or {}
    key = data.get("target", "all")
    if key in schedule_config:
        del schedule_config[key]
    return jsonify({"message": "Schedule stopped"})

@app.route("/api/schedule/get", methods=["GET"])
def get_schedules():
    return jsonify(schedule_config)

@app.route("/health")
def health():
    return jsonify({"status": "ok", "agents": len(agents), "mode": system_state["mode"]})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
