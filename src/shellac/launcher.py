import os
import platform
import shutil
import socket
import sys
import tempfile
from pathlib import Path
from typing import Optional, Dict
from urllib.parse import urlparse

from selenium import webdriver
from selenium.webdriver.firefox.options import Options as FirefoxOptions
import undetected_chromedriver as uc
from undetected_geckodriver import Firefox as UCFirefox

from .enums import Browser
from .models import WindowConfig


class BrowserLauncher:
    @staticmethod
    def _get_proxy_settings() -> Optional[Dict[str, Any]]:
        """Reads proxy settings supporting HTTP, HTTPS, and SOCKS4/5."""
        proxy_str = (
            os.environ.get("ALL_PROXY") or os.environ.get("all_proxy") or
            os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or
            os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
        )
        no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")

        if not proxy_str:
            return None

        parsed = urlparse(proxy_str if "://" in proxy_str else f"http://{proxy_str}")
        return {
            "raw": proxy_str,
            "scheme": parsed.scheme.lower(),
            "host": parsed.hostname,
            "port": parsed.port or (1080 if "socks" in parsed.scheme else 8080),
            "no_proxy": no_proxy
        }

    @staticmethod
    def get_path(browser: Browser) -> Optional[str]:
        system = platform.system()
        if system == "Windows":
            import winreg
            reg_paths = {
                Browser.Chrome: r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
                Browser.Edge: r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
                Browser.Firefox: r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\firefox.exe",
            }
            if browser in reg_paths:
                for hive in [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]:
                    try:
                        with winreg.OpenKey(hive, reg_paths[browser]) as key:
                            return winreg.QueryValue(key, None)
                    except FileNotFoundError:
                        continue
        
        names = {Browser.Chrome: "google-chrome", Browser.Firefox: "firefox", Browser.Edge: "microsoft-edge"}
        suffix = ".exe" if system == "Windows" else ""
        return shutil.which(names.get(browser, "") + suffix) or shutil.which(browser.name.lower() + suffix)

    @staticmethod
    def _apply_firefox_ui_hacks(profile_path: str, hide_controls: bool):
        profile_dir = Path(profile_path)
        chrome_dir = profile_dir / "chrome"
        chrome_dir.mkdir(parents=True, exist_ok=True)

        user_js = profile_dir / "user.js"
        pref_line = 'user_pref("toolkit.legacyUserProfileCustomizations.stylesheets", true);\n'
        
        existing_content = ""
        if user_js.exists():
            existing_content = user_js.read_text()
        
        if pref_line not in existing_content:
            with open(user_js, "a") as f:
                f.write(pref_line)

        css_path = chrome_dir / "userChrome.css"
        if hide_controls:
            css_content = """
            @namespace url("http://www.mozilla.org/keymaster/gatekeeper/there.is.only.xul");
            #nav-bar, #TabsToolbar, #PersonalToolbar, #sidebar-box, #urlbar-container {
                visibility: collapse !important;
            }
            """
            css_path.write_text(css_content)
        elif css_path.exists():
            css_path.unlink()

    @classmethod
    def create_driver(cls, browser: Browser, url: str, config: WindowConfig) -> webdriver.Remote:
        path = cls.get_path(browser)
        
        app_suffix = config.app_name.lower().replace(" ", "_")
        if config.data_dir:
            user_data_path = str(Path(config.data_dir).absolute())
        else:
            user_data_path = os.path.join(tempfile.gettempdir(), f"shellac_{app_suffix}")

        driver = None
        proxy_info = cls._get_proxy_settings()

        # --- CHROMIUM (Chrome, Edge, Brave, Vivaldi) ---
        if browser in [Browser.Chrome, Browser.Edge, Browser.Chromium, Browser.Brave, Browser.Vivaldi]:
            options = uc.ChromeOptions()
            
            options.page_load_strategy = 'none'
            options.set_capability('pageLoadStrategy', 'none')
            
            options.add_argument(f"--user-data-dir={user_data_path}") 
            options.add_argument(f"--class={config.app_name}")
            options.add_argument(f"--app-id={config.app_name}") 
            
            if config.hide_controls: 
                options.add_argument(f"--app={url}")
            
            if config.start_maximized: 
                options.add_argument("--start-maximized")
            else: 
                options.add_argument(f"--window-size={config.width},{config.height}")

            # Apply Proxy for Chromium
            if proxy_info:
                # Chromium handles socks5://host:port natively via --proxy-server
                options.add_argument(f"--proxy-server={proxy_info['raw']}")
                if proxy_info.get("no_proxy"):
                    options.add_argument(f"--proxy-bypass-list={proxy_info['no_proxy']}")

            driver = uc.Chrome(
                options=options,
                browser_executable_path=path if path else None,
            )

        # --- FIREFOX ---
        elif browser == Browser.Firefox:
            cls._apply_firefox_ui_hacks(user_data_path, config.hide_controls)

            m_port = cls._get_free_port()
            
            os.environ["MOZ_APP_REMOTINGNAME"] = config.app_name
            os.environ["MOZ_ENABLE_WAYLAND"] = "1" 

            options = FirefoxOptions()
            if path:
                options.binary_location = path
            
            options.add_argument("--no-remote")
            options.add_argument("--new-instance")
            options.add_argument("-profile")
            options.add_argument(user_data_path)
            
            options.set_preference("toolkit.legacyUserProfileCustomizations.stylesheets", True)
            options.set_preference("browser.tabs.inTitlebar", 0)
            options.set_capability('pageLoadStrategy', 'none')
            options.set_preference("webdriver.load.strategy", "none")

            # Apply Proxy for Firefox via preferences
            if proxy_info:
                scheme = proxy_info["scheme"]
                options.set_preference("network.proxy.type", 1)
                
                if "socks" in scheme:
                    options.set_preference("network.proxy.socks", proxy_info["host"])
                    options.set_preference("network.proxy.socks_port", proxy_info["port"])
                    options.set_preference("network.proxy.socks_version", 5 if scheme == "socks5" else 4)
                else:
                    options.set_preference("network.proxy.http", proxy_info["host"])
                    options.set_preference("network.proxy.http_port", proxy_info["port"])
                    options.set_preference("network.proxy.ssl", proxy_info["host"])
                    options.set_preference("network.proxy.ssl_port", proxy_info["port"])

                if proxy_info.get("no_proxy"):
                    options.set_preference("network.proxy.no_proxies_on", proxy_info["no_proxy"])

            if config.kiosk: 
                options.add_argument("--kiosk")
            
            log_file = os.path.join(os.getcwd(), f"geckodriver_{m_port}.log")
            print(f"[*] Firefox logs for this instance: {log_file}")

            service = webdriver.FirefoxService(
                log_path=log_file,
                service_args=[
                    '--marionette-port', str(m_port), 
                    '--log', 'debug'
                ]
            ) 
            
            driver = UCFirefox(options=options, service=service)
            
            if not config.start_maximized:
                driver.set_window_size(config.width, config.height)

        if driver is not None:
            try:
                executor = driver.command_executor
                if hasattr(executor, '_conn'):
                    executor._conn.connection_pool_kw['maxsize'] = 20
                    executor._conn.clear()
            except:
                pass
            return driver

        raise ValueError(f"Unsupported browser: {browser}")
    
    @staticmethod
    def _get_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('', 0))
            return s.getsockname()[1]