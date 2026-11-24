import logging
import os
from pathlib import Path
from typing import Optional
import yaml
from pydantic import BaseModel, Field
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent.parent


def get_user_config_dir() -> Path:
    """Resolve Capri user config directory honoring XDG_CONFIG_HOME when set."""
    xdg = (os.environ.get("XDG_CONFIG_HOME") or "").strip()
    base = Path(xdg).expanduser() if xdg else (Path.home() / ".config")
    return base / "capri"


USER_CONFIG_DIR = get_user_config_dir()
USER_CONFIG_FILE = USER_CONFIG_DIR / "config.yaml"
USER_ENV_FILE = USER_CONFIG_DIR / ".env"

if USER_ENV_FILE.exists():
    load_dotenv(USER_ENV_FILE, override=False)
load_dotenv(ROOT_DIR / ".env", override=False)


PROVIDER_DEFAULTS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-flash",
        "env_key": "DEEPSEEK_API_KEY",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "default_model": "openai/gpt-oss-120b",
        "env_key": "GROQ_API_KEY",
    },
    "sambanova": {
        "base_url": "https://api.sambanova.ai/v1",
        "default_model": "Meta-Llama-3.3-70B-Instruct",
        "env_key": "SAMBANOVA_API_KEY",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "default_model": "gemini-2.5-flash",
        "env_key": "GEMINI_API_KEY",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "deepseek/deepseek-chat",
        "env_key": "OPENROUTER_API_KEY",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "env_key": "OPENAI_API_KEY",
    },
}


class LLMConfig(BaseModel):
    provider: str = "groq"
    base_url: Optional[str] = "https://api.groq.com/openai/v1"
    model: str = "openai/gpt-oss-120b"
    api_key: str = ""
    temperature: float = 0.75
    frequency_penalty: float = 0.6
    presence_penalty: float = 0.5
    max_tokens: int = 2048


class VoiceConfig(BaseModel):
    tts_enabled: bool = True
    tts_engine: str = "fish"  # "fish", "elevenlabs", "gemini", "silero", "edge"
    xtts_speaker_wav: str = "assets/capri_voice_clean.wav"
    fish_audio_api_key: str = ""
    fish_audio_voice_id: str = ""
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    gemini_api_key: str = ""
    gemini_voice: str = "Puck"
    tts_voice: str = "ru-RU-SvetlanaNeural"
    tts_rate: str = "+5%"
    tts_pitch: str = "+0Hz"
    stt_mode: str = "groq"  # "groq" (сверхбыстрый Whisper Turbo) или "local" (offline)
    stt_model_size: str = "base"
    stt_device: str = "cpu"
    stt_compute_type: str = "int8"
    stt_language: str = "ru"
    ptt_enabled: bool = True
    ptt_key: str = "KEY_RIGHTALT"
    wake_word_enabled: bool = True
    wake_words: list[str] = ["капри", "эй капри", "привет капри"]
    hands_free_barge_in: bool = True
    barge_in_rms_threshold: float = 0.032
    pipewire_aec_enabled: bool = False
    input_device: str = ""


class CompanionConfig(BaseModel):
    name: str = "Капри"
    user_name: str = "User"
    style: str = "edgerunner"
    mode: str = "normal"  # "normal", "focus", "quiet", "playful"
    proactive_enabled: bool = True


class VisionConfig(BaseModel):
    enabled: bool = True
    provider: str = "gemini"  # "gemini" or "ocr"
    gemini_api_key: str = ""
    model: str = "gemini-3.1-flash-lite"
    max_dimension: int = 1280
    jpeg_quality: int = 80


class UIConfig(BaseModel):
    language: str = "ru"  # "ru" or "en"
    avatar_enabled: bool = True
    avatar_mode: str = "2d"  # "2d" or "3d"
    avatar_size: int = 240
    reduced_motion: bool = False
    minimize_to_tray: bool = True
    auto_avatar_poses: bool = True
    game_mode_enabled: bool = True
    avatar_reactions_enabled: bool = True
    avatar_physics_enabled: bool = True
    avatar_autonomous_life: bool = True


class SafetyConfig(BaseModel):
    tool_policy: str = "ask"  # "safe" | "ask" | "trusted"
    allow_shell_read_only: bool = True


