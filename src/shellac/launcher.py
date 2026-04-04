import os
import platform
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from selenium import webdriver
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities

import undetected_chromedriver as uc
from undetected_geckodriver import Firefox as UCFirefox

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
    def _apply_firefox_ui_hacks(profile_path: str, hide_controls: bool):
        """Injects CSS and preferences into a Firefox profile to hide UI elements."""
        profile_dir = Path(profile_path)
        chrome_dir = profile_dir / "chrome"
        chrome_dir.mkdir(exist_ok=True)

        # 1. Ensure the stylesheet preference is enabled in the profile's user.js
        # user.js overrides prefs.js on startup
        user_js = profile_dir / "user.js"
        pref_line = 'user_pref("toolkit.legacyUserProfileCustomizations.stylesheets", true);\n'
        
        existing_content = ""
        if user_js.exists():
            existing_content = user_js.read_text()
        
        if pref_line not in existing_content:
            with open(user_js, "a") as f:
                f.write(pref_line)

        # 2. Create/Overwrite the userChrome.css to hide the UI
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
            # If developer turned controls back ON, remove the hack file
            css_path.unlink()

    @classmethod
    def create_driver(cls, browser: Browser, url: str, config: WindowConfig) -> webdriver.Remote:
        path = cls.get_path(browser)
        
        # Resolve Data Directory
        if config.data_dir:
            user_data_path = str(Path(config.data_dir).absolute())
            os.makedirs(user_data_path, exist_ok=True)
        else:
            user_data_path = tempfile.mkdtemp(prefix="shellac_")

        driver = None

        # --- CHROMIUM (Chrome, Edge, Brave, Vivaldi) ---
        if browser in[Browser.Chrome, Browser.Edge, Browser.Chromium, Browser.Brave, Browser.Vivaldi]:
            
            # You MUST use uc.ChromeOptions(), standard ChromeOptions won't pass stealth
            options = uc.ChromeOptions()
            
            # The 'uc' package handles hiding the automation. We only need to configure the UI.
            options.add_argument(f"--user-data-dir={user_data_path}") 
            
            if config.hide_controls: 
                options.add_argument(f"--app={url}")
            
            if config.start_maximized: 
                options.add_argument("--start-maximized")
            else: 
                options.add_argument(f"--window-size={config.width},{config.height}")
            
            caps = DesiredCapabilities.CHROME.copy()
            caps['pageLoadStrategy'] = 'eager'

            driver = uc.Chrome(
                options=options,
                browser_executable_path=path if path else None,
                desired_capabilities=caps,   # <-- added
            )

        # --- FIREFOX ---
        elif browser == Browser.Firefox:
            cls._apply_firefox_ui_hacks(user_data_path, config.hide_controls)

            options = FirefoxOptions()
            if path: 
                options.binary_location = path
            
            options.add_argument("-profile")
            options.add_argument(user_data_path)
            
            # Apply your UI hacks
            options.set_preference("toolkit.legacyUserProfileCustomizations.stylesheets", True)
            options.set_preference("browser.tabs.inTitlebar", 0)
            options.set_preference("webdriver.load.strategy", "eager")

            if config.kiosk: 
                options.add_argument("--kiosk")
            
            # UCFirefox acts as a drop-in replacement for standard webdriver.Firefox
            # It natively patches geckodriver strings to bypass BotGuard checks
            driver = UCFirefox(options=options)
            
            if not config.start_maximized:
                driver.set_window_size(config.width, config.height)

        if driver is not None:
            # Patch connection pool to avoid warnings during heavy JS-Python traffic
            try:
                executor = driver.command_executor
                if hasattr(executor, '_conn'):
                    executor._conn.connection_pool_kw['maxsize'] = 20
                    executor._conn.clear()
            except: pass
            return driver

        raise ValueError(f"Unsupported browser: {browser}")