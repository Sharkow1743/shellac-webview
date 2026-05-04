from typing import Any, List, Optional
from pydantic import BaseModel, Field, ConfigDict

class WindowConfig(BaseModel):
    width: int = 1000
    height: int = 800
    hide_controls: bool = True 
    kiosk: bool = False
    data_dir: Optional[str] = None
    start_maximized: bool = False
    app_name: str = "App"
    icon_path: Optional[str] = None

class Event(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    window: Any  
    element: str = ""
    data: List[Any] = Field(default_factory=list)

    def get_string(self, index: int = 0) -> str:
        return str(self.data[index]) if index < len(self.data) else ""
        
    def get_int(self, index: int = 0) -> int:
        try: return int(self.data[index])
        except (IndexError, ValueError): return 0
        
    def get_dict(self, index: int = 0) -> dict:
        try: return dict(self.data[index])
        except (IndexError, ValueError, TypeError): return {}