class MCPServerConfig(BaseModel):
    name: str
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    timeout: float = 20.0


class MCPConfig(BaseModel):
    enabled: bool = False
    servers: list[MCPServerConfig] = Field(default_factory=list)


class AppConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    companion: CompanionConfig = Field(default_factory=CompanionConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)


def _resolve_default_config_path() -> Path:
    local_cfg = ROOT_DIR / "config.yaml"
    if local_cfg.exists():
        return local_cfg
    if USER_CONFIG_FILE.exists():
        return USER_CONFIG_FILE
    example_cfg = ROOT_DIR / "config.example.yaml"
    if example_cfg.exists():
        return example_cfg
    return USER_CONFIG_FILE


def _resolve_writable_config_path() -> Path:
    local_cfg = ROOT_DIR / "config.yaml"
    if local_cfg.exists():
        return local_cfg
    return USER_CONFIG_FILE


def _resolve_writable_env_path() -> Path:
    """
    Store user API keys locally on disk with 0600 permissions:
    - If a project-local `.env` already exists (dev checkout), keep updating it.
    - Otherwise default to `~/.config/capri/.env` so upgrades/reinstalls preserve keys
      and app directories never accidentally contain secrets.
    """
    local_env = ROOT_DIR / ".env"
    if local_env.exists():
        return local_env
    USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return USER_ENV_FILE


def scrub_config_secrets(data: dict) -> dict:
    """Strip all secret API keys from serialized config data before writing YAML."""
    cleaned = dict(data)
    if isinstance(cleaned.get("llm"), dict):
        cleaned["llm"] = dict(cleaned["llm"])
        cleaned["llm"]["api_key"] = ""
    if isinstance(cleaned.get("vision"), dict):
        cleaned["vision"] = dict(cleaned["vision"])
        cleaned["vision"]["gemini_api_key"] = ""
    if isinstance(cleaned.get("voice"), dict):
        cleaned["voice"] = dict(cleaned["voice"])
        cleaned["voice"]["fish_audio_api_key"] = ""
        cleaned["voice"]["elevenlabs_api_key"] = ""
        cleaned["voice"]["gemini_api_key"] = ""
    return cleaned


