# settings.py

from typing import Callable

import wx
from gui import guiHelper, nvdaControls
from gui.settingsDialogs import SettingsPanel

from . import audioGuard
from .constants import ADDON_SUMMARY

_: Callable[[str], str]


class AudioGuardSettingsPanel(SettingsPanel):
	"""Add-on settings panel for startup volume, synth recovery, and audio guard behavior."""
	title: str = ADDON_SUMMARY

	def makeSettings(self, sizer: wx.Sizer) -> None:
		"""Populate the panel with settings controls.
		@param sizer: The sizer to which to add the settings controls.
		@type sizer: wx.Sizer
		"""
		self.sizer = sizer
		self._engine = audioGuard.getActiveEngine()
		engineSettings = self._engine.settings
		settingsHelper = guiHelper.BoxSizerHelper(self, sizer=sizer)

		self._dialogCounterChk = settingsHelper.addItem(
			# Translators: A setting in addon settings dialog.
			wx.CheckBox(self, label=_("Show countdown &confirmation dialog before shutdown or restart"))
		)
		self._dialogCounterChk.SetValue(engineSettings["dialogCounterEnabled"])
		settingsHelper.addItem(
			wx.StaticText(
				self,
				# Translators: Help message for a dialog.
				label=_("Select the initial sound system settings that will be set when NVDA starts:"),
				style=wx.ALIGN_LEFT,
			)
		)
		self._customVolumeSlider = settingsHelper.addLabeledControl(
			# Translators: A setting in addon settings dialog.
			_("Set &custom volume level:"),
			nvdaControls.EnhancedInputSlider,
			value=engineSettings["volume"], minValue=0, maxValue=100, size=(250, -1),
		)
		self._minVolumeSlider = settingsHelper.addLabeledControl(
			# Translators: A setting in addon settings dialog.
			_("Increase the volume if it is &lower than:"),
			nvdaControls.EnhancedInputSlider,
			value=engineSettings["minlevel"], minValue=0, maxValue=100, size=(250, -1),
		)
		self._driverChk = settingsHelper.addItem(
			# Translators: A setting in addon settings dialog.
			wx.CheckBox(self, label=_("&Repeat attempts to initialize the voice synthesizer driver"))
		)
		self._driverChk.SetValue(engineSettings["reinit"])
		self._retriesCountSpin = settingsHelper.addLabeledControl(
			# Translators: A setting in addon settings dialog.
			_("&Number of retries (0 - infinitely):"),
			nvdaControls.SelectOnFocusSpinCtrl,
			value=str(engineSettings["retries"]), min=0, max=10000000,
		)
		self._retriesCountSpin.Show(self._driverChk.GetValue())
		self._driverChk.Bind(wx.EVT_CHECKBOX, self.onDriverChk)
		self._switchDeviceChk = settingsHelper.addItem(
			# Translators: A setting in addon settings dialog.
			wx.CheckBox(self, label=_("Switch to the default audio output &device"))
		)
		self._switchDeviceChk.SetValue(engineSettings["switchdevice"])
		self._playSoundChk = settingsHelper.addItem(
			# Translators: A setting in addon settings dialog.
			wx.CheckBox(self, label=_("Play &sound when audio has been successfully turned on"))
		)
		self._playSoundChk.SetValue(engineSettings["playsound"])
		self._protectAllDevicesChk = settingsHelper.addItem(
			# Translators: A setting in addon settings dialog.
			wx.CheckBox(self, label=_("&Unmute all playback devices, not only the default one"))
		)
		self._protectAllDevicesChk.SetValue(engineSettings["protectAllDevices"])
		self._alertOnDisabledChk = settingsHelper.addItem(
			# Translators: A setting in addon settings dialog.
			wx.CheckBox(self, label=_("&Alert me if a playback device becomes disabled or unavailable"))
		)
		self._alertOnDisabledChk.SetValue(engineSettings["alertOnDeviceDisabled"])
		self._preventSleepChk = settingsHelper.addItem(
			# Translators: A setting in addon settings dialog.
			wx.CheckBox(self, label=_("&Prevent Windows from going to sleep automatically"))
		)
		self._preventSleepChk.SetValue(engineSettings["preventSleep"])
		sizer.Fit(self)

	def onDriverChk(self, event: wx.CommandEvent) -> None:
		"""Performed when the "self._driverChk" check box is selected or removed.
		@param event: event binder object which processes changing of the wx.Checkbox
		@type event: wx.CommandEvent
		"""
		self._retriesCountSpin.Show(self._driverChk.GetValue())
		self._retriesCountSpin.GetParent().Layout()
		self.sizer.Fit(self)

	def postInit(self) -> None:
		"""Set system focus to the custom volume slider."""
		self._customVolumeSlider.SetFocus()

	def onSave(self) -> None:
		"""Update audio guard settings and persist them to JSON when clicking OK."""
		self._engine.settings.update({
			"dialogCounterEnabled": self._dialogCounterChk.GetValue(),
			"volume": self._customVolumeSlider.GetValue(),
			"minlevel": self._minVolumeSlider.GetValue(),
			"reinit": self._driverChk.GetValue(),
			"retries": self._retriesCountSpin.GetValue(),
			"switchdevice": self._switchDeviceChk.GetValue(),
			"playsound": self._playSoundChk.GetValue(),
			"protectAllDevices": self._protectAllDevicesChk.GetValue(),
			"alertOnDeviceDisabled": self._alertOnDisabledChk.GetValue(),
			"preventSleep": self._preventSleepChk.GetValue(),
		})
		self._engine.saveSettings()
		self._engine.applySleepPrevention()
