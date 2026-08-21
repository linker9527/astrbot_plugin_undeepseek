"""
令牌计数模块 —— 优先使用 tiktoken，回退到 HuggingFace tokenizer，
最终回退到 char/4 估算。
"""

import logging

logger = logging.getLogger(__name__)

_tokenizer = None


def _get_tiktoken():
    """尝试加载 tiktoken cl100k_base 编码器。"""
    try:
        import tiktoken
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def _get_hf_tokenizer():
    """尝试获取 HuggingFace tokenizer（在 models.py 中被加载）。"""
    try:
        from . import models
        if models.tokenizer is not None:
            return models.tokenizer
    except Exception:
        pass
    return None


def count_tokens(text: str) -> int:
    """估算文本的 token 数。

    优先级：tiktoken > HuggingFace tokenizer > len(text)//4
    """
    global _tokenizer

    if _tokenizer is None:
        _tokenizer = _get_tiktoken()
        if _tokenizer is None:
            _tokenizer = _get_hf_tokenizer()
        if _tokenizer is None:
            _tokenizer = "char4"  # fallback marker

    if _tokenizer == "char4":
        return max(1, len(text) // 4)

    try:
        if hasattr(_tokenizer, "encode"):
            result = _tokenizer.encode(text)
            if isinstance(result, list):
                return len(result)
            if hasattr(result, "__len__"):
                return len(result)
    except Exception as e:
        logger.debug(f"[count_tokens] 编码失败: {e}")

    return max(1, len(text) // 4)


def count_tokens_for_messages(messages: list) -> int:
    """估算消息列表的总 token 数。"""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and "text" in part:
                    total += count_tokens(part["text"])
        else:
            total += count_tokens(str(content))
    return max(1, total)
