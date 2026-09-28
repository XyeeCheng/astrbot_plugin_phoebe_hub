"""One ephemeral DSH runtime per request; Hub supplies the bounded conversation.

No raw QQ identifiers or persistent DSH histories are required. Linux service
owns the worker process group, including its SDK child, so timeouts reap both.
"""
import json
import os
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path


SDK_VERSION = "0.1.5rc1"


def patch_text():
    # The pinned sdk-minimal tree is standalone (not layered over dsh-base).
    return """- id: persistent-bash
  disabled: true
- id: persistent-pwsh
  disabled: true
- id: session-log-deepseek
  config:
    enabled: false
- id: plugin-package-inventory-deepseek
  config:
    enabled: false
- id: sandbox-policy
  config:
    mode: read-only
- insert:
    - id: phoebe-clock
      name: """ + json.dumps(str(Path(__file__).with_name("clock.mjs").resolve())) + "\n"


def read_secret(name):
    file = os.environ.get(name + "_FILE", "")
    return Path(file).read_text(encoding="utf-8").strip() if file else os.environ.get(name, "").strip()


def run(payload, *, base_url=None, api_key=None, model=None):
    if version("deepseek-harness-sdk") != SDK_VERSION or version("deepseek-harness-runtime-bin") != SDK_VERSION:
        raise RuntimeError("Unsupported DSH SDK/runtime version")
    from deepseek_harness import DeepSeekHarness
    key = api_key or read_secret("DEEPSEEK_API_KEY")
    selected_model = model or os.environ.get("DSH_MODEL", "").strip()
    if not key or not selected_model:
        raise RuntimeError("DSH model/key missing")
    with tempfile.TemporaryDirectory(prefix="phoebe-dsh-") as directory:
        root = Path(directory)
        patch = root / "chat.patch.yml"
        patch.write_text(patch_text(), encoding="utf-8")
        messages = {"history": payload.get("history", []), "current_message": payload["prompt"]}
        harness = DeepSeekHarness(
            profile="sdk-minimal", cwd=str(root), dsh_home=str(root / "home"),
            patches=(str(patch),), model=selected_model, max_tokens=1024,
            initialize_timeout_seconds=15, request_timeout_seconds=35, shutdown_timeout_seconds=1,
            api_key=key, base_url=base_url or os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com/v1",
            env={"DSH_SYSTEM_PROMPT": payload["system"], "DSH_CONTEXT_WINDOW": "32768"},
        )
        try:
            harness.start()
            result = harness.run(json.dumps(messages, ensure_ascii=False), session_id="phoebe-" + payload["scope"][:32])
            if result.finish_reason not in ("completed", "stop", "end_turn") or not result.final_response.strip():
                raise RuntimeError("DSH did not finish a reply")
            return {"request_id": payload["request_id"], "text": result.final_response[:16000]}
        finally:
            harness.close()


def main():
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError("request too large")
        result = run(json.loads(raw))
    except Exception:
        # SDK exceptions can contain credentials, user prompts and endpoint details.
        result = {"error": "dsh_unavailable"}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
