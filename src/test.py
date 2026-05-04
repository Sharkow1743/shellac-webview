import os
import time
import threading
from shellac import Window, Browser

# ==========================================
# 1. SETUP: Create a dummy icon for testing
# ==========================================
ICON_PATH = "test_icon.ico"

def create_dummy_icon():
    """Creates a tiny 1x1 red ICO file for testing native Windows/Chromium icons."""
    if not os.path.exists(ICON_PATH):
        with open(ICON_PATH, "wb") as f:
            f.write(
                b'\x00\x00\x01\x00\x01\x00\x01\x01\x00\x00\x01\x00\x18\x000\x00\x00\x00\x16\x00\x00\x00(\x00\x00\x00'
                b'\x01\x00\x00\x00\x02\x00\x00\x00\x01\x00\x18\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
                b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\x00\x00\x00'
                b'\x00\x00\x00\x00'
            )

create_dummy_icon()

# ==========================================
# 2. HTML UI
# ==========================================
HTML_CONTENT = """
<!DOCTYPE html>
<html>
<head>
    <title>Initial Title</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; padding: 20px; background: #1e1e1e; color: #d4d4d4; }
        h1 { color: #569cd6; border-bottom: 1px solid #333; padding-bottom: 10px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-bottom: 20px; }
        button { padding: 10px; background: #0e639c; color: white; border: none; border-radius: 4px; cursor: pointer; transition: 0.2s; }
        button:hover { background: #1177bb; }
        input { padding: 9px; border-radius: 4px; border: 1px solid #3c3c3c; background: #3c3c3c; color: white; width: calc(100% - 20px); }
        .box { background: #252526; padding: 15px; border-radius: 6px; margin-bottom: 15px; border: 1px solid #333; }
        #log { background: #000; padding: 15px; border-radius: 4px; height: 150px; overflow-y: auto; font-family: monospace; border: 1px solid #333; }
        .log-time { color: #4ec9b0; }
        .log-msg { color: #ce9178; }
    </style>
</head>
<body>
    <h1>Shellac Feature Test Dashboard</h1>
    
    <div class="box">
        <h3>Window Management</h3>
        <div class="grid">
            <button id="btn-size">Toggle Size</button>
            <button id="btn-pos">Move Window</button>
            <button id="btn-title">Change Title</button>
            <button id="btn-max">Maximize</button>
            <button id="btn-min">Minimize (Restore in 3s)</button>
            <button id="btn-alert">Native Alert</button>
        </div>
    </div>

    <div class="box">
        <h3>Python <-> JS Communication</h3>
        <div class="grid" style="grid-template-columns: 2fr 1fr;">
            <input type="text" id="test-input" value="Hello from JavaScript!" />
            <button onclick="callPythonBind()">Process in Python</button>
        </div>
    </div>

    <div class="box">
        <h3>DOM Event Handlers (@win.on)</h3>
        <div class="grid">
            <button class="dom-btn" id="db-1" value="secret-1">Delegated Button 1</button>
            <button class="dom-btn" id="db-2" value="secret-2">Delegated Button 2</button>
        </div>
    </div>

    <div class="box">
        <h3>Console Log</h3>
        <div id="log"></div>
    </div>

    <script>
        // JS helper to add logs to the UI
        function uiLog(msg) {
            const logDiv = document.getElementById('log');
            const time = new Date().toLocaleTimeString();
            logDiv.innerHTML += `<span class="log-time">[${time}]</span> <span class="log-msg">${msg}</span><br>`;
            logDiv.scrollTop = logDiv.scrollHeight;
        }

        // JS calling Python using the bridge and waiting for a result
        async function callPythonBind() {
            const val = document.getElementById('test-input').value;
            uiLog(`Calling python with: "${val}"...`);
            
            // Call bound python function
            const result = await window.webui.call('reverse_text', val);
            
            uiLog(`Python replied: "${result}"`);
        }
    </script>
</body>
</html>
"""

# ==========================================
# 3. INITIALIZE WINDOW
# ==========================================
win = Window()

