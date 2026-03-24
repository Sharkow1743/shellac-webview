import inspect
import json
import os
import socket
import threading
import time
from typing import Any, Callable, Dict, Optional, Union

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from selenium import webdriver
from selenium.common.exceptions import WebDriverException, NoSuchWindowException

from .enums import Browser
from .models import WindowConfig, Event
from .launcher import BrowserLauncher


class Window:
    def __init__(self):
        self.config = WindowConfig()
        self.port = self._get_free_port()
        self.app = FastAPI()
        self.bindings: Dict[str, Callable] = {}
        self._html_content: Optional[str] = None
        self.driver: Optional[webdriver.Remote] = None
        self._running = False
        self._setup_routes()

    def _get_free_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('', 0))
            return s.getsockname()[1]

    def _get_bridge_js(self) -> str:
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
        @self.app.get("/")
        async def index():
            content = self._html_content or "<html><body>No Content</body></html>"
            bridge = f"<script>{self._get_bridge_js()}</script>"
            if "<head>" in content:
                return HTMLResponse(content.replace("<head>", f"<head>{bridge}"))
            return HTMLResponse(bridge + content)

    def _bind_target(self, target: Any, prefix: str = "", exact_name: bool = False):
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

    def bind(self, name_or_target: Union[str, Any] = None, target: Optional[Any] = None):
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
        if self.driver:
            try: return self.driver.execute_script(script)
            except Exception as e: print(f"[WebUI] JS Execution Error: {e}")
        return None

    def close(self):
        self._running = False
        if self.driver:
            try: self.driver.quit()
            except Exception: pass
            self.driver = None

    def set_size(self, width: int, height: int):
        self.config.width = width
        self.config.height = height
        if self.driver: self.driver.set_window_size(width, height)

    def set_title(self, title: str):
        self.run_js(f"document.title = {json.dumps(title)};")

    def is_running(self) -> bool:
        return self._running

    def _bridge_monitor(self):  
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