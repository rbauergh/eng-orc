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

    def known_model_names(self) -> set[str]:
        """Best-effort model names from llama-swap's own API — includes
        aliases on versions/configs where /v1/models does not. Defensive
        about response shape; an empty set just means no extra knowledge."""
        names: set[str] = set()
        try:
            resp = self._http.get("/api/models")
            if resp.status_code == 200:
                data = resp.json()
                entries = data.get("models", data) if isinstance(data, dict) else data
                if isinstance(entries, list):
                    for entry in entries:
                        if isinstance(entry, str):
                            names.add(entry)
                        elif isinstance(entry, dict):
                            for key in ("id", "name", "model"):
                                value = entry.get(key)
                                if isinstance(value, str) and value:
                                    names.add(value)
                            aliases = entry.get("aliases")
                            if isinstance(aliases, list):
                                names.update(a for a in aliases if isinstance(a, str))
        except (httpx.HTTPError, ValueError):
            pass
        for entry in self.running_models():
            model = entry.get("model")
            if isinstance(model, str):
                names.add(model)
        return names

    _DECODE_KEYS = ("n_decoded", "tokens_predicted", "n_predicted", "tokens_evaluated_predicted")
    _PROMPT_KEYS = ("n_past", "n_prompt_tokens", "n_prompt_tokens_processed", "prompt_n", "n_ctx_used")

    @classmethod
    def _slot_numbers(cls, slot: dict) -> tuple[int, int]:
        """(decoded, prompt) token counts scavenged from a slot object —
        field names vary across llama-server versions and may be nested."""
        decoded = prompt = 0

        def scan(node) -> None:
            nonlocal decoded, prompt
            if isinstance(node, list):  # e.g. next_token is a LIST of objects
                for entry in node:
                    scan(entry)
                return
            if not isinstance(node, dict):
                return
            for key, value in node.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    lowered = key.lower()
                    if lowered in cls._DECODE_KEYS:
                        decoded = max(decoded, int(value))
                    elif lowered in cls._PROMPT_KEYS:
                        prompt = max(prompt, int(value))
                elif isinstance(value, (dict, list)):
                    scan(value)

        scan(slot)
        return decoded, prompt

    def slot_activity(self, model: str) -> tuple[int, int, int] | None:
        """(busy_slots, decoded_tokens, prompt_tokens) for a loaded model from
        the upstream /slots endpoint. Distinguishing decode from prefill
        matters: a long prefill shows zero decoded tokens while the GPU is
        working flat out. None when the endpoint is unavailable."""
        try:
            resp = self._http.get(f"/upstream/{model}/slots")
            if resp.status_code != 200:
                return None
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(data, list):
            return None
        busy = decoded = prompt = 0
        for slot in data:
            if not isinstance(slot, dict):
                continue
            processing = slot.get("is_processing")
            if processing is None:
                state = slot.get("state")
                processing = bool(state) and state not in (0, "idle")
            if not processing:
                continue
            busy += 1
            slot_decoded, slot_prompt = self._slot_numbers(slot)
            decoded += slot_decoded
            prompt += slot_prompt
        return busy, decoded, prompt

    def raw_slots(self, model: str, max_chars: int = 1500) -> str:
        """Raw slot JSON for diagnostics reports (parsing gaps become visible)."""
        import json as _json

        try:
            resp = self._http.get(f"/upstream/{model}/slots")
            if resp.status_code != 200:
                return f"(status {resp.status_code})"
            return _json.dumps(resp.json())[:max_chars]
        except (httpx.HTTPError, ValueError) as exc:
            return f"(unavailable: {exc})"

    def unload_all(self) -> bool:
        """Frees VRAM (e.g. before the user wants the GPU for something else)."""
        for method, path in (("POST", "/api/models/unload"), ("GET", "/unload")):
            try:
                if self._http.request(method, path).status_code == 200:
                    return True
            except httpx.HTTPError:
                continue
        return False
