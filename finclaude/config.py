"""Project paths, environment loading and shared errors."""
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
PROFILE_DIR = ROOT / "profile"
REPORTS_DIR = ROOT / "reports"

# Keys are read from .env into the process only; nothing here prints them.
load_dotenv(ROOT / ".env")


class NotConfigured(RuntimeError):
    """An external service is not set up yet (missing key, gateway not running)."""


class DataUnavailable(RuntimeError):
    """Data could not be fetched. Callers stop and report instead of guessing."""


class CostNotApproved(RuntimeError):
    """A paid call was attempted before the user confirmed 端点 | 次数 | 单价 | 合计."""
