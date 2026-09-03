"""Product-owned model guidance for safe Hermes-LCM recall."""

from functools import lru_cache
import hashlib
from pathlib import Path


RECALL_POLICY_PATH = (
    Path(__file__).resolve().parent
    / "skills"
    / "hermes-lcm"
    / "references"
    / "recall-policy.md"
)
MAX_RECALL_POLICY_BYTES = 8 * 1024


@lru_cache(maxsize=1)
def get_recall_policy() -> str:
    """Return the canonical bounded recall policy shipped by this plugin."""
    policy = RECALL_POLICY_PATH.read_text(encoding="utf-8").strip()
    size = len(policy.encode("utf-8"))
    if not policy:
        raise RuntimeError(f"Hermes-LCM recall policy is empty: {RECALL_POLICY_PATH}")
    if size > MAX_RECALL_POLICY_BYTES:
        raise RuntimeError(
            f"Hermes-LCM recall policy exceeds {MAX_RECALL_POLICY_BYTES} bytes: {size}"
        )
    return policy


def recall_policy_sha256() -> str:
    """Return the digest used to prove the exact product policy in a run."""
    return hashlib.sha256(get_recall_policy().encode("utf-8")).hexdigest()
