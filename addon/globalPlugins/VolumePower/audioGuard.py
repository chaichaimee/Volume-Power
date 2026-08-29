# audioGuard.py

import ctypes
import json
import os
import threading
from ctypes import POINTER, cast
from typing import Any, Callable, Dict, List, Optional, Set

import config
import core
import globalVars
import nvwave
import synthDriverHandler
import tones
import ui
import wx
from comtypes import CLSCTX_ALL, CoInitialize, CoUninitialize
from logHandler import log
from pycaw.utils import AudioUtilities, IAudioEndpointVolume, ISimpleAudioVolume

from .constants import ADDON_NAME

_: Callable[[str], str]

DEVICE_STATE_ACTIVE = 1

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

SYNTH_RESET_RETRY_DELAY_MS = 1000
SYNTH_RESET_MAX_RETRY_DELAY_MS = 30000

DEFAULT_SETTINGS: Dict[str, Any] = {
	"volume": 20,
	"minlevel": 5,
	"reinit": True,
	"retries": 0,
	"switchdevice": True,
	"playsound": True,
	"protectAllDevices": True,
	"alertOnDeviceDisabled": True,
	"preventSleep": False,
	"dialogCounterEnabled": True,
}


def getSettingsFilePath() -> str:
	"""Return the absolute path to the audio guard JSON settings file inside the user's NVDA config folder.
	@return: absolute file path
	@rtype: str
	"""
	settingsDir = os.path.join(globalVars.appArgs.configPath, "ChaiChaimee")
	os.makedirs(settingsDir, exist_ok=True)
	return os.path.join(settingsDir, f"{ADDON_NAME}_audioGuard.json")


