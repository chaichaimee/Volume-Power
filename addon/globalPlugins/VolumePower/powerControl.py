# powerControl.py

import ctypes
import os
import sys
from ctypes import wintypes
from typing import List

import nvwave
import versionInfo
from logHandler import log

try:
	IS_NVDA2026_OR_NEWER = versionInfo.version_year >= 2026
except AttributeError:
	yearText = versionInfo.version.split(".")[0]
	IS_NVDA2026_OR_NEWER = int(yearText) >= 2026

SE_SHUTDOWN_NAME = "SeShutdownPrivilege"
TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_QUERY = 0x0008
SE_PRIVILEGE_ENABLED = 0x00000002

EWX_SHUTDOWN = 0x00000001
EWX_REBOOT = 0x00000002
EWX_FORCEIFHUNG = 0x00000010


class LUID(ctypes.Structure):
	_fields_ = [
		("LowPart", wintypes.DWORD),
		("HighPart", wintypes.LONG),
	]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
	_fields_ = [
		("Luid", LUID),
		("Attributes", wintypes.DWORD),
	]


class TOKEN_PRIVILEGES(ctypes.Structure):
	_fields_ = [
		("PrivilegeCount", wintypes.DWORD),
		("Privileges", LUID_AND_ATTRIBUTES * 1),
	]


advapi32 = ctypes.windll.advapi32
kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32

advapi32.OpenProcessToken.argtypes = [
	wintypes.HANDLE,
	wintypes.DWORD,
	ctypes.POINTER(wintypes.HANDLE),
]
advapi32.OpenProcessToken.restype = wintypes.BOOL

advapi32.LookupPrivilegeValueW.argtypes = [
	wintypes.LPCWSTR,
	wintypes.LPCWSTR,
	ctypes.POINTER(LUID),
]
advapi32.LookupPrivilegeValueW.restype = wintypes.BOOL

advapi32.AdjustTokenPrivileges.argtypes = [
	wintypes.HANDLE,
	wintypes.BOOL,
	ctypes.POINTER(TOKEN_PRIVILEGES),
	wintypes.DWORD,
	ctypes.POINTER(TOKEN_PRIVILEGES),
	ctypes.POINTER(wintypes.DWORD),
]
advapi32.AdjustTokenPrivileges.restype = wintypes.BOOL

user32.ExitWindowsEx.argtypes = [wintypes.UINT, wintypes.DWORD]
user32.ExitWindowsEx.restype = wintypes.BOOL

kernel32.GetCurrentProcess.argtypes = []
kernel32.GetCurrentProcess.restype = wintypes.HANDLE

kernel32.GetLastError.argtypes = []
kernel32.GetLastError.restype = wintypes.DWORD


def enableShutdownPrivilege() -> bool:
	"""Enable the SE_SHUTDOWN_NAME privilege for the current process token.
	@return: True if the privilege was enabled successfully
	@rtype: bool
	"""
	token = wintypes.HANDLE()
	processHandle = kernel32.GetCurrentProcess()
	if not advapi32.OpenProcessToken(processHandle, TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, ctypes.byref(token)):
		log.debug("OpenProcessToken failed, error: %s", kernel32.GetLastError())
		return False
	try:
		luid = LUID()
		if not advapi32.LookupPrivilegeValueW(None, SE_SHUTDOWN_NAME, ctypes.byref(luid)):
			log.debug("LookupPrivilegeValueW failed, error: %s", kernel32.GetLastError())
			return False
		tokenPrivileges = TOKEN_PRIVILEGES()
		tokenPrivileges.PrivilegeCount = 1
		tokenPrivileges.Privileges[0].Luid = luid
		tokenPrivileges.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
		if not advapi32.AdjustTokenPrivileges(
			token, False, ctypes.byref(tokenPrivileges), ctypes.sizeof(tokenPrivileges), None, None
		):
			log.debug("AdjustTokenPrivileges failed, error: %s", kernel32.GetLastError())
			return False
		return True
	finally:
		kernel32.CloseHandle(token)


def findExitSoundPath() -> str:
	"""Locate NVDA's bundled exit.wav across known installation layouts.
	@return: absolute path to exit.wav, or an empty string if not found
	@rtype: str
	"""
	possibleBaseDirs: List[str] = []
	try:
		import globalVars
		if getattr(globalVars, "appDir", None):
			possibleBaseDirs.append(globalVars.appDir)
	except ImportError:
		log.debug("globalVars module unavailable while locating exit sound")
	if sys.executable:
		executableDir = os.path.dirname(sys.executable)
		possibleBaseDirs.append(executableDir)
		parentDir = os.path.dirname(executableDir)
		if parentDir != executableDir:
			possibleBaseDirs.append(parentDir)
	possibleBaseDirs = list(dict.fromkeys(possibleBaseDirs))
	for baseDir in possibleBaseDirs:
		for subfolder in ("sounds", "waves"):
			candidatePath = os.path.join(baseDir, subfolder, "exit.wav")
			if os.path.isfile(candidatePath):
				return candidatePath
	return ""


def playExitSound() -> bool:
	"""Play NVDA's exit.wav using nvwave so the user's configured output volume is respected.
	@return: True if playback was started
	@rtype: bool
	"""
	soundPath = findExitSoundPath()
	if not soundPath:
		log.debug("exit.wav not found in any searched location")
		return False
	try:
		nvwave.playWaveFile(soundPath)
		return True
	except OSError as soundError:
		log.debug("Failed to play exit sound via nvwave: %s", soundError)
		return False


def performShutdown(reboot: bool) -> None:
	"""Shut down or restart Windows using ExitWindowsEx.
	@param reboot: True to restart, False to shut down
	@type reboot: bool
	@raises RuntimeError: if the shutdown privilege cannot be obtained or ExitWindowsEx fails
	"""
	if not enableShutdownPrivilege():
		raise RuntimeError(_("Cannot get shutdown privilege"))
	flags = (EWX_REBOOT if reboot else EWX_SHUTDOWN) | EWX_FORCEIFHUNG
	if not user32.ExitWindowsEx(flags, 0):
		errorCode = kernel32.GetLastError()
		raise RuntimeError(_("ExitWindowsEx failed with error {error}").format(error=errorCode))
	log.debug("System %s initiated via Windows API", "reboot" if reboot else "shutdown")