# Testing New Config Features
win.config.app_name = "ShellacTestApp"  # Should group separately on Linux/Windows taskbar
win.config.icon_path = ICON_PATH        # Should show the red icon in taskbar
win.config.width = 900
win.config.height = 750
win.config.hide_controls = True         # App Mode

# State variables for toggling
toggle_size = False
toggle_pos = False

# ==========================================
# 4. DEFINE BINDINGS (Python <-> JS)
# ==========================================

@win.bind("reverse_text")
def reverse_text(text: str):
    """Called explicitly by JS: window.webui.call('reverse_text', text)"""
    return text[::-1]

@win.bind("restore_win")
def restore_win():
    """Called by JS setTimeout to restore window after minimize"""
    win.run_js("uiLog('Window restored by Python!');")
    # Setting size restores from minimized state on most OS
    win.set_size(win.config.width, win.config.height) 

# ==========================================
# 5. DEFINE DOM EVENTS (@win.on)
# ==========================================

@win.on("click", "#btn-size")
def on_size(event, text):
    global toggle_size
    curr = win.get_size()
    win.run_js(f"uiLog('Python sees current size: {curr}');")
    
    if toggle_size:
        win.set_size(900, 750)
        win.run_js("uiLog('Python resized window to 900x750');")
    else:
        win.set_size(1050, 600)
        win.run_js("uiLog('Python resized window to 1050x600');")
    toggle_size = not toggle_size

@win.on("click", "#btn-pos")
def on_pos(event, text):
    global toggle_pos
    curr = win.get_position()
    win.run_js(f"uiLog('Python sees current pos: {curr}');")
    
    if toggle_pos:
        win.set_position(100, 100)
    else:
        win.set_position(300, 300)
    toggle_pos = not toggle_pos
    win.run_js("uiLog('Python moved window position');")

@win.on("click", "#btn-title")
def on_title(event, text):
    new_title = f"Shellac App - {int(time.time())}"
    win.set_title(new_title)
    win.run_js(f"uiLog('Title changed to: {new_title}');")

@win.on("click", "#btn-max")
def on_max(event, text):
    win.run_js("uiLog('Maximizing...');")
    win.maximize()

@win.on("click", "#btn-min")
def on_min(event, text):
    win.run_js("uiLog('Minimizing for 3 seconds...');")
    win.minimize()
    # Trigger an async js function to wake it back up
    win.run_js_async("setTimeout(() => window.webui.call('restore_win'), 3000);")

@win.on("click", "#btn-alert")
def on_alert(event, text):
    win.alert("This is a native browser alert triggered securely from Python!")
    win.run_js("uiLog('Alert dismissed.');")

@win.on("click", ".dom-btn")
def on_dom_btn(event, text):
    # event.get_dict(0) contains the dictionary of event properties mapped in javascript
    event_data = event.get_dict(0)
    btn_id = event_data.get('id')
    btn_val = event_data.get('value')
    
    msg = f"Delegated Click! Text: {text} | ID: {btn_id} | Value: {btn_val}"
    print(msg)
    win.run_js(f"uiLog('{msg}');")


# ==========================================
# 6. RUN THE APP
# ==========================================

def background_status_checker():
    """Demonstrates pushing async data from Python to JS while running."""
    time.sleep(2)
    if win.is_running():
        print(f"[Backend] Connected. URL: {win.get_url()}")
        # Push message to UI without being prompted
        win.run_js("uiLog('🚀 Python Backend fully loaded and injected UI hooks.');")

if __name__ == "__main__":
    print("Launching Shellac Test Dashboard...")
    
    threading.Thread(target=background_status_checker, daemon=True).start()
    
    # Launch with AnyBrowser. Will prefer Chromium to test the app_name/favicon trick.
    win.show(HTML_CONTENT, Browser.AnyBrowser)
    
    # Block until user closes the window
    win.wait()

    # Cleanup the dummy icon
    if os.path.exists(ICON_PATH):
        os.remove(ICON_PATH)
    
    print("Exited cleanly.")