import inspect
import json
import os
import socket
import threading
import time
from typing import Any, Callable, Dict, Optional, Union
import asyncio

import urllib3
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from selenium import webdriver
from selenium.common.exceptions import WebDriverException, NoSuchWindowException

from .enums import Browser
from .models import WindowConfig, Event
from .launcher import BrowserLauncher

class Window:
    """
    Represents a WebUI window instance that manages a FastAPI backend 
    and a Selenium-controlled browser frontend.
    """

    def __init__(self):
        """Initializes a new Window instance with default configurations."""
        self.config = WindowConfig()
        self.port = self._get_free_port()
        self.app = FastAPI()
        self.bindings: Dict[str, Callable] = {}
        self._html_content: Optional[str] = None
        self.driver: Optional[webdriver.Remote] = None
        self._running = False
        self._setup_routes()

    def _get_free_port(self) -> int:
        """Finds an available TCP port on the localhost."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('', 0))
            return s.getsockname()[1]

    def _get_bridge_js(self) -> str:
        """Returns the JavaScript bridge code for Python-JS communication."""
        return """
        if (typeof window.webui === 'undefined') {
            window._webui_queue = [];
            window._webui_promises = {};
            window._webui_call_id = 0;
            window.webui = {
                call: (fn, ...args) => {
                    return new Promise((resolve, reject) => {
                        const id = window._webui_call_id++;
                        window._webui_promises[id] = resolve;
                        window._webui_queue.push({fn: fn, args: args, id: id});
                    });
                }
            };
            window._webui_resolve = (id, result) => {
                if (window._webui_promises[id]) {
                    window._webui_promises[id](result);
                    delete window._webui_promises[id];
                }
            };
        }
        """

    def _setup_routes(self):
        """Configures the internal FastAPI routes."""
        @self.app.get("/")
        async def index():
            content = self._html_content or "<html><body>No Content</body></html>"
            bridge = f"<script>{self._get_bridge_js()}</script>"
            if "<head>" in content:
                return HTMLResponse(content.replace("<head>", f"<head>{bridge}"))
            return HTMLResponse(bridge + content)

    def _bind_target(self, target: Any, prefix: str = "", exact_name: bool = False):
        """Internal helper to map functions or class methods to the registry."""
        if inspect.isfunction(target) or inspect.ismethod(target):
            name = prefix if (exact_name and prefix) else (f"{prefix}.{target.__name__}" if prefix else target.__name__)
            self.bindings[name] = target
        else:
            if inspect.isclass(target):
                try:
                    obj = target()
                except TypeError as e:
                    raise ValueError(f"Cannot auto-instantiate class '{target.__name__}'.") from e
            else:
                obj = target
                
            for name in dir(obj):
                if not name.startswith('_'): 
                    attr = getattr(obj, name)
                    if callable(attr):
                        bind_name = f"{prefix}.{name}" if prefix else name
                        self.bindings[bind_name] = attr

    def _bridge_monitor(self):
        import asyncio
        from shellac.models import Event

        while self._running:
            if self.driver:
                try:
                    # 1. Check if the bridge is initialized in the browser
                    exists = self.driver.execute_script(
                        "return (typeof window.webui !== 'undefined' && typeof window._webui_resolve !== 'undefined');"
                    )
                    if not exists:
                        self.driver.execute_script(self._get_bridge_js())
                    
                    # 2. Collect pending calls from the JavaScript queue
                    events = self.driver.execute_script("""
                        var e = window._webui_queue; 
                        window._webui_queue = []; 
                        return e;
                    """)
                    
                    for event_data in (events or []):
                        fn_name = event_data.get('fn')
                        call_id = event_data.get('id')
                        js_args = event_data.get('args', [])

                        if fn_name in self.bindings:
                            cb = self.bindings[fn_name]
                            
                            # 3. Inspect the Python function signature
                            sig = inspect.signature(cb)
                            params = list(sig.parameters.values())
                            
                            # Determine if the function wants the Shellac 'Event' object
                            wants_event = False
                            if params:
                                # We check if 1st arg is named 'event' or typed as 'Event'
                                is_event_type = params[0].annotation is Event
                                is_event_name = params[0].name == 'event'
                                wants_event = is_event_type or is_event_name

                            try:
                                # 4. Prepare the execution call
                                if wants_event:
                                    event_obj = Event(window=self, element=fn_name, data=js_args)
                                    # If function only takes (event), don't unpack JS args
                                    if len(params) == 1:
                                        target_args = (event_obj,)
                                    else:
                                        target_args = (event_obj, *js_args)
                                else:
                                    # Standard function: just pass the JS arguments
                                    target_args = tuple(js_args)

                                # 5. Execute (Handle Async vs Sync)
                                if inspect.iscoroutinefunction(cb):
                                    # Run async function in a temporary event loop
                                    result = asyncio.run(cb(*target_args))
                                else:
                                    # Run standard function
                                    result = cb(*target_args)
                                
                                # 6. Return the result to JavaScript
                                result_json = json.dumps(result)
                                self.driver.execute_script(f"window._webui_resolve({call_id}, {result_json});")
                                
                            except Exception as e:
                                # Log the error for the developer
                                print(f"[Shellac Error] Call to '{fn_name}' failed: {e}")
                                import traceback
                                traceback.print_exc()
                                
                                # Inform JS that the call failed
                                self.driver.execute_script(f"window._webui_resolve({call_id}, null);")
                            
                except Exception:
                    # Catch broad driver/poll errors silently to keep the thread alive
                    pass
            
            # Polling frequency (50ms is good for UI responsiveness)
            time.sleep(0.1)
            
    def bind(self, name_or_target: Union[str, Any] = None, target: Optional[Any] = None):
        """
        Binds a Python function, class, or instance to be callable from JavaScript.

        This method can be used as a decorator or as a direct function call.

        Args:
            name_or_target (Union[str, Any], optional): The name/prefix for the binding 
                or the target itself if no name is provided.
            target (Any, optional): The function or class instance to bind (only used 
                if name_or_target is a string).

        Returns:
            The original target or a decorator function.

        Examples:
            >>> win.bind("say_hello", lambda e: "Hello!")
            >>> @win.bind("math")
            ... class Math:
            ...     def add(self, e): return e.data[0] + e.data[1]
        """
        if isinstance(name_or_target, str) and target is not None:
            is_func = inspect.isfunction(target) or inspect.ismethod(target)
            self._bind_target(target, prefix=name_or_target, exact_name=is_func)
            return target
            
        if isinstance(name_or_target, str) and target is None:
            def decorator(decor_target: Any):
                is_func = inspect.isfunction(decor_target) or inspect.ismethod(decor_target)
                self._bind_target(decor_target, prefix=name_or_target, exact_name=is_func)
                return decor_target
            return decorator
            
        if name_or_target is not None:
            self._bind_target(name_or_target, exact_name=False)
            return name_or_target
            
        return self

    def navigate(self, url_or_html: str):
        """
        Navigates the current window to a new URL, a local file, or raw HTML content.

        Args:
            url_or_html (str): A web URL (http://...), a path to a .html file, 
                or a raw HTML string.
        """
        if not self.driver:
            return
            
        is_url = url_or_html.startswith(('http://', 'https://', 'file://'))
        if is_url:
            self.driver.get(url_or_html)
        else:
            try:
                if len(url_or_html) < 1000 and os.path.exists(url_or_html):
                    with open(url_or_html, 'r', encoding='utf-8') as f:
                        self._html_content = f.read()
                else:
                    self._html_content = url_or_html
            except OSError:
                self._html_content = url_or_html
                
            self.driver.get(f"http://127.0.0.1:{self.port}/")

    def run_js(self, script: str) -> Any:
        """
        Executes synchronous JavaScript code in the browser.

        Args:
            script (str): The JavaScript code to execute.

        Returns:
            Any: The result returned by the JavaScript execution.
        """
        if self.driver:
            try: return self.driver.execute_script(script)
            except Exception as e: print(f"[WebUI] JS Execution Error: {e}")
        return None

    def close(self):
        """Closes the browser window and shuts down the backend server."""
        self._running = False
        if self.driver:
            try: self.driver.quit()
            except Exception: pass
            self.driver = None

    def set_size(self, width: int, height: int):
        """
        Resizes the browser window.

        Args:
            width (int): Target width in pixels.
            height (int): Target height in pixels.
        """
        self.config.width = width
        self.config.height = height
        if self.driver: self.driver.set_window_size(width, height)

    def set_title(self, title: str):
        """
        Updates the browser window title.

        Args:
            title (str): The new title string.
        """
        self.run_js(f"document.title = {json.dumps(title)};")

    def is_running(self) -> bool:
        """
        Checks if the window and backend are currently running.

        Returns:
            bool: True if running, False otherwise.
        """
        return self._running
    
    def get_url(self) -> str:
        """Returns the current URL of the browser."""
        return self.driver.current_url if self.driver else ""

    def reload(self):
        """Reloads the current page."""
        if self.driver:
            self.driver.refresh()

    def maximize(self):
        """Maximizes the window."""
        if self.driver:
            self.driver.maximize_window()

    def minimize(self):
        """Minimizes the window."""
        if self.driver:
            self.driver.minimize_window()

    def get_size(self) -> Dict[str, int]:
        """Returns the current window dimensions."""
        if self.driver:
            return self.driver.get_window_size()
        return {"width": self.config.width, "height": self.config.height}

    def set_always_on_top(self, enabled: bool = True):
        """Note: Selenium doesn't support this natively across all OS, 
        but we can execute a script or suggest using a wrapper."""
        print("[Shellac] Always-on-top is not natively supported by Selenium drivers.")

    def run_js_async(self, script: str):
        """Executes JavaScript without waiting for a return value."""
        if self.driver:
            self.driver.execute_script(script)

    def set_position(self, x: int, y: int):
        """Moves the window to the specified coordinates."""
        if self.driver:
            self.driver.set_window_position(x, y)

    def get_position(self) -> Dict[str, int]:
        """Returns the window position."""
        return self.driver.get_window_position() if self.driver else {"x": 0, "y": 0}

    # Example of a new developer-friendly alert helper
    def alert(self, message: str):
        """Shows a native browser alert."""
        self.run_js(f"alert({json.dumps(message)});")

    def show(self, content: str, browser: Browser = Browser.AnyBrowser):
        """
        Starts the backend server and launches the browser window.

        Args:
            content (str): The URL, HTML file path, or HTML string to display.
            browser (Browser): The browser engine to use (defaults to AnyBrowser).
        """
        self._running = True
        is_url = content.startswith(('http://', 'https://', 'file://'))
        if not is_url:
            try:
                if len(content) < 1000 and os.path.exists(content):
                    with open(content, 'r', encoding='utf-8') as f: self._html_content = f.read()
                else: self._html_content = content
            except OSError:
                self._html_content = content
            url = f"http://127.0.0.1:{self.port}/"
        else:
            url = content

        threading.Thread(target=uvicorn.run, args=(self.app,), 
                        kwargs={"host": "127.0.0.1", "port": self.port, "log_level": "error"}, 
                        daemon=True).start()

        target = browser
        if target == Browser.AnyBrowser:
            for b in [Browser.Chrome, Browser.Edge, Browser.Firefox]:
                if BrowserLauncher.get_path(b):
                    target = b
                    break

        self.driver = BrowserLauncher.create_driver(target, url, self.config)
        self.driver.get(url)
        threading.Thread(target=self._bridge_monitor, daemon=True).start()

    def wait(self):
        """
        Blocks the main thread until the browser window is closed.
        """
        try:
            while self._running:
                if self.driver:
                    try:
                        _ = self.driver.window_handles
                    except (NoSuchWindowException, WebDriverException):
                        break
                time.sleep(2)
        except KeyboardInterrupt: pass
        finally:
            self.close()