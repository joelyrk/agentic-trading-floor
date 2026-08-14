"""Earliest process checks that must run before persistence modules import."""

from dotenv import load_dotenv

from backend.config import APIAccessSettings, ApplicationSettings, RuntimeSettings

load_dotenv()

runtime_settings = RuntimeSettings.from_env()
api_access_settings = APIAccessSettings.from_env()
application_settings = ApplicationSettings.from_env()
if runtime_settings.accounts_db.exists() and runtime_settings.accounts_db.is_dir():
    raise ValueError(f"ACCOUNTS_DB points to a directory: {runtime_settings.accounts_db}")
