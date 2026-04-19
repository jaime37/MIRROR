import json
import os
import threading
from flask import request, has_request_context

_thread_local = threading.local()

# ── Embedded fallback data (used when locales/ directory is not present) ──────
_FALLBACK_LANGUAGES = {
    "zh": {"label": "中文",    "llmInstruction": "请使用中文回答。"},
    "en": {"label": "English", "llmInstruction": "Please respond in English."},
    "es": {"label": "Español", "llmInstruction": "Por favor, responde en español."},
    "fr": {"label": "Français","llmInstruction": "Veuillez répondre en français."},
    "pt": {"label": "Português","llmInstruction": "Por favor, responda em português."},
    "ru": {"label": "Русский", "llmInstruction": "Пожалуйста, отвечайте на русском языке."},
    "de": {"label": "Deutsch", "llmInstruction": "Bitte antworten Sie auf Deutsch."},
}

_locales_dir = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'locales')

# Load language registry (falls back to embedded data if directory missing)
try:
    with open(os.path.join(_locales_dir, 'languages.json'), 'r', encoding='utf-8') as f:
        _languages = json.load(f)
except (FileNotFoundError, OSError):
    _languages = _FALLBACK_LANGUAGES

# Load translation files (silently skip if directory missing)
_translations: dict = {}
try:
    for filename in os.listdir(_locales_dir):
        if filename.endswith('.json') and filename != 'languages.json':
            locale_name = filename[:-5]
            with open(os.path.join(_locales_dir, filename), 'r', encoding='utf-8') as f:
                _translations[locale_name] = json.load(f)
except (FileNotFoundError, OSError):
    pass  # No translations available — t() will return the key as-is


def set_locale(locale: str):
    """Set locale for current thread. Call at the start of background threads."""
    _thread_local.locale = locale


def get_locale() -> str:
    if has_request_context():
        raw = request.headers.get('Accept-Language', 'zh')
        return raw if raw in _translations else 'zh'
    return getattr(_thread_local, 'locale', 'zh')


def t(key: str, **kwargs) -> str:
    locale = get_locale()
    messages = _translations.get(locale, _translations.get('zh', {}))

    value = messages
    for part in key.split('.'):
        if isinstance(value, dict):
            value = value.get(part)
        else:
            value = None
            break

    if value is None:
        value = _translations.get('zh', {})
        for part in key.split('.'):
            if isinstance(value, dict):
                value = value.get(part)
            else:
                value = None
                break

    if value is None:
        return key

    if kwargs:
        for k, v in kwargs.items():
            value = value.replace(f'{{{k}}}', str(v))

    return value


def get_language_instruction() -> str:
    locale = get_locale()
    lang_config = _languages.get(locale, _languages.get('zh', {}))
    return lang_config.get('llmInstruction', '请使用中文回答。')