def load_config(config_path: Optional[Path] = None) -> AppConfig:
    if USER_ENV_FILE.exists():
        load_dotenv(USER_ENV_FILE, override=False)
    load_dotenv(ROOT_DIR / ".env", override=False)
    if config_path is None:
        config_path = _resolve_default_config_path()

    data = {}
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    config = AppConfig(**data)

    # API Keys from env
    groq_key = os.getenv("GROQ_API_KEY")
    deepseek_key = os.getenv("DEEPSEEK_API_KEY")
    sambanova_key = os.getenv("SAMBANOVA_API_KEY")
    gemini_key = os.getenv("GEMINI_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")
    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    capri_key = os.getenv("CAPRI_API_KEY")

    fish_key = os.getenv("FISH_AUDIO_API_KEY")
    fish_voice = os.getenv("FISH_AUDIO_VOICE_ID")
    eleven_key = os.getenv("ELEVENLABS_API_KEY")
    eleven_voice = os.getenv("ELEVENLABS_VOICE_ID")

    if fish_key:
        config.voice.fish_audio_api_key = fish_key.strip()
    if fish_voice:
        config.voice.fish_audio_voice_id = fish_voice.strip()
    if eleven_key:
        config.voice.elevenlabs_api_key = eleven_key.strip()
    if eleven_voice:
        config.voice.elevenlabs_voice_id = eleven_voice.strip()
    if gemini_key:
        config.voice.gemini_api_key = gemini_key.strip()
        config.vision.gemini_api_key = gemini_key.strip()

    # Smart fallback for TTS engine based on configured keys
    if config.voice.fish_audio_api_key and "tts_engine" not in data.get("voice", {}):
        config.voice.tts_engine = "fish"
    elif config.voice.elevenlabs_api_key and "tts_engine" not in data.get("voice", {}):
        config.voice.tts_engine = "elevenlabs"
    elif config.voice.gemini_api_key and "tts_engine" not in data.get("voice", {}):
        config.voice.tts_engine = "gemini"

    # LLM Auto-detection / Provider setup
    env_model = os.getenv("LLM_MODEL") or os.getenv("GROQ_MODEL")
    if env_model:
        config.llm.model = env_model.strip()

    provider = (os.getenv("LLM_PROVIDER") or config.llm.provider or "").lower()
    if not provider:
        if deepseek_key:
            provider = "deepseek"
        elif sambanova_key and not groq_key and not gemini_key:
            provider = "sambanova"
        elif groq_key:
            provider = "groq"
        elif gemini_key:
            provider = "gemini"
        elif openrouter_key:
            provider = "openrouter"
        else:
            provider = "groq"
    config.llm.provider = provider

    known_default_urls = {p["base_url"] for p in PROVIDER_DEFAULTS.values()}
    if provider in PROVIDER_DEFAULTS:
        if not config.llm.base_url or config.llm.base_url in known_default_urls:
            config.llm.base_url = PROVIDER_DEFAULTS[provider]["base_url"]

    key_map = {
        "deepseek": deepseek_key,
        "sambanova": sambanova_key,
        "groq": groq_key,
        "gemini": gemini_key,
        "openrouter": openrouter_key,
        "openai": openai_key,
    }
    prov_key = key_map.get(provider)
    if prov_key:
        config.llm.api_key = prov_key.strip()
    elif capri_key and not config.llm.api_key:
        config.llm.api_key = capri_key.strip()

    if not config.llm.model:
        config.llm.model = PROVIDER_DEFAULTS.get(provider, {}).get("default_model", "deepseek-flash")
    elif provider == "deepseek" and (config.llm.model.startswith("openai/") or config.llm.model.startswith("gemini") or config.llm.model.startswith("Meta-")):
        config.llm.model = PROVIDER_DEFAULTS["deepseek"]["default_model"]
    elif provider == "sambanova" and (config.llm.model.startswith("openai/") or config.llm.model.startswith("deepseek") or config.llm.model.startswith("gemini")):
        config.llm.model = PROVIDER_DEFAULTS["sambanova"]["default_model"]
    elif provider == "groq" and (config.llm.model.startswith("gemini") or config.llm.model.startswith("deepseek") or config.llm.model.startswith("Meta-")):
        config.llm.model = PROVIDER_DEFAULTS["groq"]["default_model"]

    return config


def update_env_value(key: str, value: str, env_path: Optional[Path] = None) -> bool:
    """Безопасно сохраняет или обновляет переменную окружения в файле .env и в os.environ."""
    if env_path is None:
        env_path = _resolve_writable_env_path()
    try:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        found = False
        if env_path.exists():
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if stripped.startswith(f"{key}=") or stripped == key:
                        lines.append(f"{key}={value}\n")
                        found = True
                    else:
                        lines.append(line)
        if not found:
            lines.append(f"{key}={value}\n")
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        try:
            os.chmod(env_path, 0o600)
        except OSError:
            pass
        os.environ[key] = value
        return True
    except Exception as e:
        logger.error(f"Не удалось обновить .env ({key}): {e}")
        return False


def delete_env_value(key: str, env_path: Optional[Path] = None) -> bool:
    """Безопасно удаляет переменную окружения из файла .env и из os.environ."""
    os.environ.pop(key, None)
    if env_path is None:
        env_path = _resolve_writable_env_path()
    try:
        if not env_path.exists():
            return True
        kept = []
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith(f"{key}=") or stripped == key:
                    continue
                kept.append(line)
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(kept)
        try:
            os.chmod(env_path, 0o600)
        except OSError:
            pass
        return True
    except Exception as e:
        logger.error(f"Не удалось удалить ключ из .env ({key}): {e}")
        return False


def save_config(config: AppConfig, config_path: Optional[Path] = None) -> bool:
    if config_path is None:
        config_path = _resolve_writable_config_path()
    try:
        # Не сохраняем сырые API-ключи в config.yaml (все секреты хранятся в .env / env)
        data = scrub_config_secrets(config.model_dump())
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, default_flow_style=False)
        return True
    except Exception as e:
        logger.error(f"Failed to save config to {config_path}: {e}")
        return False
