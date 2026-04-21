import os
from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()

def _csv_ints(s: str, default: str):
    s = (s or default)
    out = []
    for x in s.split(","):
        x = x.strip()
        if x.isdigit():
            out.append(int(x))
    return out

class Config(BaseModel):
    # Human-readable build tag (overridable via APP_VERSION env).
    app_version: str = str(os.getenv('APP_VERSION', 'v19_heal_speedup') or 'v19_heal_speedup')
    api_id: int = int(os.getenv('API_ID', '0') or '0')
    api_hash: str = str(os.getenv('API_HASH', '') or '')
    session_name: str = os.getenv("SESSION_NAME", "autopilot")
    auth_mode: str = str(os.getenv('AUTH_MODE', os.getenv('TELEGRAM_AUTH_MODE', 'phone')) or 'phone').strip().lower()
    phone: str = str(os.getenv('PHONE', os.getenv('TELEGRAM_PHONE', '')) or '')
    string_session: str = str(os.getenv('STRING_SESSION', os.getenv('TELEGRAM_STRING_SESSION', '')) or '').strip()
    force_sms: bool = os.getenv('FORCE_SMS', os.getenv('TELEGRAM_FORCE_SMS', '0')).lower() in ('1','true','yes','on')
    game_chat: str = str(os.getenv('GAME_CHAT', '@ForestSpirits_bot') or '@ForestSpirits_bot')
    owner_id: int = int(os.getenv('OWNER_ID', '0') or '0')

    mode: str = os.getenv("MODE", "max_reward")
    blacklist: list[str] = [s.strip() for s in (os.getenv("BLACKLIST","") or "").split(",") if s.strip()]

    timezone: str = os.getenv("TIMEZONE", "Europe/Andorra")
    forest_btn: str = os.getenv("FOREST_TRIGGER_BUTTON", "🌲 Лес")
    forest_fallback_cmd: str = os.getenv("FOREST_FALLBACK_COMMAND", "/forest")
    sleep_night_from: int = int(os.getenv("SLEEP_NIGHT_FROM", "2"))
    sleep_night_to: int = int(os.getenv("SLEEP_NIGHT_TO", "7"))

    preferred_tiers: list[int] = _csv_ints(os.getenv("PREFERRED_TIERS",""), "1")

    health_min_absolute: int = int(os.getenv("HEALTH_MIN_ABSOLUTE", "300"))
    heal_missing_threshold: int = int(os.getenv("HEAL_MISSING_THRESHOLD", "300"))
    health_pause_min: int = int(os.getenv("HEALTH_PAUSE_MIN", "15"))
    health_pause_max: int = int(os.getenv("HEALTH_PAUSE_MAX", "30"))

    fish_hook_button: str = os.getenv("FISH_HOOK_BUTTON", "подсеч")
    fish_cast_button: str = os.getenv("FISH_CAST_BUTTON", "закинуть")

    fish_strike_delay_min: float = float(os.getenv("FISH_STRIKE_DELAY_MIN", "0.25"))
    fish_strike_delay_max: float = float(os.getenv("FISH_STRIKE_DELAY_MAX", "2.50"))
    fish_min_click_gap_sec: float = float(os.getenv("FISH_MIN_CLICK_GAP_SEC", "2.5"))

    # Fishing: auto-equip a new rod from inventory when it breaks/missing.
    # If disabled (default), the bot will turn fishing OFF when rod is missing.
    fish_auto_equip_rod: bool = os.getenv("FISH_AUTO_EQUIP_ROD", "1").lower() in ("1","true","yes","on")

    # Healing (post-battle)
    # Target HP fraction to keep when heal module is enabled.
    # 0.99 means "try to stay at >=99% HP".
    heal_target_pct_default: float = float(os.getenv("HEAL_TARGET_PCT", "0.99"))
    # Blood-heal contour (additional routing over base /lvl)
    blood_hp_low: int = int(os.getenv("BLOOD_HP_LOW", "60"))
    blood_hp_high: int = int(os.getenv("BLOOD_HP_HIGH", "95"))
    blood_level: int = int(os.getenv("BLOOD_LEVEL", "1"))
    # module toggles (runtime features)
    mod_forest_enabled: bool = os.getenv('MOD_FOREST_ENABLED', os.getenv('FOREST_ENABLED', '1')).lower() in ('1','true','yes','on')
    mod_fishing_enabled: bool = os.getenv('MOD_FISHING_ENABLED', os.getenv('FISHING_ENABLED', '1')).lower() in ('1','true','yes','on')
    mod_heal_enabled: bool = os.getenv('MOD_HEAL_ENABLED', os.getenv('HEAL_ENABLED', '1')).lower() in ('1','true','yes','on')
    mod_auto_switch_enabled: bool = os.getenv('MOD_AUTO_SWITCH_ENABLED', os.getenv('AUTOSWITCH_ENABLED', '1')).lower() in ('1','true','yes','on')
    mod_work_enabled: bool = os.getenv('MOD_WORK_ENABLED', os.getenv('WORK_ENABLED', '0')).lower() in ('1','true','yes','on')
    mod_dungeon_enabled: bool = os.getenv('MOD_DUNGEON_ENABLED', os.getenv('DUNGEON_ENABLED', '0')).lower() in ('1','true','yes','on')

    # Party / Group
    # Accept invites, run pre-dungeon buff routine, and manage party lifecycle.
    mod_party_enabled: bool = os.getenv('MOD_PARTY_ENABLED', os.getenv('PARTY_ENABLED', '1')).lower() in ('1','true','yes','on')

    # In party: if HP% drops below this threshold, use "full heal" (configurable via !partyhp).
    party_heal_threshold_pct: float = float(os.getenv('PARTY_HEAL_THRESHOLD_PCT', '60')) / 100.0


    # Pets / Home-care automation ("погладить всех")
    # Default is OFF (safer). Enable via /pet on or env MOD_PET_ENABLED=1
    mod_pet_enabled: bool = os.getenv('MOD_PET_ENABLED', os.getenv('PET_ENABLED', '0')).lower() in ('1','true','yes','on')

    # Petting interval (in hours): after a successful 'pet all', schedule next run in [min..max] hours.
    pet_interval_min_hours: float = float(os.getenv('PET_INTERVAL_MIN_HOURS', os.getenv('PET_INTERVAL_MIN', '1')))
    pet_interval_max_hours: float = float(os.getenv('PET_INTERVAL_MAX_HOURS', os.getenv('PET_INTERVAL_MAX', '2')))

    # Button text to enter Home
    pet_home_button: str = os.getenv('PET_HOME_BUTTON', 'Дом')

    # Slow bot tolerance: scale waits for chat replies/history polling.
    # Useful when the game answers in ~3-15 seconds and UI often updates by editing
    # an existing message instead of sending a brand new one.
    response_timeout_factor: float = float(os.getenv('RESPONSE_TIMEOUT_FACTOR', '1.35'))
    response_poll_min_sec: float = float(os.getenv('RESPONSE_POLL_MIN_SEC', '0.6'))

    # Deprecated: old window-based scheduling (no longer used).
    pet_night_from: int = int(os.getenv('PET_NIGHT_FROM', '2'))
    pet_night_to: int = int(os.getenv('PET_NIGHT_TO', '3'))

    # --- Golems ---
    # When golem_fight is OFF, we avoid golems by taking a timed pause.
    # 1) After retreating from a golem event: pause for a short window.
    golem_soft_pause_min: int = int(os.getenv('GOLEM_SOFT_PAUSE_MIN', '4'))
    golem_soft_pause_max: int = int(os.getenv('GOLEM_SOFT_PAUSE_MAX', '7'))
    # 2) If a golem appears in the target list (battle choice) while golem_fight is OFF,
    #    we pause longer to let them disappear.
    golem_battle_pause_min: int = int(os.getenv('GOLEM_BATTLE_PAUSE_MIN', '7'))
    golem_battle_pause_max: int = int(os.getenv('GOLEM_BATTLE_PAUSE_MAX', '12'))


    # Heal speed tuning (seconds). Lower = faster, but too low may trigger flood-wait.
    heal_click_delay_min: float = float(os.getenv('HEAL_CLICK_DELAY_MIN', '0.15'))
    heal_click_delay_max: float = float(os.getenv('HEAL_CLICK_DELAY_MAX', '0.35'))
    heal_after_click_wait_sec: float = float(os.getenv('HEAL_AFTER_CLICK_WAIT_SEC', '0.25'))
    heal_hp_update_timeout_sec: float = float(os.getenv('HEAL_HP_UPDATE_TIMEOUT_SEC', '0.8'))



    # LM Studio (local LLM) for dungeon decisions.
    lmstudio_base_url: str = str(os.getenv('LMSTUDIO_BASE_URL', 'http://127.0.0.1:1234/v1') or 'http://127.0.0.1:1234/v1').strip()
    lmstudio_model: str = str(os.getenv('LMSTUDIO_MODEL', 'google/gemma-4-e4b') or 'google/gemma-4-e4b').strip()
    lmstudio_timeout_sec: float = float(os.getenv('LMSTUDIO_TIMEOUT_SEC', '20'))
    lmstudio_temperature: float = float(os.getenv('LMSTUDIO_TEMPERATURE', '0.1'))
    lmstudio_max_tokens: int = int(os.getenv('LMSTUDIO_MAX_TOKENS', '80'))

    # legacy names kept for compatibility (do not use in new code)
    forest_enabled: bool = mod_forest_enabled
    fishing_enabled: bool = mod_fishing_enabled
    heal_enabled: bool = mod_heal_enabled
    autoswitch_enabled: bool = mod_auto_switch_enabled
    work_enabled: bool = mod_work_enabled

CFG = Config()
