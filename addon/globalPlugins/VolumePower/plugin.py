# plugin.py

import time
import winsound
from threading import Thread

import config
import core
import globalPluginHandler
import gui
import synthDriverHandler
import ui
import wx
from logHandler import log

from . import audioGuard, powerControl
from .constants import ADDON_SUMMARY
from .countdownDialog import PowerCountdownDialog
from .settings import AudioGuardSettingsPanel

POWER_TAP_THRESHOLD_SECONDS = 0.3


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	"""Implementation of global commands for volume control, power control, and audio protection."""
	scriptCategory: str = ADDON_SUMMARY

	def __init__(self, *args, **kwargs) -> None:
		"""Initialization of the add-on: registers settings, starts audio guard checks."""
		super().__init__(*args, **kwargs)
		self._powerTapCount = 0
		self._powerLastTapTime = 0.0
		self._audioGuardEngine = audioGuard.getActiveEngine()
		gui.settingsDialogs.NVDASettingsDialog.categoryClasses.append(AudioGuardSettingsPanel)
		Thread(target=self._audioGuardEngine.runStartupChecks, daemon=True).start()
		self._audioGuardEngine.applySleepPrevention()
		if self._audioGuardEngine.settings["reinit"]:
			self._audioGuardEngine.resetSynth()

	def terminate(self, *args, **kwargs) -> None:
		"""This will be called when NVDA is finished with this global plugin."""
		super().terminate(*args, **kwargs)
		self._audioGuardEngine.releaseSleepPrevention()
		try:
			gui.settingsDialogs.NVDASettingsDialog.categoryClasses.remove(AudioGuardSettingsPanel)
		except ValueError:
			log.debug("Could not remove %s settings panel from NVDA settings dialogs", ADDON_SUMMARY)

	# ---------------------------------------------------------
	# Volume control scripts
	# ---------------------------------------------------------
	def script_vol_up(self, gesture) -> None:
		"""Increase NVDA speech volume by 5%."""
		self._adjustSynthVolume(step=5)

	script_vol_up.__doc__ = _("Increase NVDA volume 5%")
	script_vol_up.category = scriptCategory

	def script_vol_down(self, gesture) -> None:
		"""Decrease NVDA speech volume by 5%."""
		self._adjustSynthVolume(step=-5)

	script_vol_down.__doc__ = _("Decrease NVDA volume 5%")
	script_vol_down.category = scriptCategory

	def _adjustSynthVolume(self, step: int) -> None:
		"""Adjust the current synthesizer's volume by the given signed step and persist it."""
		try:
			synth = synthDriverHandler.getSynth()
			if not synth:
				ui.message(_("Error: No synthesizer available"))
				log.debug("No synthesizer available for volume adjustment")
				return
			newVolume = min(max(synth.volume + step, 0), 100)
			synth.volume = newVolume
			config.conf["speech"][synth.name]["volume"] = newVolume
			config.conf.save()
			ui.message(_("{volume}%").format(volume=newVolume))
			log.debug("NVDA volume changed to %s%% and saved", newVolume)
		except (AttributeError, KeyError, OSError) as volumeError:
			ui.message(_("Error: Failed to change NVDA volume"))
			log.debug("Failed to change NVDA volume: %s", volumeError)

	# ---------------------------------------------------------
	# Power control script: single tap shuts down, double tap restarts
	# ---------------------------------------------------------
	def script_power(self, gesture) -> None:
		"""Shut down the system on a single tap, restart it on a double tap."""
		currentTime = time.time()
		if currentTime - self._powerLastTapTime > POWER_TAP_THRESHOLD_SECONDS:
			self._powerTapCount = 0
		self._powerTapCount += 1
		self._powerLastTapTime = currentTime
		wx.CallLater(int(POWER_TAP_THRESHOLD_SECONDS * 1000), self._resolvePowerTap)

	script_power.__doc__ = _("Shut down the system (single tap) or restart it (double tap)")
	script_power.category = scriptCategory

	def _resolvePowerTap(self) -> None:
		"""Decide, once the multi-tap window has elapsed, whether to shut down or restart."""
		tapCount = self._powerTapCount
		if tapCount == 0:
			return
		self._powerTapCount = 0
		reboot = tapCount >= 2
		if self._audioGuardEngine.settings["dialogCounterEnabled"]:
			wx.CallAfter(self._showPowerCountdown, reboot)
		else:
			wx.CallAfter(self._executeShutdown, reboot)

	def _showPowerCountdown(self, reboot: bool) -> None:
		"""Show the cancellable 3-2-1 countdown dialog for the requested power action."""
		actionLabel = _("Restart") if reboot else _("Shutdown")
		PowerCountdownDialog(
			gui.mainFrame,
			actionLabel=actionLabel,
			onConfirm=lambda: self._executeShutdown(reboot),
			onCancel=lambda: ui.message(_("Cancelled")),
		).Show()

	def _executeShutdown(self, reboot: bool) -> None:
		"""Run the version-appropriate shutdown or restart sequence after countdown confirmation."""
		ui.message(_("Restart") if reboot else _("Shutdown"))
		if powerControl.IS_NVDA2026_OR_NEWER:
			core.callLater(50, self._runShutdownNewMethod, reboot)
		else:
			Thread(target=self._runShutdownOldMethod, args=(reboot,), daemon=True).start()

	def _runShutdownNewMethod(self, reboot: bool) -> None:
		"""Play the exit sound then shut down or restart (NVDA 2026 and newer).

		Runs entirely on the main thread via core.callLater scheduling. The wait after
		the exit sound must not use time.sleep, since that would freeze NVDA's
		single-threaded event pump and trigger a Watchdog timeout; the remaining delay
		is instead scheduled as a second, non-blocking core.callLater callback.
		"""
		powerControl.playExitSound()
		core.callLater(1500, self._finishShutdownNewMethod, reboot)

	def _finishShutdownNewMethod(self, reboot: bool) -> None:
		"""Perform the actual shutdown or restart once the exit sound delay has elapsed."""
		try:
			powerControl.performShutdown(reboot)
		except RuntimeError as shutdownError:
			self._reportShutdownFailure(shutdownError, "Shutdown failed: %s")

	def _runShutdownOldMethod(self, reboot: bool) -> None:
		"""Shut down or restart without the exit sound step (NVDA 2025.x).

		Runs on a background thread. ui.message and winsound.Beep must not be called
		directly from here, since both interact with NVDA's main-thread speech queue
		and core UI state; on failure the report is marshaled to the main thread via
		wx.CallAfter instead.
		"""
		try:
			powerControl.performShutdown(reboot)
		except RuntimeError as shutdownError:
			wx.CallAfter(self._reportShutdownFailure, shutdownError, "Shutdown failed (old method): %s")

	def _reportShutdownFailure(self, shutdownError: RuntimeError, logMessage: str) -> None:
		"""Report a failed shutdown or restart attempt. Must run on the main thread."""
		ui.message(str(shutdownError))
		winsound.Beep(500, 500)
		log.debug(logMessage, shutdownError)

	__gestures = {
		"kb:nvda+shift+pageup": "vol_up",
		"kb:nvda+shift+pagedown": "vol_down",
		"kb:alt+windows+s": "power",
	}
