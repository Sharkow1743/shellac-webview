import platform
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from selenium import webdriver
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.chrome.service import Service as ChromeService

from .enums import Browser
from .models import WindowConfig


class BrowserLauncher:
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
                    except FileNotFoundError: continue
        
        names = {Browser.Chrome: "google-chrome", Browser.Firefox: "firefox", Browser.Edge: "microsoft-edge"}
        suffix = ".exe" if system == "Windows" else ""
        return shutil.which(names.get(browser, "") + suffix) or shutil.which(browser.name.lower() + suffix)

    @staticmethod
    def prepare_firefox_profile(config: WindowConfig) -> str:
        temp_dir = tempfile.mkdtemp(prefix="webui_ff_")
        chrome_dir = Path(temp_dir) / "chrome"
        chrome_dir.mkdir()

        prefs = {
            "toolkit.legacyUserProfileCustomizations.stylesheets": "true",
            "browser.tabs.inTitlebar": "0", 
            "browser.shell.checkDefaultBrowser": "false",
            "browser.startup.page": "0",
            "browser.tabs.warnOnClose": "false",
            "security.csp.enable": "false",  
            "security.mixed_content.block_active_content": "false", 
            "network.websocket.allowInsecureFromHttp": "true",
            "devtools.chrome.enabled": "true"
        }
        
        with open(Path(temp_dir) / "prefs.js", "w") as f:
            for k, v in prefs.items():
                f.write(f'user_pref("{k}", {v});\n')

        if config.hide_controls:
            with open(chrome_dir / "userChrome.css", "w") as f:
                f.write("""
                @namespace url("http://www.mozilla.org/keymaster/gatekeeper/there.is.only.xul");
                #nav-bar, #TabsToolbar, #PersonalToolbar, #sidebar-box, #urlbar-container {
                    visibility: collapse !important;
                }
                """)
        return temp_dir

    @staticmethod
    def _patch_driver_pool(driver: webdriver.Remote, size: int = 20):
        """
        Manually increases the connection pool size of the driver's 
        internal urllib3 PoolManager to prevent 'pool is full' warnings.
        """
        try:
            # Access the command_executor (RemoteConnection)
            executor = driver.command_executor
            
            # Selenium 4.x uses a PoolManager stored in _conn
            if hasattr(executor, '_conn'):
                # Update the pool keywords for new connections
                executor._conn.connection_pool_kw['maxsize'] = size
                executor._conn.connection_pool_kw['block'] = False
                
                # Clear the current pool to force it to re-initialize with new settings
                executor._conn.clear()
        except Exception:
            pass

    @classmethod
    def create_driver(cls, browser: Browser, url: str, config: WindowConfig) -> webdriver.Remote:
        path = cls.get_path(browser)
        driver = None
        
        # Determine Data Directory (Persistent vs Temporary)
        user_data_path = config.data_dir
        if user_data_path:
            user_data_path = str(Path(user_data_path).absolute())
            if not os.path.exists(user_data_path):
                os.makedirs(user_data_path, exist_ok=True)
        else:
            user_data_path = tempfile.mkdtemp(prefix="shellac_")

        if browser in [Browser.Chrome, Browser.Edge, Browser.Chromium, Browser.Brave, Browser.Vivaldi]:
            is_edge = "Edge" in str(browser)
            options = webdriver.EdgeOptions() if is_edge else webdriver.ChromeOptions()
            
            if path: options.binary_location = path
            
            if config.hide_controls:
                options.add_argument(f"--app={url}")
            
            options.add_argument("--disable-web-security")
            options.add_argument("--allow-running-insecure-content") 
            # Use the data_dir here
            options.add_argument(f"--user-data-dir={user_data_path}") 
            
            if config.start_maximized:
                options.add_argument("--start-maximized")
            else:
                options.add_argument(f"--window-size={config.width},{config.height}")
            
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            
            if is_edge:
                driver = webdriver.Edge(options=options, service=EdgeService())
            else:
                driver = webdriver.Chrome(options=options, service=ChromeService())

        elif browser == Browser.Firefox:
            # For Firefox, if data_dir is custom, we point the profile there
            options = webdriver.FirefoxOptions()
            if path: options.binary_location = path
            
            if config.data_dir:
                # Use existing profile path
                options.add_argument("-profile")
                options.add_argument(user_data_path)
            else:
                # Create temporary optimized profile
                profile_path = cls.prepare_firefox_profile(config) 
                options.add_argument("-profile")
                options.add_argument(profile_path)

            if config.kiosk: options.add_argument("--kiosk")
            
            driver = webdriver.Firefox(service=FirefoxService(), options=options)
            if not config.start_maximized:
                driver.set_window_size(config.width, config.height)

        if driver:
            cls._patch_driver_pool(driver, size=20)
            return driver