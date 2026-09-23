"""config package: engine settings (misaka.config.engine), product CFG and the roster
(misaka.config.product), role paths (profiles, identity) and the home (misaka.config.home). The names below
are the ones the codebase imports as ``from misaka.config import X``."""
from misaka.config.engine import (  # noqa: F401
    APP_NAME,
    APP_TITLE,
    CONFIG_DIR_NAME,
    VERSION,
    expand_tilde_path,
    get_agent_dir,
    get_auth_path,
    get_bin_dir,
    get_custom_themes_dir,
    get_debug_log_path,
    get_export_template_dir,
    get_models_path,
    get_sessions_dir,
    get_themes_dir,
)
from misaka.config.product import CFG, current_config, sisters  # noqa: F401
