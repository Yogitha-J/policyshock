"""Day-1 check: one tiny call. Prints the reply and token usage so you can budget the $1."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from policyshock.serv_client import chat

r = chat("You are a concise assistant.", "Reply with the single word: ready", max_tokens=20, use_cache=False)
print(r["text"]); print("usage:", r["usage"])
