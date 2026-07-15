from flask import Flask, request, jsonify
from flask_cors import CORS
import uuid, os, threading, time, random
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
schedule_config = {}
schedule_timers = {}

# Auto Pilot storage
autopilot_config = {}
autopilot_running = {}

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
    cfg = schedule_config.get(key)
    if not cfg: return
    target = cfg["target"]
    bot = cfg["bot"]
    delay_min = cfg["delayMin"]
    delay_max = cfg["delayMax"]
    restart_cmd = {"id": str(uuid.uuid4())[:8], "type": "restart", "payload": {}, "issued_at": now()}
    if target == "all":
        for aid in agents: commands[aid].append(restart_cmd)
    else:
        commands[target].append(restart_cmd)
    if bot == "none": return
    random_sec = random.randint(delay_min, delay_max)
    total_wait = 120 + random_sec
    time.sleep(total_wait)
    path = "Smartbot15\\Smartbot15\\Smart bot 1.5.exe" if bot == "1.5" else "Smartbot16\\Smartbot16\\Smart bot 1.6.exe"
    bot_cmd = {"id": str(uuid.uuid4())[:8], "type": "launch_and_enter",
               "payload": {"path": path, "wait1": 7, "wait2": 4}, "issued_at": now()}
    if target == "all":
        for aid in agents: commands[aid].append(bot_cmd)
    else:
        commands[target].append(bot_cmd)

def schedule_loop(key):
    while key in schedule_config:
        cfg = schedule_config.get(key)
        if not cfg: break
        interval_sec = cfg["intervalMs"] / 1000
        time.sleep(interval_sec)
        if key in schedule_config:
            t = threading.Thread(target=run_schedule_job, args=(key,))
            t.daemon = True
            t.start()

@app.route("/api/schedule/save", methods=["POST"])
def save_schedule():
    data = request.json or {}
    key = data.get("target", "all")
    schedule_config[key] = {
        "value": data.get("value", 2), "unit": data.get("unit", "hours"),
        "bot": data.get("bot", "1.5"), "delayMin": data.get("delayMin", 2),
        "delayMax": data.get("delayMax", 10), "target": data.get("target", "all"),
        "intervalMs": data.get("intervalMs", 7200000), "created_at": now(),
    }
    if key in schedule_timers: del schedule_timers[key]
    t = threading.Thread(target=schedule_loop, args=(key,))
    t.daemon = True
    t.start()
    schedule_timers[key] = t
    return jsonify({"message": "Schedule saved", "config": schedule_config[key]})

@app.route("/api/schedule/stop", methods=["POST"])
def stop_schedule():
    data = request.json or {}
    key = data.get("target", "all")
    if key in schedule_config: del schedule_config[key]
    return jsonify({"message": "Schedule stopped"})

@app.route("/api/schedule/get", methods=["GET"])
def get_schedules():
    return jsonify(schedule_config)

# ── AUTO PILOT API ────────────────────────────────────────────

def send_cmd_to_all(cmd_type, payload={}):
    """Server side command send karo sab agents ko"""
    cmd = {"id": str(uuid.uuid4())[:8], "type": cmd_type,
           "payload": payload, "issued_at": now()}
    for aid in agents:
        commands[aid].append(cmd)

def send_close_bot():
    """Close bot 1.5 - same command as Close Bot button"""
    close_cmd = (
        'powershell -command "'
        'Add-Type -AssemblyName Microsoft.VisualBasic; '
        '[Microsoft.VisualBasic.Interaction]::AppActivate(\'Smart bot 1.5\'); '
        'Start-Sleep -m 500; '
        'Add-Type -AssemblyName System.Windows.Forms; '
        '[System.Windows.Forms.SendKeys]::SendWait(\'{ENTER}\'); '
        'Start-Sleep -Seconds 3; '
        "taskkill /f /fi 'WINDOWTITLE eq Smart bot 1.5*'\""
    )
    cmd = {"id": str(uuid.uuid4())[:8], "type": "shell",
           "payload": {"cmd": close_cmd}, "issued_at": now()}
    for aid in list(agents.keys()):
        commands[aid].append(cmd)
