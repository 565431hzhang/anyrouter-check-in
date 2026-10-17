"""统一日志输出：emoji 前缀 + 分区标题，保持 GitHub Actions 日志清晰易读。"""

from __future__ import annotations

from utils.debug import is_debug_enabled

LINE_HEAVY = '━' * 36
LINE_LIGHT = '─' * 36


def _emit(icon: str, message: str) -> None:
	print(f'{icon} {message}' if icon else message)


def say(message: str = '') -> None:
	"""无前缀输出。"""
	print(message)


def info(message: str) -> None:
	_emit('ℹ️ ', message)


def ok(message: str) -> None:
	_emit('✅', message)


def warn(message: str) -> None:
	_emit('⚠️ ', message)


def fail(message: str) -> None:
	_emit('❌', message)


def error(message: str) -> None:
	_emit('🚨', message)


def step(message: str) -> None:
	"""进行中的步骤。"""
	_emit('⏳', message)


def network(message: str) -> None:
	_emit('🌐', message)


def response(message: str) -> None:
	_emit('📡', message)


def auth(message: str) -> None:
	_emit('🔐', message)


def notify(message: str) -> None:
	_emit('📣', message)


def proxy(message: str) -> None:
	_emit('🛡️ ', message)


def shot(message: str) -> None:
	_emit('📸', message)


def debug(message: str) -> None:
	"""仅在 DEBUG_MODE 下输出。"""
	if is_debug_enabled():
		_emit('🐞', message)


def banner(title: str) -> None:
	"""整个流程的大标题。"""
	print(LINE_HEAVY)
	print(title)
	print(LINE_HEAVY)


def section(title: str) -> None:
	"""每个账号 / 阶段的小标题。"""
	print()
	print(LINE_LIGHT)
	print(title)
	print(LINE_LIGHT)
