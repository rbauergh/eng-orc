"""llama-swap proxy control: health, which model is resident, warm-up, unload.

The orchestrator treats model swaps as expensive scheduler events (tens of
seconds of VRAM traffic on a 12 GB card), so it wants to know what is
currently loaded and to group work accordingly. All endpoints degrade
gracefully: when the proxy lacks a control surface, the scheduler simply
loses swap-awareness, not correctness.
"""

from __future__ import annotations

import httpx

from ..config import ServerConfig


class SwapServer:
    def __init__(self, server: ServerConfig):
        self.server = server
        self._http = httpx.Client(
            base_url=server.control_url.rstrip("/"),
            timeout=httpx.Timeout(15.0, connect=server.connect_timeout),
        )

    def close(self) -> None:
        self._http.close()

    def health(self) -> bool:
        for path in ("/health", "/v1/models"):
            try:
                if self._http.get(path).status_code == 200:
                    return True
            except httpx.HTTPError:
                continue
        return False

    def running_models(self) -> list[dict]:
        """llama-swap /running returns the loaded model processes and states."""
        try:
            resp = self._http.get("/running")
            if resp.status_code != 200:
                return []
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return []
        if isinstance(data, dict):
            return data.get("running", []) or []
        return data if isinstance(data, list) else []

    def loaded_model(self) -> str | None:
        for entry in self.running_models():
            model = entry.get("model")
            state = (entry.get("state") or "").lower()
            if model and state in ("ready", "running", "loaded", ""):
                return model
        return None

    def unload_all(self) -> bool:
        """Frees VRAM (e.g. before the user wants the GPU for something else)."""
        for method, path in (("POST", "/api/models/unload"), ("GET", "/unload")):
            try:
                if self._http.request(method, path).status_code == 200:
                    return True
            except httpx.HTTPError:
                continue
        return False
