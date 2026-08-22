import os

import requests
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

load_dotenv()

pushover_user = os.getenv("PUSHOVER_USER")
pushover_token = os.getenv("PUSHOVER_TOKEN")
pushover_url = "https://api.pushover.net/1/messages.json"


mcp = FastMCP("push_server")


class PushModelArgs(BaseModel):
    message: str = Field(description="A brief message to push")


@mcp.tool()
def push(args: PushModelArgs):
    """Send a push notification with this brief message"""
    if not pushover_user or not pushover_token:
        raise RuntimeError("Push notification unavailable: credentials are not configured")
    payload = {"user": pushover_user, "token": pushover_token, "message": args.message}
    response = requests.post(pushover_url, data=payload, timeout=10)
    response.raise_for_status()
    return "Push notification sent"


if __name__ == "__main__":
    mcp.run(transport="stdio")