def run_bot_batches():
    """Bot 1.5 batches mein launch - same as RUN BOT button"""
    agent_ids = list(agents.keys())
    i = 0
    while i < len(agent_ids):
        batch_size = random.randint(3, 4)
        batch = agent_ids[i:i+batch_size]
        bot_cmd = {
            "id": str(uuid.uuid4())[:8],
            "type": "launch_and_enter",
            "payload": {
                "path": "Smartbot15\\Smartbot15\\Smart bot 1.5.exe",
                "wait1": 7,
                "wait2": 4
            },
            "issued_at": now()
        }
        for aid in batch:
            commands[aid].append(bot_cmd)
        i += batch_size
        wait_sec = random.randint(6, 9)
        time.sleep(wait_sec)
def autopilot_cycle(key):
    """Auto Pilot cycle - pehle interval wait, phir kaam"""
    cfg = autopilot_config.get(key)
    if not cfg: return

    close_wait = cfg["closeWait"] * 60   # minutes to seconds
    run_wait = cfg["runWait"] * 60
    interval_sec = cfg["intervalMs"] / 1000

    while autopilot_running.get(key):
        # Step 1: Pehle interval wait karo
        elapsed = 0
        while elapsed < interval_sec:
            if not autopilot_running.get(key): return
            time.sleep(1)
            elapsed += 1

        if not autopilot_running.get(key): break

        # Step 2: Wake All
        system_state["mode"] = "wake"
        for a in agents.values(): a["status"] = "online"

        # Step 2.5: 5 second wait - agents online hon phir close bhejo
        elapsed = 0
        while elapsed < 5:
            if not autopilot_running.get(key): return
            time.sleep(1)
            elapsed += 1

        # Step 3: Close Bot
        send_close_bot()

        # Step 4: Wait close_wait (1 sec chunks for stop check)
        elapsed = 0
        while elapsed < close_wait:
            if not autopilot_running.get(key): return
            time.sleep(1)
            elapsed += 1

        if not autopilot_running.get(key): break

        # Step 5: Run Bot batches
        run_bot_batches()

        # Step 6: Wait run_wait (1 sec chunks for stop check)
        elapsed = 0
        while elapsed < run_wait:
            if not autopilot_running.get(key): return
            time.sleep(1)
            elapsed += 1

        if not autopilot_running.get(key): break

        # Step 7: Sleep All
        system_state["mode"] = "sleep"
        for a in agents.values(): a["status"] = "sleep"

@app.route("/api/autopilot/start", methods=["POST"])
def autopilot_start():
    data = request.json or {}
    key = "main"
    autopilot_config[key] = {
        "closeWait": data.get("closeWait", 4),
        "runWait": data.get("runWait", 5),
        "intervalMs": data.get("intervalMs", 3600000),
        "intervalVal": data.get("intervalVal", 1),
        "intervalUnit": data.get("intervalUnit", "hours"),
        "created_at": now(),
    }
    autopilot_running[key] = True
    t = threading.Thread(target=autopilot_cycle, args=(key,))
    t.daemon = True
    t.start()
    return jsonify({"message": "Auto Pilot started", "config": autopilot_config[key]})

@app.route("/api/autopilot/stop", methods=["POST"])
def autopilot_stop():
    autopilot_running["main"] = False
    if "main" in autopilot_config:
        del autopilot_config["main"]
    return jsonify({"message": "Auto Pilot stopped"})

@app.route("/api/autopilot/get", methods=["GET"])
def autopilot_get():
    cfg = autopilot_config.get("main", {})
    return jsonify({
        "running": autopilot_running.get("main", False),
        "config": cfg,
        "intervalVal": cfg.get("intervalVal", 1),
        "intervalUnit": cfg.get("intervalUnit", "hours"),
    })

@app.route("/health")
def health():
    return jsonify({"status": "ok", "agents": len(agents), "mode": system_state["mode"]})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
