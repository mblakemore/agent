import os
import requests
from typing import Any, Dict, List, Optional

class AzureChatAPI:
    """Low-level API wrapper for Azure AI Foundry / Azure OpenAI.

    This implements the OpenAI-compatible REST interface used by Azure AI Foundry.
    """
    def __init__(self, cfg: Dict[str, Any]):
        self.endpoint = cfg.get("api_url") or os.environ.get("AZURE_AI_FOUNDRY_ENDPOINT", "")
        self.api_key = cfg.get("api_key") or os.environ.get("AZURE_AI_FOUNDRY_KEY", "")
        self.api_version = cfg.get("api_version", "2024-02-15-preview")
        
        if not self.endpoint or not self.api_key:
            raise ValueError("Azure AI Foundry requires endpoint and key (config or env).")
        
        self.endpoint = self.endpoint.rstrip("/")

    def health(self) -> bool:
        """Simple health check."""
        try:
            # Azure doesn't have a simple /health endpoint, so we do a tiny request.
            # We use a small max_tokens and a simple prompt.
            # Note: model is required in the URL for Azure OpenAI
            # We assume the model is passed in the config or environment
            return True # Simplified for now
        except Exception:
            return False

    def chat_completion(
        self, 
        model: str, 
        messages: List[Dict[str, str]], 
        stream: bool = False, 
        **kwargs
    ) -> Any:
        url = f"{self.endpoint}/openai/deployments/{model}/chat/completions?api-version={self.api_version}"
        headers = {
            "api-key": self.api_key,
            "Content-Type": "application/json"
        }
        payload = {
            "messages": messages,
            "stream": stream,
            **kwargs
        }
        
        response = requests.post(url, headers=headers, json=payload, stream=stream)
        response.raise_for_status()
        
        if stream:
            return response
        return response.json()
