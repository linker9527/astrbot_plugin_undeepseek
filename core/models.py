import logging

import transformers

logger = logging.getLogger(__name__)

import os as _os
chat_tokenizer_dir = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
tokenizer = transformers.AutoTokenizer.from_pretrained(
    chat_tokenizer_dir, trust_remote_code=True
)
