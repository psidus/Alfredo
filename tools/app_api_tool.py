import json
from typing import Type

import requests
from pydantic import BaseModel, Field
from crewai.tools import BaseTool


class AppApiCallerInput(BaseModel):
    """Input schema for AppApiCallerTool."""
    endpoint: str = Field(
        ...,
        description="API path to call (e.g., '/users/123', '/orders?status=active')."
    )
    method: str = Field(
        default="GET",
        description="HTTP method: GET, POST, PUT, or DELETE."
    )
    body: str = Field(
        default="",
        description="JSON string body for POST/PUT requests (e.g., '{\"name\": \"John\"}')."
    )
    params: str = Field(
        default="",
        description="JSON string of query parameters (e.g., '{\"page\": 1, \"limit\": 10}')."
    )


class AppApiCallerTool(BaseTool):
    """
    A CrewAI tool that allows agents to call REST API endpoints of a connected
    external application. Supports standard HTTP methods with JSON payloads.

    Use this tool when you need to:
    - Fetch data from an app's API (GET)
    - Create resources via POST
    - Update resources via PUT
    - Delete resources via DELETE
    """

    name: str = "app_api_caller"
    description: str = (
        "Call REST API endpoints of a connected external app. "
        "Specify the endpoint path, HTTP method, and optional JSON body or query params."
    )
    args_schema: Type[BaseModel] = AppApiCallerInput

    # Custom fields needed for execution
    base_url: str = Field(..., description="Base URL of the app's API (e.g., 'https://api.example.com').")
    api_key: str = Field(default="", description="API key for Bearer token authentication.")
    app_name: str = Field(default="", description="Name of the connected app.")

    def _run(self, endpoint: str, method: str = "GET", body: str = "", params: str = "") -> str:
        try:
            # Compose full URL
            url = self.base_url.rstrip("/") + "/" + endpoint.lstrip("/")

            # Build headers
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            # Parse optional JSON body
            json_body = None
            if body:
                try:
                    json_body = json.loads(body)
                except json.JSONDecodeError as e:
                    return f"Error: Invalid JSON in 'body' parameter: {e}"

            # Parse optional query params
            query_params = None
            if params:
                try:
                    query_params = json.loads(params)
                except json.JSONDecodeError as e:
                    return f"Error: Invalid JSON in 'params' parameter: {e}"

            # Execute the HTTP request
            method = method.upper().strip()
            response = requests.request(
                method=method,
                url=url,
                headers=headers,
                json=json_body,
                params=query_params,
                timeout=30
            )

            # Check for HTTP errors
            response.raise_for_status()

            # Format response body
            try:
                response_data = response.json()
                return json.dumps(response_data, indent=2, ensure_ascii=False)
            except (json.JSONDecodeError, ValueError):
                return response.text

        except requests.exceptions.ConnectionError:
            return (
                f"Error: Could not connect to '{self.app_name}' API at {self.base_url}. "
                "Please verify the base URL and network connectivity."
            )
        except requests.exceptions.Timeout:
            return (
                f"Error: Request to '{self.app_name}' API timed out after 30 seconds. "
                "The endpoint may be slow or unresponsive."
            )
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response is not None else "unknown"
            response_text = ""
            if e.response is not None:
                try:
                    response_text = e.response.text[:500]
                except Exception:
                    pass
            return (
                f"Error: HTTP {status_code} from '{self.app_name}' API "
                f"({method} {url}): {response_text}"
            )
        except Exception as e:
            return f"Error calling '{self.app_name}' API: {e}"
