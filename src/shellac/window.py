import inspect
import json
import os
import socket
import threading
import time
from typing import Any, Callable, Dict, Optional, Union

import urllib3
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from selenium import webdriver
from selenium.common.exceptions import WebDriverException, NoSuchWindowException
from selenium.webdriver.remote.remote_connection import RemoteConnection

from .enums import Browser
from .models import WindowConfig, Event
from .launcher import BrowserLauncher

RemoteConnection._conn_pool_size = 20
urllib3.connectionpool.HTTPConnectionPool.default_maxsize = 20
urllib3.connectionpool.HTTPSConnectionPool.default_maxsize = 20

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
        """Internal background thread that polls the JS bridge for incoming calls."""
        while self._running:
            if self.driver:
                try:
                    exists = self.driver.execute_script(
                        "return (typeof window.webui !== 'undefined' && typeof window._webui_resolve !== 'undefined');"
                    )
                    if not exists:
                        self.driver.execute_script(self._get_bridge_js())
                    
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
                            
                            sig = inspect.signature(cb)
                            params = list(sig.parameters.values())
                            
                            wants_event = any(p.annotation is Event for p in params) or \
                                          (params and params[0].name == 'event')

                            try:
                                if wants_event:
                                    event_obj = Event(window=self, element=fn_name, data=js_args)
                                    
                                    expects_extra_args = len(params) > 1 or any(
                                        p.kind == inspect.Parameter.VAR_POSITIONAL for p in params
                                    )

                                    if expects_extra_args:
                                        result = cb(event_obj, *js_args)
                                    else:
                                        result = cb(event_obj)
                                else:
                                    result = cb(*js_args)
                                
                                result_json = json.dumps(result)
                                self.driver.execute_script(f"window._webui_resolve({call_id}, {result_json});")
                                
                            except Exception as e:
                                self.driver.execute_script(f"window._webui_resolve({call_id}, null);")
                            
                except Exception:
                    pass
            time.sleep(0.2)
            
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

    def _bridge_monitor(self):  
        """Internal background thread that polls the JS bridge for incoming calls."""
        while self._running:
            if self.driver:
                try:
                    exists = self.driver.execute_script("return (typeof window.webui !== 'undefined' && typeof window._webui_resolve !== 'undefined');")
                    if not exists:
                        self.driver.execute_script(self._get_bridge_js())
                    
                    events = self.driver.execute_script("""
                        var e = window._webui_queue; 
                        window._webui_queue = []; 
                        return e;
                    """)
                    
                    for event_data in (events or []):
                        fn = event_data.get('fn')
                        call_id = event_data.get('id')
                        if fn in self.bindings:
                            event = Event(window=self, element=fn, data=event_data.get('args', []))
                            cb = self.bindings[fn]
                            result = cb(event)
                            result_json = json.dumps(result)
                            self.driver.execute_script(f"window._webui_resolve({call_id}, {result_json});")
                            
                except Exception:
                    pass
            time.sleep(0.05) 

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
                time.sleep(1)
        except KeyboardInterrupt: pass
        finally:
            self.close()