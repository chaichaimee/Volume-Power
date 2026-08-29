# countdownDialog.py

from typing import Callable, Optional

import gui
import tones
import wx

_: Callable[[str], str]

COUNTDOWN_START_SECONDS = 3
COUNTDOWN_BEEP_FREQUENCIES = {3: 300, 2: 500, 1: 800}
COUNTDOWN_BEEP_DURATION_MS = 150


class PowerCountdownDialog(wx.Dialog):
	"""Non-modal confirmation dialog that counts down 3-2-1 with rising-pitch beeps
	before a shutdown or restart, cancellable at any point with Enter or Escape.
	"""

	def __init__(
		self,
		parent: wx.Window,
		actionLabel: str,
		onConfirm: Callable[[], None],
		onCancel: Optional[Callable[[], None]] = None,
	) -> None:
		super().__init__(parent, title=actionLabel, style=wx.DEFAULT_DIALOG_STYLE)
		# Without this, Windows' foreground-lock heuristics keep whatever app was
		# previously active (e.g. WordPad) in the foreground, so this dialog never
		# actually receives keyboard input and EVT_CHAR_HOOK never fires, making
		# the countdown impossible to cancel.
		gui.mainFrame.prePopup()
		self._onConfirm = onConfirm
		self._onCancel = onCancel
		self._remainingSeconds = COUNTDOWN_START_SECONDS
		self._isResolved = False

		mainSizer = wx.BoxSizer(wx.VERTICAL)
		self._messageText = wx.StaticText(
			self,
			label=_("{action} in {seconds}. Press any key to cancel.").format(
				action=actionLabel, seconds=self._remainingSeconds
			),
		)
		mainSizer.Add(self._messageText, flag=wx.ALL, border=20)
		self.SetSizerAndFit(mainSizer)
		self.CentreOnScreen()

		self.Bind(wx.EVT_CHAR_HOOK, self._onCharHook)
		self.Bind(wx.EVT_CLOSE, self._onCloseWindow)

		self._countdownTimer = wx.Timer(self)
		self.Bind(wx.EVT_TIMER, self._onTimerTick, self._countdownTimer)
		self._countdownTimer.Start(1000)
		tones.beep(COUNTDOWN_BEEP_FREQUENCIES[self._remainingSeconds], COUNTDOWN_BEEP_DURATION_MS)

	def _onTimerTick(self, event: wx.TimerEvent) -> None:
		self._remainingSeconds -= 1
		if self._remainingSeconds <= 0:
			self._resolve(confirmed=True)
			return
		self._messageText.SetLabel(_("{seconds}...").format(seconds=self._remainingSeconds))
		frequency = COUNTDOWN_BEEP_FREQUENCIES.get(self._remainingSeconds, 800)
		tones.beep(frequency, COUNTDOWN_BEEP_DURATION_MS)

	def _onCharHook(self, event: wx.KeyEvent) -> None:
		self._resolve(confirmed=False)

	def _onCloseWindow(self, event: wx.CloseEvent) -> None:
		self._resolve(confirmed=False)

	def _resolve(self, confirmed: bool) -> None:
		if self._isResolved:
			return
		self._isResolved = True
		if self._countdownTimer.IsRunning():
			self._countdownTimer.Stop()
		self.Hide()
		gui.mainFrame.postPopup()
		wx.CallAfter(self.Destroy)
		if confirmed:
			self._onConfirm()
		elif self._onCancel is not None:
			self._onCancel()


