import os
import sys
from datetime import datetime

from azure.identity import DefaultAzureCredential
from azure.mgmt.containerinstance import ContainerInstanceManagementClient
from azure.keyvault.secrets import SecretClient
from azure.core.exceptions import ResourceNotFoundError

# ---------------------------------------------------------------------------
# PAIRS
# Format: (container_resource_group, container_name, keyvault_resource_group, keyvault_name)
# Add one tuple per container/keyvault pair.
# ---------------------------------------------------------------------------
PAIRS = [
    ("my-container-rg-1", "my-container-1", "my-keyvault-rg-1", "my-keyvault-1"),
    ("my-container-rg-2", "my-container-2", "my-keyvault-rg-2", "my-keyvault-2"),
    ("my-container-rg-3", "my-container-3", "my-keyvault-rg-3", "my-keyvault-3"),
]

SECRET_NAME = "container-ip"


# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------
class Colour:
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    CYAN   = "\033[96m"
    RESET  = "\033[0m"

    @staticmethod
    def info(msg):    print(f"{Colour.CYAN}[INFO]{Colour.RESET}     {msg}")
    @staticmethod
    def ok(msg):      print(f"{Colour.GREEN}[OK]{Colour.RESET}       {msg}")
    @staticmethod
    def warn(msg):    print(f"{Colour.YELLOW}[WARN]{Colour.RESET}     {msg}")
    @staticmethod
    def error(msg):   print(f"{Colour.RED}[ERROR]{Colour.RESET}    {msg}")
    @staticmethod
    def changed(msg): print(f"{Colour.YELLOW}[CHANGED]{Colour.RESET}  {msg}")


# ---------------------------------------------------------------------------
# Azure SDK helpers
# ---------------------------------------------------------------------------

def get_container_ip(
    credential: DefaultAzureCredential,
    subscription_id: str,
    container_resource_group: str,
    container_name: str,
) -> str | None:
    """Return the private IP of an Azure Container Instance, or None if unavailable."""
    try:
        client = ContainerInstanceManagementClient(credential, subscription_id)
        container_group = client.container_groups.get(container_resource_group, container_name)
        ip = container_group.ip_address.ip if container_group.ip_address else None
        if not ip:
            Colour.error(f"Container '{container_name}' has no IP address assigned.")
        return ip
    except ResourceNotFoundError:
        Colour.error(f"Container '{container_name}' not found in resource group '{container_resource_group}'.")
        return None
    except Exception as e:
        Colour.error(f"Unexpected error fetching IP for '{container_name}': {e}")
        return None


def get_keyvault_secret(
    credential: DefaultAzureCredential,
    keyvault_name: str,
    secret_name: str,
) -> str | None:
    """
    Return the current value of a Key Vault secret.
    Returns None ONLY if the secret genuinely does not exist.
    Raises on all other errors so misconfigurations are not silently skipped.
    """
    vault_url = f"https://{keyvault_name}.vault.azure.net"
    client = SecretClient(vault_url=vault_url, credential=credential)
    try:
        secret = client.get_secret(secret_name)
        return secret.value
    except ResourceNotFoundError:
        return None
    except Exception as e:
        raise RuntimeError(f"Failed to read secret '{secret_name}' from '{keyvault_name}': {e}") from e


def update_keyvault_secret(
    credential: DefaultAzureCredential,
    keyvault_name: str,
    secret_name: str,
    value: str,
) -> None:
    """Update an existing Key Vault secret. Does NOT create if missing."""
    vault_url = f"https://{keyvault_name}.vault.azure.net"
    client = SecretClient(vault_url=vault_url, credential=credential)
    try:
        client.set_secret(secret_name, value)
    except Exception as e:
        raise RuntimeError(f"Failed to update secret '{secret_name}' in '{keyvault_name}': {e}") from e


# ---------------------------------------------------------------------------
# Core logic per pair
# ---------------------------------------------------------------------------

def check_and_sync_pair(
    credential: DefaultAzureCredential,
    subscription_id: str,
    container_resource_group: str,
    container_name: str,
    keyvault_resource_group: str,
    keyvault_name: str,
) -> bool:
    """
    Check if the container IP matches the Key Vault secret.
    Updates the secret if there is a mismatch.
    Skips if the secret does not exist — will NOT create it.
    Returns True on success, False on error.
    """
    print()
    print("-" * 60)
    Colour.info(f"Container     : {container_name}")
    Colour.info(f"Container RG  : {container_resource_group}")
    Colour.info(f"Key Vault     : {keyvault_name}")
    Colour.info(f"Key Vault RG  : {keyvault_resource_group}")
    print("-" * 60)

    # --- Get current IP from container ---
    Colour.info("Fetching current IP from container instance...")
    current_ip = get_container_ip(credential, subscription_id, container_resource_group, container_name)
    if not current_ip:
        return False

    expected_secret = f"http://{current_ip}:443"
    Colour.info(f"Current container IP  : {current_ip}")
    Colour.info(f"Expected secret value : {expected_secret}")

    # --- Get current secret from Key Vault ---
    Colour.info(f"Fetching secret '{SECRET_NAME}' from Key Vault '{keyvault_name}'...")
    try:
        current_secret = get_keyvault_secret(credential, keyvault_name, SECRET_NAME)
    except RuntimeError as e:
        Colour.error(str(e))
        return False

    if current_secret is None:
        Colour.error(
            f"Secret '{SECRET_NAME}' does not exist in Key Vault '{keyvault_name}'. "
            "Please create it manually before running this script."
        )
        return False

    Colour.info(f"Current secret value  : {current_secret}")

    # --- Compare and update if needed ---
    if current_secret == expected_secret:
        Colour.ok("Secret is up to date. No changes needed.")
        return True

    Colour.changed("Mismatch detected — updating secret...")
    Colour.changed(f"  Old : {current_secret}")
    Colour.changed(f"  New : {expected_secret}")

    try:
        update_keyvault_secret(credential, keyvault_name, SECRET_NAME, expected_secret)
        Colour.ok(f"Secret updated successfully in '{keyvault_name}'.")
        return True
    except RuntimeError as e:
        Colour.error(str(e))
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("=" * 60)
    print(f" Container IP Sync — {timestamp}")
    print(f" Pairs to check: {len(PAIRS)}")
    print("=" * 60)

    credential = DefaultAzureCredential()
    subscription_id = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
    if not subscription_id:
        Colour.error("AZURE_SUBSCRIPTION_ID environment variable is not set.")
        sys.exit(1)

    error_count = 0

    for container_resource_group, container_name, keyvault_resource_group, keyvault_name in PAIRS:
        success = check_and_sync_pair(
            credential,
            subscription_id,
            container_resource_group,
            container_name,
            keyvault_resource_group,
            keyvault_name,
        )
        if not success:
            error_count += 1

    print()
    print("=" * 60)
    if error_count > 0:
        Colour.warn(f"Completed with {error_count} error(s). Check logs above.")
        sys.exit(1)
    else:
        Colour.ok("All pairs checked successfully.")
    print("=" * 60)
