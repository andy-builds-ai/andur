"""Where the workspace keys live.

The keys belong to the workspaces (Taug); Svar's answer function reads them with Taug's
key check (taug/access/keys.py), packed into it."""

from aws_cdk import Aws

#: Where the workspace keys live: ``<path>/<workspace>``, one SecureString each.
#: The stack creates none; `python -m taug.scripts.workspace` adds and removes them.
API_KEY_PARAMETER_PATH = "/andur/api-keys"


def api_key_path_arn() -> str:
    """The ARN of the key path; a key is this plus ``/<workspace>``."""
    return (
        f"arn:{Aws.PARTITION}:ssm:{Aws.REGION}:{Aws.ACCOUNT_ID}:parameter{API_KEY_PARAMETER_PATH}"
    )
