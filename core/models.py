import os as _os

import transformers

from astrbot.api import logger

# 分词器数据（tokenizer.json，约 6.6MB）不随插件包发布：
# 优先使用 tiktoken；本模块缺失数据时回落到 char/4 估算，不能因此中断导入。
chat_tokenizer_dir = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
try:
    tokenizer = transformers.AutoTokenizer.from_pretrained(
        chat_tokenizer_dir, trust_remote_code=True
    )
except Exception as _e:  # noqa: BLE001
    logger.debug("[models] tokenizer 加载失败，回落 char/4 估算: %s", _e)
    tokenizer = None
