from __future__ import annotations

import logging
import urllib.request

from coc_ai_controller.models import EntityMapping
from coc_ai_controller.constants import ENTITY_MAPPING_URL, ENTITY_MAPPING_PATH

logger = logging.getLogger(__name__)


def fetch_entity_mapping() -> EntityMapping:
    """Refresh the community data_id → name table, keeping a copy under `~/.coc_ai`.

    The download is what keeps names current as the game adds entities, but it must
    not be what decides whether the application starts, so a failed refresh falls
    back to the cached copy and only raises when there is nothing cached.
    """
    try:
        # S310 wants the scheme checked; the URL is a constant, not caller input.
        with urllib.request.urlopen(ENTITY_MAPPING_URL, timeout=15) as response:  # noqa: S310
            text = response.read().decode("utf-8")
        ENTITY_MAPPING_PATH.write_text(text, encoding="utf-8")
        logger.info("Downloaded entity mapping to %s", ENTITY_MAPPING_PATH)
    except OSError as exc:
        if not ENTITY_MAPPING_PATH.exists():
            raise
        logger.warning("Entity mapping download failed (%s); using the cached copy", exc)
        text = ENTITY_MAPPING_PATH.read_text(encoding="utf-8")
    return EntityMapping.model_validate_json(text)
