# __init__.py
# Copyright (C) 2026 Chai Chaimee
# Licensed under GNU General Public License. See COPYING.txt for details.

import addonHandler
from logHandler import log

try:
	addonHandler.initTranslation()
except addonHandler.AddonError:
	log.warning("Unable to init translations. This may be because the addon is running from NVDA scratchpad.")

from .plugin import GlobalPlugin  # noqa: E402, F401
