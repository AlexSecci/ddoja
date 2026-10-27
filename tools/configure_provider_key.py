"""Save the DeepSeek key using a hidden prompt and Windows user-bound encryption."""
import getpass
from pathlib import Path
import sys
import warnings

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.provider_credentials import ProviderCredentials

if __name__ == "__main__":
    warnings.simplefilter("error", getpass.GetPassWarning)
    value = getpass.getpass("DeepSeek API key (hidden): ")
    ProviderCredentials(ROOT).save(value)
    value = None
    print("DeepSeek key saved with Windows user-bound encryption. Campaigns were not changed.")