class AudioGuardEngine:
	"""Protects the user's audio setup by restoring volume, mute state, and device
	visibility on NVDA startup, and can optionally prevent automatic system sleep.
	"""

	def __init__(self) -> None:
		self.settings: Dict[str, Any] = dict(DEFAULT_SETTINGS)
		self._knownActiveDeviceIds: List[str] = []
		self._lock = threading.Lock()
		self.loadSettings()
		self._sleepPrevented = False
		self._synthResetAttempts = 0

	def loadSettings(self) -> None:
		"""Load audio guard settings from the JSON file, falling back to defaults on missing or invalid data."""
		settingsPath = getSettingsFilePath()
		if not os.path.isfile(settingsPath):
			return
		try:
			with open(settingsPath, "r", encoding="utf-8") as settingsFile:
				storedSettings = json.load(settingsFile)
		except (OSError, ValueError) as loadError:
			log.debug("Failed to load audio guard settings, using defaults: %s", str(loadError))
			return
		self.settings.update({key: storedSettings[key] for key in DEFAULT_SETTINGS if key in storedSettings})
		self._knownActiveDeviceIds = storedSettings.get("knownActiveDeviceIds", [])

	def saveSettings(self) -> None:
		"""Persist current audio guard settings and the in-memory known-active device cache to disk.

		This only serializes self._knownActiveDeviceIds and never performs COM hardware
		enumeration itself, since this method is also called directly from the NVDA
		settings panel on the main thread (AudioGuardSettingsPanel.onSave), where a
		blocking device query could trigger a Watchdog freeze. Callers that need the
		cache refreshed from hardware should update self._knownActiveDeviceIds on a
		background thread beforehand, as runStartupChecks does. Only the payload capture
		is guarded by self._lock; the actual disk write is offloaded to a separate daemon
		thread so a slow filesystem (roaming profiles, a busy disk) or lock contention with
		a background startup check can never block the NVDA main event pump.
		"""
		settingsPath = getSettingsFilePath()
		with self._lock:
			payload = dict(self.settings)
			payload["knownActiveDeviceIds"] = list(self._knownActiveDeviceIds)

		def writeSettingsFile() -> None:
			try:
				with open(settingsPath, "w", encoding="utf-8") as settingsFile:
					json.dump(payload, settingsFile, indent="\t")
			except OSError as saveError:
				log.debug("Failed to save audio guard settings: %s", str(saveError))

		threading.Thread(target=writeSettingsFile, daemon=True).start()

	def _collectActiveDeviceIds(self) -> List[str]:
		"""Return the IDs of currently active playback devices.

		On enumeration failure, falls back to the last known-good cache. That read is
		guarded by self._lock, since self._knownActiveDeviceIds can be concurrently
		reassigned by runStartupChecks on the background thread.

		@return: list of active device IDs
		@rtype: list
		"""
		try:
			devices = AudioUtilities.GetAllDevices()
		except OSError as enumerationError:
			log.debug("Failed to enumerate audio devices: %s", str(enumerationError))
			with self._lock:
				return list(self._knownActiveDeviceIds)
		return [device.id for device in devices if getattr(device, "state", DEVICE_STATE_ACTIVE) == DEVICE_STATE_ACTIVE]

	def runStartupChecks(self) -> None:
		"""Run all audio protection routines. Intended to run on a background thread.

		The background thread is initialized into a single-threaded COM apartment so
		pycaw endpoint objects created here are safe to use, and any routine that
		touches core NVDA state (config, speech, UI) is marshaled back to the main
		thread instead of being executed directly on this thread. Hardware enumeration
		is always performed here, on the background thread, since it can block for a
		long time if an audio driver is hung; only the resulting UI notification is
		marshaled to the main thread. The confirmation sound is deliberately not played
		here when a device switch is pending, since switchToDefaultOutputDevice runs
		asynchronously on the main thread and playing a sound from both threads within
		milliseconds of each other causes audio buffer truncation. Even when no device
		switch is pending, playConfirmationSound() is still marshaled to the main thread
		via wx.CallAfter rather than called directly here, since nvwave's audio pipeline
		is bound to the main thread's COM apartment and calling it from this background
		STA thread would violate COM marshaling rules. Mutation of the shared
		self._knownActiveDeviceIds cache is guarded by self._lock, released before
		calling saveSettings() (which acquires the same non-reentrant lock itself) to
		avoid a deadlock.
		"""
		CoInitialize()
		try:
			volumeStateChanged = self.unmuteAudio()
			if self.unmuteNvdaProcess():
				volumeStateChanged = True
			if self.settings["protectAllDevices"]:
				self.restoreAllDeviceVolumes()
			currentActiveDeviceIds = set(self._collectActiveDeviceIds())
			if self.settings["alertOnDeviceDisabled"]:
				missingDeviceIds = self._computeMissingDeviceIds(currentActiveDeviceIds)
				wx.CallAfter(self._alertMissingDevices, missingDeviceIds)
			with self._lock:
				self._knownActiveDeviceIds = list(currentActiveDeviceIds)
			self.saveSettings()
			if self.settings["switchdevice"]:
				wx.CallAfter(self._finishStartupOnMainThread, volumeStateChanged)
			elif volumeStateChanged and self.settings["playsound"]:
				wx.CallAfter(self.playConfirmationSound)
		finally:
			CoUninitialize()

	def _finishStartupOnMainThread(self, volumeStateChanged: bool) -> None:
		"""Switch to the default output device, then fire the single confirmation sound for this startup.

		Must run on the main thread. Combines the volume-restore outcome from the
		background thread with the device-switch outcome so playConfirmationSound()
		is triggered exactly once per startup sequence, regardless of which steps
		actually changed anything.
		@param volumeStateChanged: whether unmuteAudio or unmuteNvdaProcess changed anything
		@type volumeStateChanged: bool
		"""
		deviceSwitched = self.switchToDefaultOutputDevice()
		if (volumeStateChanged or deviceSwitched) and self.settings["playsound"]:
			self.playConfirmationSound()

	def unmuteAudio(self) -> bool:
		"""Turn on Windows sound if it is muted or too low on the default playback device.

		Wrapped in a broad try/except since pycaw/comtypes can raise a COMError or
		OSError if the Windows Audio service is restarting or no device is present;
		this must fail gracefully so runStartupChecks can continue with the remaining
		startup tasks on the background thread.

		@return: True if the mute or volume state was changed
		@rtype: bool
		"""
		try:
			device = AudioUtilities.GetSpeakers()
			interface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
			endpointVolume = cast(interface, POINTER(IAudioEndpointVolume))
			volumeStateChanged = False
			if endpointVolume.GetMute():
				endpointVolume.SetMute(False, None)
				volumeStateChanged = True
			if endpointVolume.GetMasterVolumeLevelScalar() * 100 < self.settings["minlevel"]:
				self.settings["volume"] = max(self.settings["volume"], self.settings["minlevel"])
				endpointVolume.SetMasterVolumeLevelScalar(float(self.settings["volume"]) / 100.0, None)
				volumeStateChanged = True
			return volumeStateChanged
		except Exception as unmuteError:
			log.debug("Failed to unmute default playback device: %s", str(unmuteError))
			return False

	def unmuteNvdaProcess(self) -> bool:
		"""Turn on the NVDA process audio session if it is muted or too low.

		Wrapped in a broad try/except since pycaw/comtypes can raise a COMError or
		OSError if the Windows Audio service is restarting or an audio session becomes
		invalid mid-enumeration; this must fail gracefully so runStartupChecks can
		continue with the remaining startup tasks on the background thread.

		@return: True if the mute or volume state was changed
		@rtype: bool
		"""
		try:
			for session in AudioUtilities.GetAllSessions():
				if session.Process and session.Process.name().lower() == "nvda.exe":
					sessionVolume: ISimpleAudioVolume = session.SimpleAudioVolume
					volumeStateChanged = False
					if sessionVolume.GetMute():
						sessionVolume.SetMute(False, None)
						volumeStateChanged = True
					if sessionVolume.GetMasterVolume() * 100.0 < self.settings["minlevel"]:
						sessionVolume.SetMasterVolume(float(self.settings["volume"]) / 100.0, None)
						volumeStateChanged = True
					return volumeStateChanged
			return False
		except Exception as unmuteError:
			log.debug("Failed to unmute NVDA process audio session: %s", str(unmuteError))
			return False

	def restoreAllDeviceVolumes(self) -> None:
		"""Unmute every active playback device, not only the current default one.

		Uses a broad except Exception for both enumeration and per-device activation,
		since comtypes can raise _ctypes.COMError when hardware state changes mid-call
		(e.g. a Bluetooth device disconnecting), and COMError does not inherit from
		OSError, so a narrower catch would let it terminate the background thread.
		"""
		try:
			devices = AudioUtilities.GetAllDevices()
		except Exception as enumerationError:
			log.debug("Failed to enumerate audio devices for volume restore: %s", str(enumerationError))
			return
		for device in devices:
			if getattr(device, "state", DEVICE_STATE_ACTIVE) != DEVICE_STATE_ACTIVE:
				continue
			try:
				interface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
				endpointVolume = cast(interface, POINTER(IAudioEndpointVolume))
				if endpointVolume.GetMute():
					endpointVolume.SetMute(False, None)
			except Exception as deviceError:
				log.debug("Failed to restore volume for device %s: %s", getattr(device, "id", "unknown"), str(deviceError))

	def _computeMissingDeviceIds(self, currentActiveIds: Set[str]) -> Set[str]:
		"""Compare currently active devices against the last known-good list.

		Takes the already-collected set of active device IDs rather than enumerating
		hardware itself, so the caller can reuse a single COM device query for both
		this comparison and refreshing self._knownActiveDeviceIds. Reading the shared
		self._knownActiveDeviceIds cache is guarded by self._lock, since it can be
		mutated concurrently from the background thread and read from the main thread.

		@param currentActiveIds: currently active device IDs, already collected by the caller
		@type currentActiveIds: set
		@return: IDs of devices that were active last session but are no longer active
		@rtype: set
		"""
		with self._lock:
			previousActiveIds = set(self._knownActiveDeviceIds)
		if not previousActiveIds:
			return set()
		missingDeviceIds = previousActiveIds - currentActiveIds
		if missingDeviceIds:
			log.debug("Playback devices no longer active since last session: %s", missingDeviceIds)
		return missingDeviceIds

	def _alertMissingDevices(self, missingDeviceIds: Set[str]) -> None:
		"""Notify the user that a previously active playback device is now missing.

		Re-enabling a disabled Windows audio endpoint from user mode requires an
		undocumented, version-fragile COM interface, so this deliberately only
		alerts the user instead of attempting a silent, unverifiable fix.

		Must run on the main thread, since it calls ui.message() and tones.beep().
		"""
		if not missingDeviceIds:
			return
		ui.message(
			_(
				"Warning: a sound device that was previously enabled is now "
				"unavailable or disabled. Please check Sound settings."
			)
		)
		tones.beep(200, 400)

	def resetSynth(self) -> None:
		"""If the active synthesizer does not match the configured one, repeatedly attempt to initialize it.

		Must be called on the main thread. Deliberately does not call
		synthDriverHandler.initialize() itself, since NVDA's core speech subsystem
		already owns driver startup at this point in the boot sequence; calling it
		again here would race with that core initialization. Uses a non-blocking,
		self-rescheduling core.callLater loop with capped exponential backoff instead
		of a blocking sleep loop or a fixed-interval loop, so the main event pump is
		never starved by repeated failing driver reload attempts.
		"""
		activeSynth = synthDriverHandler.getSynth()
		targetSynthName = config.conf["speech"]["synth"]
		if activeSynth and activeSynth.name == targetSynthName:
			return
		self._synthResetAttempts = 0
		self._attemptSynthReset()

	def _attemptSynthReset(self) -> None:
		"""Perform a single synth reinitialization attempt and reschedule itself if needed."""
		activeSynth = synthDriverHandler.getSynth()
		targetSynthName = config.conf["speech"]["synth"]
		if activeSynth and activeSynth.name == targetSynthName:
			if self.settings["playsound"]:
				self.playConfirmationSound()
			return
		retryLimitReached = self.settings["retries"] != 0 and self._synthResetAttempts >= self.settings["retries"]
		if retryLimitReached:
			if self.settings["playsound"]:
				self.playConfirmationSound()
			return
		try:
			synthDriverHandler.setSynth(targetSynthName)
		except Exception as synthResetError:
			log.debug("Failed to reset synthesizer to %s: %s", targetSynthName, str(synthResetError))
		retryDelayMs = min(
			SYNTH_RESET_RETRY_DELAY_MS * (2 ** self._synthResetAttempts),
			SYNTH_RESET_MAX_RETRY_DELAY_MS,
		)
		self._synthResetAttempts += 1
		core.callLater(retryDelayMs, self._attemptSynthReset)

	def switchToDefaultOutputDevice(self) -> bool:
		"""Switch NVDA audio output back to the system default device.

		Must run on the main thread, since it writes to config.conf and reloads the synth driver.
		The synth driver reload is wrapped in a try/except, since the underlying speech
		engine can reject the new audio endpoint and raise, which would otherwise crash
		this main thread UI callback and skip the remaining recovery steps. Does not play
		the confirmation sound itself; the caller centralizes that so it fires exactly
		once per startup sequence alongside the volume-restore outcome.

		@return: True if the synth driver was successfully reloaded onto the default device
		@rtype: bool
		"""
		targetDevice = ""
		if config.conf["audio"]["outputDevice"] == targetDevice:
			return False
		config.conf["audio"]["outputDevice"] = targetDevice
		currentSynth = synthDriverHandler.getSynth()
		if not currentSynth:
			return False
		try:
			synthReloaded = synthDriverHandler.setSynth(currentSynth.name)
			if synthReloaded:
				tones.terminate()
				tones.initialize()
			return bool(synthReloaded)
		except Exception as deviceSwitchError:
			log.debug("Failed to reload synth after switching to default output device: %s", str(deviceSwitchError))
			return False

	def playConfirmationSound(self) -> None:
		"""Play the bundled confirmation sound when audio has been successfully restored.

		Must run on the main thread. nvwave binds to NVDA's core audio routing manager
		and WASAPI endpoints on the main thread's COM apartment, so calling this directly
		from the background startup thread (a separate STA) breaks COM marshaling and can
		raise RPC_E_WRONG_THREAD or corrupt the audio buffer pipeline.
		"""
		try:
			nvwave.playWaveFile(os.path.join(os.path.dirname(__file__), "unmuted.wav"), asynchronous=True)
		except OSError as playbackError:
			log.debug("Failed to play audio guard confirmation sound: %s", str(playbackError))

	def applySleepPrevention(self) -> None:
		"""Apply or release the Windows automatic-sleep prevention flag based on the current setting."""
		if self.settings["preventSleep"]:
			ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
			self._sleepPrevented = True
		elif self._sleepPrevented:
			ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
			self._sleepPrevented = False

	def releaseSleepPrevention(self) -> None:
		"""Release the sleep prevention flag on plugin termination, if it was applied."""
		if self._sleepPrevented:
			ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
			self._sleepPrevented = False


_activeEngine: Optional[AudioGuardEngine] = None


def getActiveEngine() -> AudioGuardEngine:
	"""Return the single shared AudioGuardEngine instance, creating it on first use.
	@return: the active engine
	@rtype: AudioGuardEngine
	"""
	global _activeEngine
	if _activeEngine is None:
		_activeEngine = AudioGuardEngine()
	return _activeEngine
