import base64
import inspect
import json
import os
import platform
import socket
import threading
import time
from typing import Any, Callable, Dict, Optional, Union
import logging
import ctypes

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from selenium import webdriver
from selenium.common.exceptions import WebDriverException, NoSuchWindowException, InvalidSessionIdException

from .enums import Browser
from .models import WindowConfig, Event
from .launcher import BrowserLauncher

log = logging.getLogger('shellac')


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
        self._event_bindings: Dict[str, Dict[str, str]] = {}  # {event: {selector: callback_name}}
        self._html_content: Optional[str] = None
        self.driver: Optional[webdriver.Remote] = None
        self._running = False
        self._setup_routes()

    def _get_free_port(self) -> int:
        """Finds an available TCP port on the localhost."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('', 0))
            return s.getsockname()[1]

    def _get_event_bindings_js(self) -> str:
        """Returns JavaScript that sets up all registered DOM event listeners."""
        if not self._event_bindings:
            return ""

        js_lines = []
        for event, selectors in self._event_bindings.items():
            for selector, fn_name in selectors.items():
                # Use event delegation to support dynamic content
                safe_selector = selector.replace("'", "\\'")
                js_lines.append(f"""
                (function() {{
                    const eventType = '{event}';
                    const targetSelector = '{safe_selector}';
                    const handlerName = '{fn_name}';
                    const handler = function(e) {{
                        let target = e.target;
                        // Climb up to the matching selector if needed
                        if (!target.matches(targetSelector)) {{
                            target = target.closest(targetSelector);
                        }}
                        if (target) {{
                            const eventData = {{
                                type: e.type,
                                target: target.tagName,
                                id: target.id,
                                className: target.className,
                                value: target.value,
                                checked: target.checked,
                                data: e.detail || null
                            }};
                            window.webui.call(handlerName, eventData, target.innerText);
                        }}
                    }};
                    document.addEventListener(eventType, handler);
                    // Store handler for potential cleanup (optional)
                    window._webui_handlers = window._webui_handlers || {{}};
                    window._webui_handlers[eventType + '_' + targetSelector] = handler;
                }})();
                """)
        return "\n".join(js_lines)

    def _get_bridge_js(self) -> str:
        """Returns the JavaScript bridge code for Python-JS communication, including event bindings."""
        base_js = """
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
        event_setup = self._get_event_bindings_js()
        if event_setup:
            # Wait for DOM to be ready before attaching event listeners
            event_setup = f"""
            if (document.readyState === 'loading') {{
                document.addEventListener('DOMContentLoaded', () => {{ {event_setup} }});
            }} else {{
                {event_setup}
            }}
            """
        return base_js + event_setup

    def _setup_routes(self):
        """Configures the internal FastAPI routes."""
        @self.app.get("/")
        async def index():
            content = self._html_content or "<html><body>No Content</body></html>"
            bridge = f"<script>{self._get_bridge_js()}</script>"
            favicon = self._get_favicon_html()
            
            injection = f"{favicon}\n{bridge}"
            if "<head>" in content:
                return HTMLResponse(content.replace("<head>", f"<head>\n{injection}"))
            elif "<html>" in content:
                return HTMLResponse(content.replace("<html>", f"<html>\n<head>{injection}</head>"))
            return HTMLResponse(f"<html><head>{injection}</head>\n<body>{content}</body></html>")

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
            if self.driver is not None:
                try:
                    exists = self.driver.execute_script(
                        "return (typeof window.webui !== 'undefined' && typeof window._webui_resolve !== 'undefined');"
                    )
                    if not exists:
                        self.driver.execute_script(self._get_bridge_js())

                    # Get queue
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

                            # Find out if function accepts `Event` or not
                            wants_event = False
                            if params:
                                is_event_type = params[0].annotation is Event
                                is_event_name = params[0].name == 'event'
                                wants_event = is_event_type or is_event_name

                            try:
                                if wants_event:
                                    event_obj = Event(window=self, element=fn_name, data=js_args)
                                    if len(params) == 1:
                                        target_args = (event_obj,)
                                    else:
                                        target_args = (event_obj, *js_args)
                                else:
                                    target_args = tuple(js_args)

                                if inspect.iscoroutinefunction(cb):
                                    result = asyncio.run(cb(*target_args))
                                else:
                                    result = cb(*target_args)

                                result_json = json.dumps(result)
                                self.driver.execute_script(f"window._webui_resolve({call_id}, {result_json});")

                            except Exception as e:
                                log.exception(f"Call to '{fn_name}' failed: {e}")
                                self.driver.execute_script(f"window._webui_resolve({call_id}, null);")
                except InvalidSessionIdException:
                    log.debug("Browser session ended, stopping bridge monitor")
                    self._running = False
                    break
                except Exception:
                    log.debug("Error in bridge monitor loop", exc_info=True)

            time.sleep(0.1)
            
    def _get_favicon_html(self) -> str:
        """Reads the local icon and creates a base64 HTML tag for Chromium extraction."""
        if not self.config.icon_path or not os.path.exists(self.config.icon_path):
            return ""
        
        try:
            with open(self.config.icon_path, "rb") as f:
                encoded = base64.b64encode(f.read()).decode("utf-8")
            
            ext = self.config.icon_path.lower().split('.')[-1]
            mime_types = {
                "png": "image/png", "ico": "image/x-icon", 
                "jpg": "image/jpeg", "jpeg": "image/jpeg", "svg": "image/svg+xml"
            }
            mime = mime_types.get(ext, "image/png")
            return f'<link rel="icon" type="{mime}" href="data:{mime};base64,{encoded}">'
        except Exception as e:
            log.warning(f"Failed to load icon: {e}")
            return ""

    def _apply_windows_native_hacks(self):
        """Uses ctypes to forcibly inject the icon and separate taskbar grouping on Windows."""
        if platform.system() != "Windows" or not self.config.icon_path:
            return
            
        if not self.config.icon_path.endswith('.ico'):
            log.warning("Windows native icon replacement requires a .ico file format.")
            return

        def inject_native_icon():
            hwnd = 0
            # Wait for the browser window to appear
            for _ in range(50):
                if self.driver and self.driver.title:
                    hwnd = ctypes.windll.user32.FindWindowW(None, self.driver.title)
                    if hwnd: break
                time.sleep(0.1)
                
            if not hwnd:
                return

            try:
                # Load the .ico file
                LR_LOADFROMFILE = 0x0010
                IMAGE_ICON = 1
                WM_SETICON = 0x0080
                ICON_BIG = 1
                ICON_SMALL = 0
                
                hicon = ctypes.windll.user32.LoadImageW(
                    0, self.config.icon_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE
                )
                
                # Forcibly overwrite the browser's Windows handle icon
                if hicon:
                    ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon)
                    ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon)
            except Exception as e:
                log.debug(f"Failed to set Windows native icon: {e}")

        threading.Thread(target=inject_native_icon, daemon=True).start()
            
            
            
    # ======== PUBLIC METHODS ========

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

    def on(self, event: str, selector: str):
        """
        Decorator to bind a DOM event on elements matching a CSS selector to a Python function.

        The decorated function receives an Event object (or two args: event_data, textContent).

        Args:
            event (str): DOM event name (e.g., 'click', 'load', 'input').
            selector (str): CSS selector to match target elements.

        Returns:
            Callable: The decorator that registers the function.

        Example:
            >>> @win.on("click", "#myButton")
            ... def handle_click(event_data, text):
            ...     print(f"Button clicked! Text: {text}")
        """
        def decorator(func: Callable):
            # Register the function so it can be called from JS
            self.bind(func)  # uses func.__name__ as the key
            fn_name = func.__name__
            if event not in self._event_bindings:
                self._event_bindings[event] = {}
            self._event_bindings[event][selector] = fn_name
            return func
        return decorator

    def navigate(self, url_or_html: str):
        """
        Navigates the current window to a new URL, a local file, or raw HTML content.

        Args:
            url_or_html (str): A web URL (http://...), a path to a .html file, or a raw HTML string.
        """
        if self.driver is None:
            log.warning("Cannot navigate: driver is not initialized")
            return

        is_url = url_or_html.startswith(('http://', 'https://', 'file://'))
        if is_url:
            self.driver.get(url_or_html)
            log.debug(f"Navigated to URL: {url_or_html}")
        else:
            try:
                if len(url_or_html) < 1000 and os.path.exists(url_or_html):
                    with open(url_or_html, 'r', encoding='utf-8') as f:
                        self._html_content = f.read()
                    log.debug(f"Loaded HTML file: {url_or_html}")
                else:
                    self._html_content = url_or_html
                    log.debug("Set raw HTML content")
            except OSError as e:
                log.error(f"Failed to read HTML file: {e}")
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
        if self.driver is not None:
            try:
                return self.driver.execute_script(script)
            except Exception as e:
                log.error(f"JS Execution Error: {e}")
        return None

    def close(self):
        """Closes the browser window and shuts down the backend server."""
        self._running = False
        if self.driver is not None:
            try:
                self.driver.quit()
                log.debug("Browser driver closed")
            except Exception as e:
                log.error(f"Error while closing driver: {e}")
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
        if self.driver is not None:
            self.driver.set_window_size(width, height)
            log.debug(f"Window resized to {width}x{height}")

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
        if self.driver is not None:
            self.driver.refresh()
            log.debug("Page reloaded")

    def maximize(self):
        """Maximizes the window."""
        if self.driver is not None:
            self.driver.maximize_window()
            log.debug("Window maximized")

    def minimize(self):
        """Minimizes the window."""
        if self.driver is not None:
            self.driver.minimize_window()
            log.debug("Window minimized")

    def get_size(self) -> Dict[str, int]:
        """Returns the current window dimensions."""
        if self.driver is not None:
            return self.driver.get_window_size()
        return {"width": self.config.width, "height": self.config.height}

    def run_js_async(self, script: str):
        """Executes JavaScript without waiting for a return value."""
        if self.driver is not None:
            self.driver.execute_script(script)

    def set_position(self, x: int, y: int):
        """Moves the window to the specified coordinates."""
        if self.driver is not None:
            self.driver.set_window_position(x, y)
            log.debug(f"Window moved to ({x}, {y})")

    def get_position(self) -> Dict[str, int]:
        """Returns the window position."""
        return self.driver.get_window_position() if self.driver else {"x": 0, "y": 0}

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
                    with open(content, 'r', encoding='utf-8') as f:
                        self._html_content = f.read()
                    log.debug(f"Loaded HTML content from file: {content}")
                else:
                    self._html_content = content
                    log.debug("Set raw HTML content")
            except OSError as e:
                log.error(f"Failed to read HTML file: {e}")
                self._html_content = content
            url = f"http://127.0.0.1:{self.port}/"
        else:
            url = content
            log.debug(f"Using URL: {url}")

        threading.Thread(target=uvicorn.run, args=(self.app,),
                         kwargs={"host": "127.0.0.1", "port": self.port, "log_level": "error"},
                         daemon=True).start()
        log.debug(f"FastAPI server started on port {self.port}")

        target = browser
        if target == Browser.AnyBrowser:
            for b in [Browser.Chrome, Browser.Edge, Browser.Firefox]:
                if BrowserLauncher.get_path(b):
                    target = b
                    break
            log.debug(f"Auto-selected browser: {target}")

        self.driver = BrowserLauncher.create_driver(target, url, self.config)
        self.driver.get(url)
        log.info(f"Browser launched with {target.name}")
        self._apply_windows_native_hacks()
        threading.Thread(target=self._bridge_monitor, daemon=True).start()

    def wait(self):
        """
        Blocks the main thread until the browser window is closed.
        """
        try:
            while self._running:
                if self.driver is not None:
                    try:
                        _ = self.driver.window_handles
                    except (NoSuchWindowException, WebDriverException):
                        log.debug("Browser window closed")
                        break
                time.sleep(2)
        except KeyboardInterrupt:
            log.info("Interrupted by user")
        finally:
            self.close()