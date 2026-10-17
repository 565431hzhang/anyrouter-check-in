"""代理配置：读取环境变量并供浏览器 / HTTP 客户端使用。

要求走代理（use_proxy=True）但代理未就绪时直接抛错，绝不静默回退直连。
"""

from __future__ import annotations

import os


class ProxyUnavailableError(RuntimeError):
	"""平台要求走代理，但没有可用的代理。"""


def get_proxy_server(*, use_proxy: bool = True) -> str | None:
	"""use_proxy=False 返回 None；use_proxy=True 必须有 CHECKIN_PROXY_URL，否则抛 ProxyUnavailableError。"""
	if not use_proxy:
		return None
	server = os.getenv('CHECKIN_PROXY_URL', '').strip()
	if not server:
		raise ProxyUnavailableError('该平台要求走代理，但代理未就绪（NODE_LINK 未配置或代理启动失败），已拒绝直连')
	return server


def get_playwright_proxy(*, use_proxy: bool = True) -> dict[str, str] | None:
	server = get_proxy_server(use_proxy=use_proxy)
	if not server:
		return None
	return {'server': server}
