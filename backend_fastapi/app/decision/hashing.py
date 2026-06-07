import hashlib
import json
from typing import Any


def stable_hash(payload: Any) -> str:
    """
    生成稳定的 SHA256 哈希值
    用于防止数据篡改和审计
    """
    s = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(s.encode("utf-8")).hexdigest()
